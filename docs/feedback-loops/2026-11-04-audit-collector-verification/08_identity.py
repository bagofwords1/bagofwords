"""Historical identity: is the actor identity delivered to the collector the identity at event time,
or whatever the live users table says at export time?
 1. pause the webhook stream; member performs an audited action; member changes their email; resume
 2. pause again; member performs an action; the user row is deleted (account deletion); resume
Also: can a second organization be created (isolation test precondition)?"""
import sys, os, json, time, sqlite3; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
DBP = os.environ.get("AUDIT_LOOP_DB", "/home/user/bagofwords/backend/db/audit_loop.db")
pw = json.load(open(os.path.join(S, "03_result.json")))["member_password"]
H = hdr(ADMIN["token"])
streams = {s["name"]: s["id"] for s in api.get("/api/enterprise/audit/streams", headers=H).json()}
def set_state(name, state):
    r = api.patch(f"/api/enterprise/audit/streams/{streams[name]}", headers=H, json={"state": state}); assert r.status_code == 200, r.text
res = {}
# second org?
r = api.post("/api/organizations", headers=H, json={"name": "Second Org"}); res["create_second_org"] = (r.status_code, r.text[:120])
# ---- 1. email change between event and export
for n in streams: set_state(n, "inactive")
tok = login(MEMBER["email"], pw)
mh = {"Authorization": f"Bearer {tok}", "X-Organization-Id": ORG, "User-Agent": "audit-loop/identity"}
k = api.post("/api/api_keys", headers=mh, json={"name": "member-key-before-rename"}); res["member_apikey_1"] = k.status_code
new_email = "renamed-member@example.com"
r = api.patch("/api/users/me", headers=mh, json={"email": new_email}); res["email_change"] = (r.status_code, r.text[:120])
time.sleep(1)
base = stats()["deliveries"]
for n in streams: set_state(n, "active")
got = wait_action("api_key.created", n=1, timeout=40, since=base) or []
mine = [e["event"] for e in got if e["event"]["metadata"].get("name") == "member-key-before-rename"]
res["event_after_rename"] = [{"actor": e["actor"], "occurred_at": e["occurred_at"]} for e in mine]
print("1) email changed after the event, before export ->", json.dumps(res["event_after_rename"]))
# ---- 2. account deletion between event and export
for n in streams: set_state(n, "inactive")
tok = login(new_email if res["email_change"][0] == 200 else MEMBER["email"], pw)
mh["Authorization"] = f"Bearer {tok}"
k = api.post("/api/api_keys", headers=mh, json={"name": "member-key-before-delete"}); res["member_apikey_2"] = k.status_code
me = api.get("/api/users/me", headers=mh).json(); uid = me["id"]
con = sqlite3.connect(DBP); con.execute("PRAGMA foreign_keys=ON")
before = con.execute("select count(*), sum(user_id is null) from audit_logs where organization_id=?", (ORG,)).fetchone()
con.execute("delete from memberships where user_id=?", (uid,)); con.execute("delete from api_keys where user_id=?", (uid,))
try:
    con.execute("delete from users where id=?", (uid,)); con.commit(); res["user_delete"] = "ok"
except Exception as e:
    con.rollback(); res["user_delete"] = f"failed: {e}"
after = con.execute("select count(*), sum(user_id is null) from audit_logs where organization_id=?", (ORG,)).fetchone()
res["audit_rows_before_after_delete"] = {"before": before, "after": after}
con.close()
base = stats()["deliveries"]
for n in streams: set_state(n, "active")
got = wait_action("api_key.created", n=1, timeout=40, since=base) or []
mine = [e["event"] for e in got if e["event"]["metadata"].get("name") == "member-key-before-delete"]
res["event_after_delete"] = [{"actor": e["actor"], "occurred_at": e["occurred_at"]} for e in mine]
print("2) user deleted after the event, before export ->", json.dumps(res["event_after_delete"]))
print("rows before/after delete (count, null-user rows):", res["audit_rows_before_after_delete"])
print(json.dumps({k: v for k, v in res.items() if k.startswith(("create_second", "email_change", "user_delete", "member_apikey"))}))
json.dump(res, open(os.path.join(S, "08_result.json"), "w"), indent=1, default=str)
