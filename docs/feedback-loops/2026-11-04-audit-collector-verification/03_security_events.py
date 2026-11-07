"""S2 authentication lifecycle + S3 authorization denial, observed at the collector and in the DB."""
import sys, os, json, time, sqlite3; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
DB = os.environ.get("AUDIT_LOOP_DB", "/home/user/bagofwords/backend/db/audit_loop.db")
def db_rows(where):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f"select id, organization_id, user_id, action, resource_type, resource_id, details, ip_address, user_agent, created_at, export_seq from audit_logs where {where} order by created_at")]
    con.close(); return rows
base_delivery = stats()["deliveries"]
t_start = time.time()
UA = "audit-loop/security-scenarios"
out = {}
# --- S2: password login success / failure / logout / password change / forgot / oauth error
r = api.post("/api/auth/jwt/login", data={"username": MEMBER["email"], "password": MEMBER["password"]}, headers={"User-Agent": UA}); out["login_ok"] = r.status_code
tok = r.json()["access_token"]
r = api.post("/api/auth/jwt/login", data={"username": MEMBER["email"], "password": "wrong-password-xyz"}, headers={"User-Agent": UA}); out["login_bad"] = r.status_code
r = api.post("/api/auth/jwt/login", data={"username": "nobody@example.com", "password": "whatever"}, headers={"User-Agent": UA}); out["login_unknown_user"] = r.status_code
r = api.post("/api/auth/jwt/logout", headers={"Authorization": f"Bearer {tok}", "User-Agent": UA}); out["logout"] = r.status_code
tok = login(MEMBER["email"], MEMBER["password"])
r = api.patch("/api/users/me", headers={"Authorization": f"Bearer {tok}", "User-Agent": UA}, json={"password": "NewPassword456!"}); out["password_change"] = r.status_code
MEMBER_PW = "NewPassword456!" if r.status_code == 200 else MEMBER["password"]
r = api.post("/api/auth/forgot-password", json={"email": MEMBER["email"]}, headers={"User-Agent": UA}); out["forgot_password"] = r.status_code
r = api.post("/api/auth/reset-password", json={"token": "bogus-token", "password": "Another789!"}, headers={"User-Agent": UA}); out["reset_password_bad_token"] = r.status_code
r = api.get("/api/auth/google/callback", params={"error": "access_denied", "error_description": "synthetic provider error"}, headers={"User-Agent": UA}, follow_redirects=False); out["oauth_callback_error"] = r.status_code
r = api.get("/api/auth/google/callback", params={"code": "x", "state": "y"}, headers={"User-Agent": UA}, follow_redirects=False); out["oauth_callback_bad_state"] = r.status_code
# --- S3: authorization denials
mh = {"Authorization": f"Bearer {tok}", "X-Organization-Id": ORG, "User-Agent": UA}
r = api.get("/api/enterprise/audit/streams", headers=mh); out["member_streams_list"] = r.status_code
r = api.get("/api/enterprise/audit", headers=mh); out["member_audit_list"] = r.status_code
r = api.delete(f"/api/data_sources/{SEED['demo_data_source']['data_source_id']}", headers=mh); out["member_delete_ds"] = r.status_code
r = api.get(f"/api/data_sources/{SEED['demo_data_source']['data_source_id']}", headers={**mh, "X-Organization-Id": "00000000-0000-0000-0000-000000000000"}); out["member_wrong_org"] = r.status_code
r = api.get("/api/reports", headers={"User-Agent": UA}); out["anonymous_reports"] = r.status_code
print("HTTP outcomes:", json.dumps(out))
time.sleep(14)  # two export ticks
# DB view
rows = db_rows(f"created_at >= datetime('now', '-{int(time.time()-t_start)+5} seconds')")
print("\nDB rows written during the scenario:")
for r_ in rows:
    print(f"  org={r_['organization_id']} user={r_['user_id']} action={r_['action']} ip={r_['ip_address']} ua={r_['user_agent']} seq={r_['export_seq']} details={r_['details']}")
# collector view
got = [e for e in events(since=base_delivery) if not e["dup"]]
print("\nCollector received (new, unique):")
for e in got:
    ev = e["event"]; print(f"  {ev['action']} actor={ev['actor']} ctx={ev['context']} targets={ev['targets']} meta={json.dumps(ev['metadata'])[:200]}")
json.dump({"http": out, "db_rows": rows, "collector": [e["event"] for e in got], "member_password": MEMBER_PW}, open(os.path.join(S, "03_result.json"), "w"), default=str, indent=1)
