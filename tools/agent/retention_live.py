#!/usr/bin/env python3
"""Live loop for report / dashboard / file retention, against a running stack.

Drives real agent runs through the API (any configured model; the stub LLM
from tools/agent/stub_llm.py makes it deterministic), so every tree is what
production writes: completions, agent executions, plan decisions, tool
executions, completion blocks, context snapshots, widgets, steps, queries.
Then it ages some of them, turns retention on through the API, reads the
preview, runs the nightly job (the function the scheduler calls) and checks
every scenario, the whole database for dangling references, and the bytes of
deleted files on disk. A second run must delete nothing.

    cd backend
    TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \\
      uv run python ../tools/agent/retention_live.py --db-path db/agent.db

DevBox: source tools/agent/devbox_env.sh first; --db-path "$BOW_SQLITE_DB".
Prints PASS/FAIL lines and a JSON summary (`--out`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import uuid
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore", category=DeprecationWarning)

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base-url", default="http://localhost:8000")
p.add_argument("--email", default=os.environ.get("BOW_EMAIL", "admin@example.com"))
p.add_argument("--password", default=os.environ.get("BOW_PASSWORD", "Password123!"))
p.add_argument("--db-path", default="db/agent.db")
p.add_argument("--days", type=int, default=30)
p.add_argument("--prompt", default="How many orders per region? Show a chart.")
p.add_argument("--out", default=None)
a = p.parse_args()

c = httpx.Client(base_url=a.base_url, timeout=120, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
_all = c.get("/api/data_sources", headers=H).json()
# The stub LLM queries the seeded SQLite source (seed_org.py --sqlite-sources 1).
sources = ([d["id"] for d in _all if d["name"].startswith("sqlite_source")] or [d["id"] for d in _all])[:1]

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail="") -> None:
    results.append((bool(ok), label, str(detail)))
    print(("PASS " if ok else "FAIL ") + label + (f"  {detail}" if detail else ""), flush=True)


def sql(q, params=()):
    conn = sqlite3.connect(a.db_path)
    try:
        cur = conn.execute(q, params)
        rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def chat(title: str, turns: int = 1) -> str:
    rep = c.post("/api/reports", json={"title": title, "widget": None, "files": [], "data_sources": sources},
                 headers=H).json()
    for _ in range(turns):
        r = c.post(f"/api/reports/{rep['id']}/completions", params={"background": True}, headers=H,
                   json={"prompt": {"content": a.prompt, "widget_id": None, "step_id": None, "mentions": [{}]}})
        r.raise_for_status()
        deadline = time.monotonic() + 300
        time.sleep(2)
        while time.monotonic() < deadline:
            n = sql("SELECT COUNT(*) FROM completions WHERE report_id=? AND status='in_progress'", (rep["id"],))[0][0]
            if n == 0:
                break
            time.sleep(1.5)
    failed = sql("SELECT completion FROM completions WHERE report_id=? AND role='system' AND status!='success'",
                 (rep["id"],))
    if failed:
        raise SystemExit(f"agent run failed in {title!r}: {failed[0][0][:300]} (is the model configured?)")
    return rep["id"]


def upload(report_id: str) -> str:
    r = c.post("/api/files", headers=H, data={"report_id": report_id},
               files={"file": (f"retention-{uuid.uuid4().hex[:6]}.csv", b"region,orders\nEU,3\nUS,5\n", "text/csv")})
    r.raise_for_status()
    return r.json()["id"]


def dashboard(report_id: str, title: str) -> str:
    r = c.post("/api/artifacts", headers=H, json={
        "report_id": report_id, "title": title, "mode": "page",
        "content": {"code": "function App() { return <div><h1>Orders</h1></div>; }"}})
    r.raise_for_status()
    # The route answers with the version; the dashboard is its artifact.
    return sql("SELECT artifact_id FROM artifact_versions WHERE id=?", (r.json()["id"],))[0][0]


def age(report_id: str, days: int, *, activity_days: int | None = None) -> None:
    """Make a conversation (and its runs, dashboards and files) ``days`` old."""
    t = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")
    act = t if activity_days is None else (datetime.utcnow() - timedelta(days=activity_days)).strftime("%Y-%m-%d %H:%M:%S.%f")
    sql("UPDATE reports SET created_at=?, updated_at=?, last_activity_at=? WHERE id=?", (t, t, act, report_id))
    sql("UPDATE completions SET created_at=?, updated_at=? WHERE report_id=?", (t, t, report_id))
    sql("UPDATE agent_executions SET created_at=?, updated_at=? WHERE report_id=?", (t, t, report_id))
    sql("UPDATE artifacts SET created_at=?, updated_at=?, content_updated_at=? WHERE report_id=?", (t, t, t, report_id))
    sql("UPDATE artifact_versions SET created_at=?, updated_at=? WHERE report_id=?", (t, t, report_id))


def age_artifact(artifact_id: str, days: int) -> None:
    t = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")
    sql("UPDATE artifacts SET created_at=?, updated_at=?, content_updated_at=? WHERE id=?", (t, t, t, artifact_id))
    sql("UPDATE artifact_versions SET created_at=?, updated_at=? WHERE artifact_id=?", (t, t, artifact_id))


# -- the scenarios ------------------------------------------------------------

print(f"org {org}, data source {sources}", flush=True)
S: dict[str, dict] = {}

def scenario(key, label, expect, **ids):
    S[key] = {"label": label, "expect": expect, **ids}

rid = chat("S1 old chat, two turns", turns=2)
own_file = upload(rid); shared_file = upload(rid)  # an upload is activity: age after it
age(rid, 60)
scenario("S1", "old chat, two turns, with its own upload", "deleted", report=rid, file=own_file)

rid2 = chat("S2 recent chat")
sql("INSERT INTO report_file_association (report_id, file_id) VALUES (?, ?)", (rid2, shared_file))
scenario("S2", "recent chat (also uses S1's second upload)", "kept", report=rid2, file=shared_file)

rid = chat("S3 old chat, message yesterday"); age(rid, 60, activity_days=1)
scenario("S3", "old chat with activity yesterday", "kept", report=rid)

rid = chat("S4 scheduled report"); age(rid, 60)
c.post(f"/api/reports/{rid}/scheduled-prompts", headers=H,
       json={"prompt": {"content": "daily orders"}, "cron_schedule": "0 9 * * *"}).raise_for_status()
scenario("S4", "old report with an active schedule", "kept", report=rid)

rid = chat("S6 shared conversation"); age(rid, 60)
sql("UPDATE reports SET conversation_visibility='public' WHERE id=?", (rid,))
scenario("S6", "old conversation shared by link", "kept", report=rid)

rid = chat("S7 old chat, shared dashboard"); art = dashboard(rid, "Shared dashboard"); age(rid, 60)
sql("UPDATE artifacts SET visibility='shared' WHERE id=?", (art,))
scenario("S7", "old chat whose dashboard is shared", "kept", report=rid, artifact=art)

rid = chat("S8 old chat, unpublished dashboard"); art = dashboard(rid, "Draft dashboard"); age(rid, 60)
scenario("S8", "old chat with a never-published dashboard", "deleted", report=rid, artifact=art)

rid = chat("S9 rewound turns", turns=2); age(rid, 60)
last = sql("SELECT id FROM completions WHERE report_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (rid,))[0][0]
sql("UPDATE completions SET deleted_at=? WHERE id=?", (datetime.utcnow().isoformat(sep=" "), last))
scenario("S9", "old chat with a rewound turn", "deleted", report=rid, rewound=last)

rid = chat("S10 instruction learned from the run"); age(rid, 60)
instr = None
r = c.post("/api/instructions", headers=H, json={"text": "Orders are counted per region.", "category": "general",
                                                 "data_source_ids": sources})
if r.status_code == 200:
    instr = r.json()["id"]
    ex = sql("SELECT id FROM agent_executions WHERE report_id=? LIMIT 1", (rid,))[0][0]
    sql("UPDATE instructions SET agent_execution_id=? WHERE id=?", (ex, instr))
scenario("S10", "old chat an instruction was learned from", "deleted", report=rid, instruction=instr)

rid = chat("S13 run in progress"); age(rid, 60)
sql("UPDATE agent_executions SET status='in_progress' WHERE id=(SELECT id FROM agent_executions WHERE report_id=? LIMIT 1)", (rid,))
scenario("S13", "old chat with a run in progress", "kept", report=rid)

home = chat("S14 home of a standalone dashboard")
art = dashboard(home, "Standalone old dashboard")
sql("UPDATE artifacts SET report_id=NULL WHERE id=?", (art,)); age_artifact(art, 60)
scenario("S14", "old dashboard that left its (recent) conversation", "artifact deleted", report=home, artifact=art)

art2 = dashboard(home, "Standalone recent dashboard")
sql("UPDATE artifacts SET report_id=NULL WHERE id=?", (art2,))
scenario("S15", "recent standalone dashboard", "artifact kept", report=home, artifact=art2)

old_upload = upload(rid2)
sql("UPDATE files SET created_at=? WHERE id=?",
    ((datetime.utcnow() - timedelta(days=60)).isoformat(sep=" "), old_upload))
scenario("S16", "60-day-old upload in a recent chat (file retention)", "file deleted", report=rid2, file=old_upload)


# -- census before ------------------------------------------------------------

sys.path.insert(0, os.getcwd())
from app.dependencies import async_session_maker  # noqa: E402
from app.services.retention import purge  # noqa: E402
from tests.fixtures.retention_tree import orphans  # noqa: E402


async def _trees():
    out = {}
    async with async_session_maker() as db:
        for k, s in S.items():
            roots = {"reports": [s["report"]]} if "artifact" not in s or k in ("S7", "S8") else {"artifacts": [s["artifact"]]}
            plan = await purge.closure(db, roots)
            out[k] = {t: sorted(ids) for t, ids in plan.ids.items() if ids}
    return out


async def _orphans():
    async with async_session_maker() as db:
        return await orphans(db)


async def _alive(tree):
    md = purge._metadata()
    from sqlalchemy import func, select
    alive = {}
    async with async_session_maker() as db:
        for t, ids in tree.items():
            col = md.tables[t].c.id
            alive[t] = (await db.execute(select(func.count()).select_from(md.tables[t]).where(col.in_(ids))
                                         .execution_options(include_rewound=True))).scalar()
    return alive


trees = asyncio.run(_trees())
for k in ("S1", "S8"):
    counts = {t: len(v) for t, v in trees[k].items()}
    print(f"{k} tree before: {json.dumps(counts, sort_keys=True)}", flush=True)
run_tables = {"completions", "agent_executions", "tool_executions", "plan_decisions", "completion_blocks"}
check(run_tables <= set(trees["S1"]), "real agent runs wrote the run tree", sorted(run_tables & set(trees["S1"])))
check(asyncio.run(_orphans()) == {}, "no dangling references before the run")
usage_before = sql("SELECT COUNT(*) FROM llm_usage_records WHERE organization_id=?", (org,))[0][0]
file_paths = {fid: (sql("SELECT path FROM files WHERE id=?", (fid,)) or [[None]])[0][0]
              for fid in (own_file, shared_file, old_upload)}
check(all(p and os.path.isfile(p) for p in file_paths.values()), "uploads are on disk before the run")

# -- settings, preview, run ---------------------------------------------------

r = c.put("/api/organization/settings", headers=H, json={"config": {
    "report_retention_days": {"value": a.days}, "artifact_retention_days": {"value": a.days},
    "file_retention_days": {"value": a.days}}})
check(r.status_code == 200 and r.json()["config"]["report_retention_days"]["value"] == a.days,
      "retention set through the API", {k: r.json()["config"][k]["value"] for k in
                                         ("report_retention_days", "artifact_retention_days", "file_retention_days")})
preview = c.get("/api/organization/retention/preview", headers=H).json()
expected_reports = sum(1 for s in S.values() if s["expect"] == "deleted")
print("preview:", json.dumps(preview), flush=True)
check(preview["reports"] == expected_reports, "preview counts the reports tonight's run deletes",
      f"{preview['reports']} == {expected_reports}")

from app.services.file_retention_service import apply_file_retention  # noqa: E402
from app.services.retention.jobs import apply_retention  # noqa: E402

first = asyncio.run(apply_retention())
first_files = asyncio.run(apply_file_retention())
print("nightly run:", json.dumps(first), "| file retention:", first_files, flush=True)

# -- verify -------------------------------------------------------------------

def api_sees(rid):
    return c.get(f"/api/reports/{rid}", headers=H).status_code == 200


for k, s in S.items():
    alive = asyncio.run(_alive(trees[k]))
    rows_left = sum(alive.values())
    rows_before = sum(len(v) for v in trees[k].values())
    if s["expect"] == "deleted":
        check(rows_left == 0 and not api_sees(s["report"]), f"{k} {s['label']}: deleted with its whole tree",
              f"{rows_before} rows in {len(trees[k])} tables -> {rows_left}")
    elif s["expect"] == "kept":
        check(rows_left == rows_before and api_sees(s["report"]), f"{k} {s['label']}: kept whole",
              f"{rows_before} rows -> {rows_left}")
    elif s["expect"] == "artifact deleted":
        check(rows_left == 0 and api_sees(s["report"]), f"{k} {s['label']}: dashboard deleted, conversation kept",
              f"{rows_before} rows -> {rows_left}")
    elif s["expect"] == "artifact kept":
        check(rows_left == rows_before, f"{k} {s['label']}: kept", f"{rows_before} rows -> {rows_left}")

if S["S10"].get("instruction"):
    row = sql("SELECT id, agent_execution_id FROM instructions WHERE id=?", (S["S10"]["instruction"],))
    check(row and row[0][1] is None, "S10 the instruction stays, its link to the run cleared", row)
rew = sql("SELECT COUNT(*) FROM completions WHERE id=?", (S["S9"]["rewound"],))[0][0]
check(rew == 0, "S9 the rewound (hidden) turn is deleted too")

files_now = {f["id"] for f in c.get("/api/files", headers=H).json()}
check(own_file not in files_now and not os.path.exists(file_paths[own_file]),
      "S1's own upload: row and bytes gone", file_paths[own_file])
check(shared_file in files_now and os.path.isfile(file_paths[shared_file]),
      "S2 still uses S1's second upload: kept")
check(old_upload not in files_now and not os.path.exists(file_paths[old_upload]),
      "S16 old upload deleted by file retention, bytes gone")
check(asyncio.run(_orphans()) == {}, "no dangling reference anywhere after the run")
usage_after = sql("SELECT COUNT(*) FROM llm_usage_records WHERE organization_id=?", (org,))[0][0]
check(usage_after == usage_before, "LLM usage history kept", f"{usage_before} -> {usage_after} records")
audit = sql("SELECT action, details FROM audit_logs WHERE organization_id=? AND action LIKE 'retention.%' ORDER BY created_at", (org,))
check(any(x[0] == "retention.reports_deleted" for x in audit) and any(x[0] == "retention.artifacts_deleted" for x in audit),
      "deletions are in the audit log (counts only)", [(x[0], json.loads(x[1]).get("reports", json.loads(x[1]).get("artifacts"))) for x in audit])

second = asyncio.run(apply_retention())
check(second["reports"] == 0 and second["artifacts"] == 0, "a second run deletes nothing", second)
preview2 = c.get("/api/organization/retention/preview", headers=H).json()
check(preview2["reports"] == 0 and preview2["artifacts"] == 0, "preview is empty after the run", preview2)

passed = sum(1 for ok, _, _ in results if ok)
print(f"{passed}/{len(results)} passed", flush=True)
if a.out:
    json.dump({"scenarios": S, "preview": preview, "run": first, "second_run": second,
               "results": [{"ok": ok, "label": l, "detail": d} for ok, l, d in results]},
              open(a.out, "w"), indent=2, default=str)
sys.exit(0 if passed == len(results) else 1)
