import sys, os, json, time, sqlite3; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
pw = json.load(open(os.path.join(S, "03_result.json")))["member_password"]
tok = login(MEMBER["email"], pw)
UA = "audit-loop/authz-denials"
mh = {"Authorization": f"Bearer {tok}", "X-Organization-Id": ORG, "User-Agent": UA, "X-Forwarded-For": "203.0.113.7"}
base = stats()["deliveries"]; out = {}
DS = SEED['demo_data_source']['data_source_id']
out["member_streams_list"] = api.get("/api/enterprise/audit/streams", headers=mh).status_code
out["member_audit_list"] = api.get("/api/enterprise/audit", headers=mh).status_code
out["member_delete_ds"] = api.delete(f"/api/data_sources/{DS}", headers=mh).status_code
out["member_create_stream"] = api.post("/api/enterprise/audit/streams", headers=mh, json={"name":"x","destination":"https","config":{"url":"http://127.0.0.1:1/x"},"secrets":{}}).status_code
out["member_wrong_org"] = api.get(f"/api/data_sources/{DS}", headers={**mh, "X-Organization-Id": "00000000-0000-0000-0000-000000000000"}).status_code
out["member_invite"] = api.post("/api/organizations/members", headers=mh, json={"email":"zz@example.com","role":"member"}).status_code
print("HTTP:", out)
got = wait_action("access.denied", n=1, timeout=30, since=base) or []
time.sleep(6)
got = [e for e in events("access.denied", since=base) if not e["dup"]]
print(f"\naccess.denied events at collector: {len(got)}")
for e in got:
    ev = e["event"]; print("  ", json.dumps({"actor": ev["actor"], "context": ev["context"], "targets": ev["targets"], "metadata": ev["metadata"], "occurred_at": ev["occurred_at"]}))
json.dump({"http": out, "events": [e["event"] for e in got]}, open(os.path.join(S, "04_result.json"), "w"), indent=1, default=str)
