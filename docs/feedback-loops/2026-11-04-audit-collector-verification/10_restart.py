"""Restart mid-send: 3,000 real audit rows written through audit_service.log (the function every route uses),
the backend is killed while streams are delivering, then restarted. At the collector: every DB row must arrive
exactly once after dedupe by envelope id; duplicates are counted (at-least-once is the documented contract)."""
import sys, os, json, time, sqlite3, asyncio, subprocess, signal; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import main
from app.dependencies import async_session_maker
from app.ee.audit.service import audit_service
DBP = os.environ.get("AUDIT_LOOP_DB", "/home/user/bagofwords/backend/db/audit_loop.db")
N = 20000
H = hdr(ADMIN["token"])
uid = api.get("/api/users/me", headers=H).json()["id"]
async def write():
    async with async_session_maker() as db:
        for i in range(N):
            await audit_service.log(db=db, organization_id=ORG, action="loop.restart_burst2", user_id=uid, resource_type="burst", resource_id=str(i), details={"i": i}, commit=False)
        await db.commit()
t0 = time.time(); asyncio.run(write()); print(f"wrote {N} rows in {time.time()-t0:.1f}s", flush=True)
before = stats()
# wait until delivery has started, then kill the backend process tree mid-stream
while True:
    st = stats()
    if any(a == "loop.restart_burst2" for a in st["actions"]): break
    time.sleep(0.05)
pids = os.popen("ps -eo pid,cmd | grep '[m]ain.py' | awk '{print $1}'").read().split()
print("delivery started; collector has", st["actions"].get("loop.restart_burst2"), "burst events; killing backend pids", pids, flush=True)
for p in pids: os.kill(int(p), signal.SIGKILL)
os.system("fuser -k 8000/tcp >/dev/null 2>&1")
time.sleep(2)
mid = stats(); print("at kill: unique burst events at collector =", mid["actions"].get("loop.restart_burst2"), flush=True)
env = dict(os.environ)
subprocess.Popen(["bash", "-c", f"cd /home/user/bagofwords/backend && source {S}/env.sh && exec uv run python main.py >> {S}/backend.log 2>&1"], env=env, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
t1 = time.time()
while time.time() - t1 < 120:
    try:
        if httpx.get(f"{BASE}/health", timeout=2).status_code == 200: break
    except Exception: pass
    time.sleep(2)
print(f"backend back after {time.time()-t1:.0f}s", flush=True)
t2 = time.time()
while time.time() - t2 < 420:
    st = stats()
    if st["actions"].get("loop.restart_burst2", 0) >= N:
        break
    time.sleep(5)
st = stats()
con = sqlite3.connect(DBP)
db_n = con.execute("select count(*) from audit_logs where action='loop.restart_burst'").fetchone()[0]
unstamped = con.execute("select count(*) from audit_logs where organization_id=? and export_seq is null", (ORG,)).fetchone()[0]
per_intake = {}
for line in open(os.path.join(S, "collector-state", "events.jsonl")):
    r = json.loads(line)
    if (r["event"] or {}).get("action") == "loop.restart_burst2":
        per_intake.setdefault(r["intake"], set()).add(r["event"]["id"])
res = {"db_rows": db_n, "unique_at_collector": st["actions"].get("loop.restart_burst2"), "per_intake_unique": {k: len(v) for k, v in per_intake.items()},
       "duplicates_added_during_scenario": st["duplicates"] - before["duplicates"], "rejected_deliveries_added": st["rejected_deliveries"] - before["rejected_deliveries"], "unstamped_rows_left": unstamped,
       "drain_seconds": round(time.time() - t2)}
print(json.dumps(res, indent=1))
json.dump(res, open(os.path.join(S, "10_result.json"), "w"), indent=1)
