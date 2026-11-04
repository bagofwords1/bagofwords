"""C: DB write lock held longer than the tool-audit retry ladder (busy_timeout 30 s x 4 attempts) -> spill file,
replayed by the backend's 60 s scheduled job (same spill dir), then exported. Observed at the collector."""
import sys, os, json, time, sqlite3, asyncio, threading, uuid, glob; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from datetime import datetime, timezone
import main
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.user import User
from app.models.organization import Organization
from app.ee.audit.tool_audit import log_tool_audit, drain_tool_audit_queue, get_tool_audit_queue_stats, _spill_dir, stop_tool_audit_worker
DBP = os.environ.get("AUDIT_LOOP_DB", "/home/user/bagofwords/backend/db/audit_loop.db")
LOCK_S = 135
def hold_lock(seconds, started):
    con = sqlite3.connect(DBP, timeout=60, isolation_level=None)
    con.execute("PRAGMA busy_timeout=60000"); con.execute("BEGIN IMMEDIATE"); con.execute("update audit_export_state set last_seq=last_seq where 0")
    started.set(); time.sleep(seconds); con.execute("ROLLBACK"); con.close()
async def run():
    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.email == ADMIN["email"]))).scalar_one(); org = await db.get(Organization, ORG)
    ctx = {"user": user, "organization": org, "agent_execution_id": str(uuid.uuid4()), "mode": "chat"}
    started = threading.Event(); t = threading.Thread(target=hold_lock, args=(LOCK_S, started)); t.start(); started.wait()
    marks = {"C_occurred": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "lock_seconds": LOCK_S}
    await log_tool_audit(ctx, "tool.create_data.executed", "report", "audit-loop-report-C", {"tag": "C-spilled", "executed_queries": ["SELECT 2"]})
    t0 = time.time()
    while time.time() - t0 < LOCK_S + 10:
        await asyncio.sleep(2)
        if glob.glob(os.path.join(_spill_dir(), "*.jsonl")): break
    marks["spill_seen_after_s"] = round(time.time() - t0, 1); marks["spill_files"] = glob.glob(os.path.join(_spill_dir(), "*.jsonl")); marks["queue_stats"] = get_tool_audit_queue_stats()
    if marks["spill_files"]:
        first = json.loads(open(marks["spill_files"][0]).readline()); marks["spill_event"] = {"id": first["id"], "occurred_at": first["occurred_at"]}
    await stop_tool_audit_worker(timeout=1, drain=False)   # this process exits; the backend must replay
    t.join(); return marks
marks = asyncio.run(run()); print("marks:", json.dumps(marks, indent=1, default=str), flush=True)
eid = (marks.get("spill_event") or {}).get("id")
t0 = time.time(); found = None
while time.time() - t0 < 200 and eid:
    ev = [e for e in events("tool.create_data", 0) if not e["dup"] and e["event"]["id"] == eid]
    if ev: found = ev[0]; break
    time.sleep(3)
print("spill files now:", glob.glob(os.path.join(_spill_dir(), "*")))
print("backend log replay lines:", os.popen(f"grep -n 'Replayed\\|spill\\|Spill' {S}/backend.log | tail -5").read())
if found:
    x = found["event"]; print(f"REPLAYED EVENT at collector: id={x['id']} occurred_at(envelope)={x['occurred_at']} metadata.occurred_at={x['metadata'].get('occurred_at')} received={found['received_at']} original={marks['C_occurred']}")
else:
    print("replayed event NOT at collector within 200 s")
json.dump({"marks": marks, "found": found}, open(os.path.join(S, "06c_result.json"), "w"), indent=1, default=str)
