#!/usr/bin/env python
"""Live LLM end-to-end verification.

Drives a real Anthropic model through the ACTUAL agent tool schemas
(list_files / search_files / read_file), dispatching each tool call to the
REAL tool.run_stream with a real runtime_ctx (DB, report, org, user). Proves the
model-facing contract works: completion, glob-denial surfacing, and
windowed/cursor reads of a huge file — on network_dir AND S3.
"""
from __future__ import annotations
import asyncio, json, os, pkgutil, importlib, sys

import anthropic
import app.models as _m
for _mod in pkgutil.iter_modules(_m.__path__):
    if _mod.name != "application":
        importlib.import_module(f"app.models.{_mod.name}")

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.user import User
from app.ai.tools.implementations.list_files import ListFilesTool
from app.ai.tools.implementations.read_file import ReadFileTool
from app.ai.tools.implementations.search_files import SearchFilesTool

MODEL = "claude-haiku-4-5-20251001"
SEED = json.load(open("/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime/seed.json"))
CONN = {n: c for n, c in SEED["connections"]}
FIN, LOGS, S3 = CONN["Finance Share (content)"], CONN["Ops Logs (live)"], CONN["S3 bowathena docs (content)"]

TOOLS = {"list_files": ListFilesTool(), "read_file": ReadFileTool(), "search_files": SearchFilesTool()}


def anth_tools():
    out = []
    for t in TOOLS.values():
        m = t.metadata
        out.append({"name": m.name, "description": m.description, "input_schema": m.input_schema})
    return out


def _compact(out: dict) -> dict:
    """Trim tool output for the model, preserving the signal it needs."""
    if not isinstance(out, dict):
        return {"result": str(out)[:500]}
    c = dict(out)
    if c.get("files"):
        c["files"] = [{"id": f.get("id"), "name": f.get("name")} for f in c["files"][:25]]
        c["files_shown"] = len(c["files"])
    if c.get("csv"):
        c["csv"] = c["csv"][:800]
    if c.get("text"):
        c["text"] = c["text"][:1500]
    return c


async def dispatch(name, args, ctx, trace):
    tool = TOOLS.get(name)
    if not tool:
        return {"success": False, "error": f"unknown tool {name}"}
    out = None
    async for ev in tool.run_stream(args, ctx):
        if getattr(ev, "type", None) == "tool.end":
            out = ev.payload["output"]
    trace.append({"tool": name, "args": args, "success": bool(out and out.get("success")),
                  "error": (out or {}).get("error")})
    return _compact(out or {"success": False, "error": "no output"})


async def run_scenario(anth, ctx, system, task, max_turns=12):
    trace = []
    messages = [{"role": "user", "content": task}]
    final_text = ""
    for _ in range(max_turns):
        resp = anth.messages.create(model=MODEL, max_tokens=1200, system=system,
                                    tools=anth_tools(), messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                final_text += b.text.strip() + "\n"
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            out = await dispatch(tu.name, tu.input, ctx, trace)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})
    return final_text.strip(), trace


PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌ FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))


async def main():
    anth = anthropic.Anthropic()
    async with async_session_maker() as db:
        report = (await db.execute(select(Report).where(Report.id == SEED["report_id"])
                  .options(selectinload(Report.data_sources).selectinload(DataSource.connections)))).scalar_one()
        org = (await db.execute(select(Organization).where(Organization.id == SEED["org_id"]))).scalar_one()
        user = (await db.execute(select(User).where(User.email == "admin@example.com"))).scalar_one()
        ctx = {"db": db, "organization": org, "user": user, "report": report}

        sysmsg = ("You operate file connections through tools. Always pass the exact connection_id "
                  "given in the task. Use the tools to complete the task, then give a short final answer.")

        print("=== Scenario 1: COMPLETION (S3 list + read) ===")
        text, trace = await run_scenario(anth, ctx, sysmsg,
            f"Using connection_id '{S3}': list the files, then read 'docs/team.json' and tell me the JSON keys. Then stop.")
        print("  final:", text[:200].replace("\n", " "))
        names = [t["tool"] for t in trace]
        check("S1 called list_files", "list_files" in names)
        check("S1 called read_file", "read_file" in names)
        check("S1 all tool calls succeeded", all(t["success"] for t in trace), str([t for t in trace if not t["success"]])[:200])

        print("=== Scenario 2: GLOB DENIAL surfaces to the model ===")
        text, trace = await run_scenario(anth, ctx, sysmsg,
            f"Using connection_id '{FIN}': try to read the file 'secrets/prod.env'. "
            f"If you are not allowed to access it, tell me clearly that access was denied and why. Then stop.")
        print("  final:", text[:250].replace("\n", " "))
        denied_calls = [t for t in trace if t["tool"] == "read_file" and not t["success"]]
        check("S2 read of secret was attempted and denied by the tool", len(denied_calls) >= 1,
              str(trace)[:200])
        check("S2 denial reason mentions patterns/denied/allowed", any(
            kw in (t.get("error") or "").lower() for t in denied_calls for kw in ("denied", "allowed patterns", "outside")))
        check("S2 model reports it could not access the file", any(
            kw in text.lower() for kw in ("denied", "not allowed", "cannot", "can't", "outside", "no access", "unable")))

        print("=== Scenario 3: WINDOWED reads of a 44MB file (first + last line) ===")
        text, trace = await run_scenario(anth, ctx, sysmsg,
            f"Using connection_id '{LOGS}': the file 'logs/huge_stream.log' is ~44MB. "
            f"Do a windowed read at offset 0 with length 300 to get the FIRST line and the total_size. "
            f"Then do a windowed read near the end (offset = total_size - 300) to get the LAST line. "
            f"Report the exact first line and last line. Then stop.", max_turns=10)
        print("  final:", text[:300].replace("\n", " "))
        win_calls = [t for t in trace if t["tool"] == "read_file" and t.get("args", {}).get("offset") is not None]
        check("S3 model used windowed reads (offset set)", len(win_calls) >= 2, f"win_calls={len(win_calls)}")
        check("S3 model reported the correct last line", "0599999" in text, "expected LINE 0599999 in answer")
        check("S3 model reported the first line", "0000000" in text or "LINE 0" in text)

    print(f"\n==== LLM RESULT: {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL); sys.exit(1)
    print("ALL LIVE-LLM CHECKS PASSED ✅")


asyncio.run(main())
