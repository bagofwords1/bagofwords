"""Tool/agent audit evidence through the helper every AI tool calls (app.ee.audit.tool_audit.log_tool_audit),
with real User/Organization rows and the real queue worker, against the live DB the backend exports from.
 A: 12 executed queries of 800 chars  -> what reaches the collector?
 B: write delayed 8 s by a DB lock     -> which time does the collector see as occurred_at?
 C: DB locked longer than the retries  -> spill file, replayed by the *backend's* scheduled job, then exported"""
import sys, os, json, time, sqlite3, asyncio, threading, uuid, glob; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from datetime import datetime, timezone
import main
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.user import User
from app.models.organization import Organization
from app.ee.audit.tool_audit import log_tool_audit, _truncate_queries, drain_tool_audit_queue, get_tool_audit_queue_stats, _spill_dir
DBP = os.environ.get("AUDIT_LOOP_DB", "/home/user/bagofwords/backend/db/audit_loop.db")

def hold_lock(seconds, started):
    con = sqlite3.connect(DBP, timeout=30, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000"); con.execute("BEGIN IMMEDIATE"); con.execute("update audit_export_state set last_seq=last_seq where 0")
    started.set(); time.sleep(seconds); con.execute("ROLLBACK"); con.close()

async def run():
    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.email == ADMIN["email"]))).scalar_one()
        org = await db.get(Organization, ORG)
    exec_id = str(uuid.uuid4())
    ctx = {"user": user, "organization": org, "agent_execution_id": exec_id, "mode": "chat"}
    marks = {}
    # A: 12 long queries, exactly as create_data.py passes them (via _truncate_queries)
    queries = [f"SELECT /* q{i:02d} */ " + ", ".join(f"col_{i}_{j} AS c{j}" for j in range(60)) + f" FROM invoices_{i} WHERE total > {i}" for i in range(12)]
    assert all(len(q) > 500 for q in queries)
    await log_tool_audit(ctx, "tool.create_data.executed", "report", "audit-loop-report-A", {"tag": "A-truncation", "executed_queries": _truncate_queries(queries), "query_count": len(queries)})
    await drain_tool_audit_queue(10)
    # B: enqueue during an 8 s lock -> write lands late
    started = threading.Event(); t = threading.Thread(target=hold_lock, args=(8, started)); t.start(); started.wait()
    marks["B_occurred"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    await log_tool_audit(ctx, "tool.create_data.executed", "report", "audit-loop-report-B", {"tag": "B-late-write", "executed_queries": ["SELECT 1"]})
    t.join(); await drain_tool_audit_queue(30)
    marks["B_written"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    # C: lock longer than the retry ladder (0.5+1+2 s + 4 attempts) -> spill
    started = threading.Event(); t = threading.Thread(target=hold_lock, args=(14, started)); t.start(); started.wait()
    marks["C_occurred"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    await log_tool_audit(ctx, "tool.create_data.executed", "report", "audit-loop-report-C", {"tag": "C-spilled", "executed_queries": ["SELECT 2"]})
    for _ in range(30):
        await asyncio.sleep(1)
        if glob.glob(os.path.join(_spill_dir(), "*.jsonl")): break
    spills = glob.glob(os.path.join(_spill_dir(), "*.jsonl"))
    marks["C_spill_files"] = spills; marks["queue_stats"] = get_tool_audit_queue_stats()
    if spills: marks["C_spill_event_id"] = json.loads(open(spills[0]).readline())["id"]
    t.join()
    return marks

marks = asyncio.run(run())
print("marks:", json.dumps(marks, indent=1, default=str))
# wait for the backend's spill replay job (60 s) + export ticks
t0 = time.time()
while time.time() - t0 < 150:
    ev = [e for e in events("tool.create_data", 0) if not e["dup"]]
    if len(ev) >= 3 or (not glob.glob(os.path.join(_spill_dir(), "*.jsonl")) and len(ev) >= 2 and time.time() - t0 > 75): break
    time.sleep(3)
ev = [e for e in events("tool.create_data", 0) if not e["dup"]]
con = sqlite3.connect(DBP); con.row_factory = sqlite3.Row
dbrows = {r["id"]: dict(r) for r in con.execute("select id, created_at, details from audit_logs where action='tool.create_data.executed'")}
print(f"\ncollector tool events: {len(ev)}  spill files left: {glob.glob(os.path.join(_spill_dir(), '*'))}")
for e in ev:
    x = e["event"]; m = x["metadata"]; tag = m.get("tag")
    print(f"\n[{tag}] id={x['id']} actor={x['actor']} occurred_at(envelope)={x['occurred_at']} received={e['received_at']} metadata.occurred_at={m.get('occurred_at')} db.created_at={dbrows.get(x['id'],{}).get('created_at')}")
    if tag == "A-truncation":
        qs = m["executed_queries"]; print(f"   queries delivered={len(qs)} of query_count={m.get('query_count')}; lengths={[len(q) for q in qs]}; last query ends with: ...{qs[-1][-30:]!r}")
json.dump({"marks": marks, "events": [e for e in ev]}, open(os.path.join(S, "06_result.json"), "w"), indent=1, default=str)
