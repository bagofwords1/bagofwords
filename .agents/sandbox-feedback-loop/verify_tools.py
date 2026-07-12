#!/usr/bin/env python
"""Deterministic tool-layer verification (no LLM).

Drives the REAL agent tools' run_stream (list_files / read_file / search_files)
against the seeded connections, asserting the full test matrix:
glob scope + access enforcement, live vs cached listing, windowed/cursor reads
incl. a 44MB file paged to EOF — on BOTH network_dir and S3.
"""
from __future__ import annotations
import asyncio, json, pkgutil, importlib, sys

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

SEED = json.load(open("/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime/seed.json"))
CONN = {name: cid for name, cid in SEED["connections"]}
FIN = CONN["Finance Share (content)"]
LOGS = CONN["Ops Logs (live)"]
S3 = CONN["S3 bowathena docs (content)"]

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌ FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))


async def run_tool(tool, tool_input, ctx):
    out = None
    async for ev in tool.run_stream(tool_input, ctx):
        if getattr(ev, "type", None) == "tool.end":
            out = ev.payload["output"]
    return out


async def main():
    async with async_session_maker() as db:
        report = (await db.execute(
            select(Report)
            .where(Report.id == SEED["report_id"])
            .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
        )).scalar_one()
        org = (await db.execute(select(Organization).where(Organization.id == SEED["org_id"]))).scalar_one()
        user = (await db.execute(select(User).where(User.email == "admin@example.com"))).scalar_one()
        ctx = {"db": db, "organization": org, "user": user, "report": report}

        lf, rf, sf = ListFilesTool(), ReadFileTool(), SearchFilesTool()

        print("=== A. network_dir CONTENT connection (globs: reports/**/*.csv, docs/**, files/**/*.ppt) ===")
        out = await run_tool(lf, {"connection_id": FIN, "recursive": True}, ctx)
        ids = [f["id"] for f in out["files"]]
        check("A.list_files returns files", out["success"] and out["file_count"] > 0, str(out)[:120])
        check("A.list_files scope: no secrets/env/key/pptx leaked",
              not any(("secret" in i or i.endswith((".env",".key",".pptx",".pdf")) or i.startswith("logs/")) for i in ids),
              f"leak sample={[i for i in ids if 'secret' in i or i.endswith(('.env','.key','.pptx'))][:3]}")
        # read an allowed csv (whole/parsed)
        a_csv = next(i for i in ids if i.endswith(".csv"))
        out = await run_tool(rf, {"connection_id": FIN, "file_id": a_csv}, ctx)
        check("A.read_file csv parsed (tabular)", out["success"] and out.get("content_type") == "tabular", str(out)[:150])
        # OFF-GLOB read must be denied
        out = await run_tool(rf, {"connection_id": FIN, "file_id": "secrets/prod.env"}, ctx)
        check("A.read off-glob secrets/prod.env DENIED", (not out["success"]) and ("allowed patterns" in (out.get("error") or "").lower() or "denied" in (out.get("error") or "").lower()),
              f"got={out}")
        out = await run_tool(rf, {"connection_id": FIN, "file_id": "files/sales/deck_sales_000.pptx"}, ctx)
        check("A.read off-glob .pptx DENIED", not out["success"], f"got={str(out)[:120]}")
        # content search
        out = await run_tool(sf, {"connection_id": FIN, "query": "revenue"}, ctx)
        check("A.search_files content finds matches", out["success"] and out.get("file_count", 0) > 0, str(out)[:120])

        print("=== B. network_dir LIVE connection (index_mode=none, globs: logs/*.log, data/*.ndjson) ===")
        out = await run_tool(lf, {"connection_id": LOGS, "recursive": True}, ctx)
        ids = [f["id"] for f in out["files"]]
        check("B.list_files LIVE returns files", out["success"] and out["file_count"] > 0, str(out)[:120])
        check("B.live listing includes huge_stream.log", any(i.endswith("huge_stream.log") for i in ids))
        check("B.live scope: only logs/ndjson", all(i.endswith((".log",".ndjson")) for i in ids), f"sample={ids[:4]}")
        # windowed read of the 44MB file, page to EOF
        fid = "logs/huge_stream.log"
        w = await run_tool(rf, {"connection_id": LOGS, "file_id": fid, "offset": 0, "length": 200}, ctx)
        check("B.windowed read returns window", w["success"] and w.get("windowed") and w.get("total_size") == 44333340, str(w)[:150])
        check("B.window snaps to newline", (w.get("text") or "").endswith("\n") and (w.get("text") or "").startswith("LINE 0000000"))
        cur, nlines, iters = 0, 0, 0
        while True:
            w = await run_tool(rf, {"connection_id": LOGS, "file_id": fid, "offset": cur, "length": 2_000_000}, ctx)
            nlines += (w.get("text") or "").count("\n"); iters += 1
            if w.get("eof"): break
            cur = w["next_cursor"]
            if iters > 200: check("B.huge paging terminates", False, "runaway"); break
        check("B.huge file paged to EOF: 600000 lines", nlines == 600000, f"got {nlines} in {iters} windows")
        # off-glob deny on live connection too
        out = await run_tool(rf, {"connection_id": LOGS, "file_id": "secrets/prod.env"}, ctx)
        check("B.off-glob DENIED on live conn", not out["success"], str(out)[:100])

        print("=== C. S3 CONTENT connection (bucket bowathena14, globs: docs/**) ===")
        out = await run_tool(lf, {"connection_id": S3, "recursive": True}, ctx)
        ids = [f["id"] for f in out["files"]]
        check("C.S3 list_files returns docs", out["success"] and out["file_count"] > 0, str(out)[:120])
        check("C.S3 scope: only docs/*", all(i.startswith("docs/") for i in ids), f"sample={ids[:5]}")
        # read a doc (whole)
        out = await run_tool(rf, {"connection_id": S3, "file_id": "docs/revenue.csv"}, ctx)
        check("C.S3 read docs/revenue.csv parsed", out["success"] and out.get("content_type") == "tabular", str(out)[:150])
        # off-glob deny (results/ and parquet are outside docs/**)
        out = await run_tool(rf, {"connection_id": S3, "file_id": "output.parquet"}, ctx)
        check("C.S3 off-glob output.parquet DENIED", not out["success"], str(out)[:120])
        # windowed read on S3
        w = await run_tool(rf, {"connection_id": S3, "file_id": "docs/events.log", "offset": 0, "length": 100}, ctx)
        check("C.S3 windowed read works", w["success"] and w.get("windowed") and w.get("total_size", 0) > 0, str(w)[:150])
        # S3 content search — must be scoped to THIS connection (no netdir leak)
        out = await run_tool(sf, {"connection_id": S3, "query": "revenue"}, ctx)
        s3_ids = [f["id"] for f in out.get("files", [])]
        real_s3 = {"docs/events.log", "docs/pnl.xlsx", "docs/revenue.csv", "docs/team.json"}
        check("C.S3 content search returns matches", out["success"] and out.get("file_count", 0) > 0, str(out)[:120])
        check("C.S3 search scoped to this connection (no netdir leak)",
              all(i in real_s3 for i in s3_ids), f"leaked={[i for i in s3_ids if i not in real_s3][:5]}")

        # Cross-check: netdir search stays in netdir
        out = await run_tool(sf, {"connection_id": FIN, "query": "revenue"}, ctx)
        fin_ids = [f["id"] for f in out.get("files", [])]
        check("A2.netdir search scoped (no S3 leak, only globbed paths)",
              all((i.startswith(("reports/", "docs/", "files/")) and not i.endswith((".env",".key"))) for i in fin_ids),
              f"sample={fin_ids[:5]}")

    print(f"\n==== RESULT: {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
    print("ALL TOOL-LAYER CHECKS PASSED ✅")


asyncio.run(main())
