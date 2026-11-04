"""Health monitoring as seen by an operator and by the collector.
 a) silent producer: nothing happens for a while -> what does the stream status say? (same as idle?)
 b) sustained retryable failure: collector answers 503 to every delivery for ~2 min -> backoff, state, alerts?
 c) invalid credentials: collector answers 401 -> state 'invalid', audit_stream.state_changed, admin email attempt
 d) recovery: clear faults, resume -> every event the collector missed arrives exactly once (dedupe by id)"""
import sys, os, json, time, sqlite3; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
H = hdr(ADMIN["token"], ua="audit-loop/health")
def streams(): return {s["name"]: s for s in api.get("/api/enterprise/audit/streams", headers=H).json()}
def brief(s): return {k: s.get(k) for k in ("state", "consecutive_failures", "next_attempt_at", "last_error", "last_delivered_at", "delivered_count", "status")}
res = {}
# a) silent producer
time.sleep(12)
res["a_idle_status"] = {n: brief(s) for n, s in streams().items()}
print("a) idle/silent producer status:", json.dumps(res["a_idle_status"], default=str))
# b) sustained 503s: keep producing real events meanwhile (api key create/delete), observe backoff
coll.post("/_control/fault", json={"mode": "503", "count": 100000})
ids = []
timeline = []
t0 = time.time()
while time.time() - t0 < 120:
    k = api.post("/api/api_keys", headers=H, json={"name": f"during-outage-{int(time.time()-t0)}"}).json(); ids.append(k["id"])
    st = streams()["collector-webhook"]; timeline.append((round(time.time()-t0), st["consecutive_failures"], st["next_attempt_at"], st["state"], st["status"]))
    time.sleep(10)
res["b_timeline"] = timeline
print("b) sustained 503 timeline (t, failures, next_attempt_at, state, pending):"); [print("   ", x) for x in timeline]
con = sqlite3.connect(os.environ.get("AUDIT_LOOP_DB", "/home/user/bagofwords/backend/db/audit_loop.db"))
res["b_state_changed_events"] = con.execute("select count(*) from audit_logs where action='audit_stream.state_changed'").fetchone()[0]
print("   audit_stream.state_changed rows during 503 phase:", res["b_state_changed_events"])
# c) invalid credentials
coll.post("/_control/fault", json={"mode": "401", "count": 100000})
# force the next attempt now by resetting backoff: pause/resume bumps nothing; instead patch next_attempt_at via API? not exposed -> wait for backoff (<= 20 s at this failure count)
t1 = time.time(); seen = None
while time.time() - t1 < 150:
    st = streams()
    if any(s["state"] in ("invalid", "error") for s in st.values()): seen = {n: brief(s) for n, s in st.items()}; break
    time.sleep(5)
res["c_after_401"] = seen; res["c_seconds"] = round(time.time() - t1)
print("c) after 401s:", json.dumps(seen, default=str), "after", res["c_seconds"], "s")
res["c_state_changed_rows"] = [r for r in con.execute("select action, details, created_at from audit_logs where action='audit_stream.state_changed'")]
print("   state_changed rows:", res["c_state_changed_rows"])
print("   admin email attempt in backend log:", os.popen(f"grep -c 'admin notification\\|stopped (invalid)\\|Audit log stream' {S}/backend.log").read().strip())
# d) recovery
coll.delete("/_control/fault")
base_unique = stats()["unique_events"]
for n, s in streams().items():
    if s["state"] != "active": api.patch(f"/api/enterprise/audit/streams/{s['id']}", headers=H, json={"state": "active"})
t2 = time.time()
while time.time() - t2 < 90:
    st = streams()
    if all(s["status"]["pending"] == 0 for s in st.values()) and time.time() - t2 > 10: break
    time.sleep(5)
st = stats()
got = [e["event"] for e in events("api_key.created", 0) if not e["dup"]]
names = {e["metadata"].get("name") for e in got}
res["d_recovered"] = {"outage_keys_created": len(ids), "outage_keys_at_collector": sum(1 for i in range(len(ids)) if any(n == f"during-outage-{t}" for n in names for t in [None]) or True), "duplicates_total": st["duplicates"], "unique_total": st["unique_events"], "streams": {n: brief(s) for n, s in st_.items()} if (st_ := streams()) else None}
outage_names = {k["name"] for k in api.get("/api/api_keys", headers=H).json() if k["name"].startswith("during-outage-")}
res["d_recovered"]["outage_keys_at_collector"] = len(outage_names & names)
print("d) recovery:", json.dumps(res["d_recovered"], default=str))
json.dump(res, open(os.path.join(S, "09_result.json"), "w"), indent=1, default=str)
