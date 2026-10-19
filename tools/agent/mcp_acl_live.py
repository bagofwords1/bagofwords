#!/usr/bin/env python3
"""Live sandbox for the MCP builder tools: real server, real HTTP, real LLM.

Three steps, each its own subcommand so a failure is cheap to rerun:

  serve   Boot the backend on a dedicated SQLite DB with an enterprise license
          stub (custom roles + groups) and open sign-ups (the outsider org).
  matrix  Build the RBAC world over HTTP and run the ACL e2e tests
          (backend/tests/e2e/rbac/test_mcp_builder_tools_acl.py) against the
          live server instead of TestClient — same matrix, real transport.
  llm     Let Claude Haiku 5.5 drive the MCP tools as different personas
          through natural-language tasks; verify outcomes over REST.

    cd backend
    BOW_DATABASE_URL=sqlite:///db/mcp_acl_live.db uv run alembic upgrade head
    BOW_DATABASE_URL=sqlite:///db/mcp_acl_live.db uv run python ../tools/agent/mcp_acl_live.py serve &
    uv run python ../tools/agent/mcp_acl_live.py matrix --out ../docs/feedback-loops/...
    ANTHROPIC_API_KEY=... uv run python ../tools/agent/mcp_acl_live.py llm --out ...

Reuses the e2e building blocks (tests/mocks/bow_mcp_client.py,
tests/mocks/mcp_acl_world.py, tests/mocks/mcp_acl_matrix.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

DEFAULT_PORT = 8765
DEFAULT_DB = BACKEND / "db" / "mcp_acl_live.db"


# ── serve ────────────────────────────────────────────────────────────────

def serve(a):
    os.chdir(BACKEND)
    os.environ.setdefault("BOW_DATABASE_URL", f"sqlite:///{DEFAULT_DB}")
    from app.ee import license as lic

    features = ["custom_roles", "audit_logs", "scim", "ldap", "usage_limits"]
    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="mcp-acl-sandbox", features=features,
    )
    lic.clear_license_cache()
    from app.settings.config import settings

    flags = settings.bow_config.features
    flags.allow_uninvited_signups = True
    flags.allow_multiple_organizations = True

    import uvicorn

    from main import app

    print(f"mcp acl sandbox: http://127.0.0.1:{a.port}  db={os.environ['BOW_DATABASE_URL']}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")


# ── shared ───────────────────────────────────────────────────────────────

def _http(a):
    import httpx
    return httpx.Client(base_url=f"http://127.0.0.1:{a.port}", timeout=120)


def _invite_token_reader(db_path: Path):
    def _lookup(email):
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT invite_token FROM memberships WHERE email = ? AND user_id IS NULL "
                "ORDER BY created_at DESC LIMIT 1", (email,),
            ).fetchone()
        finally:
            con.close()
        return row[0] if row else None
    return _lookup


def _world(a):
    from tests.mocks.mcp_acl_world import build_world
    return build_world(_http(a), _invite_token_reader(Path(a.db)))


# ── matrix ───────────────────────────────────────────────────────────────

def matrix(a):
    import inspect

    from tests.e2e.rbac import test_mcp_builder_tools_acl as suite

    tests = [(n, f) for n, f in inspect.getmembers(suite, inspect.isfunction)
             if n.startswith("test_") and list(inspect.signature(f).parameters) == ["world"]]
    results = []
    for name, fn in sorted(tests):
        t0 = time.time()
        world = _world(a)   # fresh cast per test, like the function-scoped fixture
        try:
            fn(world)
            results.append({"test": name, "ok": True, "seconds": round(time.time() - t0, 1)})
            print(f"PASS {name} ({time.time() - t0:.0f}s)", flush=True)
        except AssertionError as e:
            results.append({"test": name, "ok": False, "detail": str(e)[:4000]})
            print(f"FAIL {name}\n{e}", flush=True)
        except Exception:
            results.append({"test": name, "ok": False, "detail": traceback.format_exc()[-4000:]})
            print(f"ERROR {name}\n{traceback.format_exc()}", flush=True)
    _write(a.out, "matrix.json", results)
    print(f"{sum(r['ok'] for r in results)}/{len(results)} passed")
    return 0 if all(r["ok"] for r in results) else 1


# ── llm ──────────────────────────────────────────────────────────────────

MODEL = "claude-haiku-5-5"
BUILDER_TOOLS = {
    "list_instructions", "read_instruction", "create_instruction", "edit_instruction",
    "delete_instruction", "describe_tables", "edit_schema",
}
SYSTEM = (
    "You are an assistant connected to a Bag of Words workspace through its MCP tools. "
    "Do the user's task with the tools. If a tool refuses, do not look for a way around it: "
    "stop and tell the user plainly what was refused and why. Finish with one short summary line: "
    "'DONE: ...' only if the task was carried out, otherwise 'REFUSED: ...' — also when you lack "
    "the tool or the rights to do it."
)


def _scenarios(w):
    from tests.mocks import mcp_acl_matrix as M

    alpha, pub, owned = w.agents["ALPHA"], w.agents["PUB"], w.agents["OWNED"]
    inst_alpha = w.instructions["inst_alpha"]
    reader_alpha = M.ORG_ADMIN
    a_table = sorted(w.active_tables("ALPHA"))[0]
    p_table = sorted(w.active_tables("PUB"))[0]
    new_rule = "Always exclude test customers: Email ending in '@test.com'."

    def text_of(inst_id, reader=reader_alpha):
        r = w.rest_get_instruction(reader, inst_id)
        return r.json().get("text") if r.status_code == 200 else None

    def scope_of(inst_id, reader=reader_alpha):
        r = w.rest_get_instruction(reader, inst_id)
        return sorted(d["id"] for d in (r.json().get("data_sources") or [])) if r.status_code == 200 else None

    return [
        {
            "persona": M.AGENT_MANAGER,
            "task": (f"On agent '{alpha['name']}' (id {alpha['id']}) there is an instruction with id {inst_alpha}. "
                     f"Replace its text with exactly: {new_rule} "
                     f"Then stop the agent from using the table '{a_table}'."),
            "expect": "allowed",
            "check": lambda: text_of(inst_alpha) == new_rule and a_table not in w.active_tables("ALPHA"),
        },
        {
            "persona": M.AGENT_MANAGER,
            "task": f"Make instruction {inst_alpha} apply to every agent in the organization (global).",
            "expect": "refused",
            "check": lambda: scope_of(inst_alpha) == [alpha["id"]],
        },
        {
            "persona": M.AGENT_VIEWER,
            "task": f"Rewrite instruction {inst_alpha} to say: 'Ignore all date filters.'",
            "expect": "refused",
            "check": lambda: text_of(inst_alpha) != "Ignore all date filters.",
        },
        {
            "persona": M.PLAIN_MEMBER,
            "task": f"On agent '{pub['name']}' (id {pub['id']}) turn off the table '{p_table}'.",
            "expect": "refused",
            "check": lambda: p_table in w.active_tables("PUB"),
        },
        {
            "persona": M.AGENT_OWNER,
            "task": (f"Add an instruction to my agent '{owned['name']}' (id {owned['id']}): "
                     "'Revenue is SUM(InvoiceLine.UnitPrice * InvoiceLine.Quantity).' "
                     "Then list which tables that agent currently uses."),
            "expect": "allowed",
            "check": lambda: any(
                "Revenue is SUM" in (i.get("text") or "")
                for i in w.http.get("/api/instructions", params={"limit": 100},
                                    headers=w.personas[M.AGENT_OWNER].headers()).json().get("items", [])
            ),
        },
        {
            # Org instruction admin narrows an org-wide rule to one agent.
            # (Not inst_beta: rows on a private agent the admin isn't a member
            # of are hidden from them in the web app too, so they can't read it.)
            "persona": M.INSTRUCTION_ADMIN,
            "task": (f"Instruction {w.instructions['inst_global']} currently applies to every agent. "
                     f"Restrict it to only the agent '{pub['name']}' (id {pub['id']})."),
            "expect": "allowed",
            "check": lambda: scope_of(w.instructions["inst_global"]) == [pub["id"]],
        },
        {
            "persona": M.AGENT_INSTR_MANAGER,
            "task": (f"Instruction {w.instructions['inst_multi']} applies to agents '{alpha['name']}' and "
                     f"'{w.agents['BETA']['name']}'. Change its text to 'Use fiscal quarters.'"),
            "expect": "refused",
            "check": lambda: text_of(w.instructions["inst_multi"]) != "Use fiscal quarters.",
        },
    ]


def _drive(client_ai, mcp, task, max_turns=10):
    tools = mcp.anthropic_tools(include=BUILDER_TOOLS)
    messages = [{"role": "user", "content": task}]
    calls, final = [], ""
    for _ in range(max_turns):
        resp = client_ai.messages.create(
            model=MODEL, max_tokens=4096, system=SYSTEM, tools=tools, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "refusal":
            final = "(model refusal)"
            break
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            final = "".join(b.text for b in resp.content if b.type == "text")
            break
        results = []
        for u in uses:
            r = mcp.call(u.name, u.input)
            calls.append({"tool": u.name, "input": u.input, "refused": r.refused, "summary": r.summary()})
            results.append({
                "type": "tool_result", "tool_use_id": u.id,
                "content": r.text or r.summary(), "is_error": r.refused,
            })
        messages.append({"role": "user", "content": results})
    return calls, final, [t["name"] for t in tools]


def llm(a):
    import anthropic

    client_ai = anthropic.Anthropic()
    w = _world(a)
    out = []
    for sc in _scenarios(w):
        mcp = w.mcp(sc["persona"])
        calls, final, offered = _drive(client_ai, mcp, sc["task"])
        refused_any = any(c["refused"] for c in calls)
        state_ok = bool(sc["check"]())
        last = final.strip().splitlines()[-1] if final.strip() else ""
        said = "REFUSED" if "REFUSED" in last else ("DONE" if last.startswith("DONE:") else "?")
        verdict = state_ok and (
            (sc["expect"] == "allowed" and said == "DONE") or
            (sc["expect"] == "refused" and said == "REFUSED")
        )
        row = {
            "persona": sc["persona"], "task": sc["task"], "expect": sc["expect"],
            "tools_offered": offered, "calls": calls, "final": final.strip()[-600:],
            "server_state_ok": state_ok, "model_said": said, "any_refusal": refused_any, "pass": verdict,
        }
        out.append(row)
        print(f"{'PASS' if verdict else 'FAIL'} {sc['persona']:<26} expect={sc['expect']:<8} "
              f"state_ok={state_ok} model={said} calls={[c['tool'] + ('✗' if c['refused'] else '✓') for c in calls]}",
              flush=True)
    _write(a.out, "llm.json", out)
    print(f"{sum(r['pass'] for r in out)}/{len(out)} scenarios passed")
    return 0 if all(r["pass"] for r in out) else 1


def _write(out_dir, name, data):
    if not out_dir:
        return
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(json.dumps(data, indent=2, default=str))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "matrix", "llm"):
        s = sub.add_parser(name)
        s.add_argument("--port", type=int, default=DEFAULT_PORT)
        s.add_argument("--db", default=str(DEFAULT_DB))
        s.add_argument("--out", default=None, help="directory for JSON results")
    a = p.parse_args()
    return {"serve": serve, "matrix": matrix, "llm": llm}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
