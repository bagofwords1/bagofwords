"""WP2 (pentest 2026-10) HTTP probe against a running stack. Usage:
    cd backend && .venv/bin/python ../tools/agent/verify_wp2_viewer_authority.py db/agent.db tests/config/chinook.sqlite /tmp/wp2_ids.json
Seed first with tools/agent/seed_org.py
with --invite viewer@ / manager@ / owner@example.com). Each run creates fresh
agents, so it is repeatable. Prints one line per check."""
import sqlite3, sys, uuid, json
import httpx

BASE = "http://localhost:8000"
DB = sys.argv[1]
CHINOOK = sys.argv[2]
c = httpx.Client(base_url=BASE, timeout=120)


def login(e):
    r = c.post("/api/auth/jwt/login", data={"username": e, "password": "Password123!"})
    r.raise_for_status()
    return r.json()["access_token"]


tok = {k: login(f"{k}@example.com") for k in ("admin", "viewer", "manager", "owner")}
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok['admin']}"}).json()[0]["id"]
H = {k: {"Authorization": f"Bearer {v}", "X-Organization-Id": org} for k, v in tok.items()}
db = sqlite3.connect(DB)
uid = lambda e: db.execute("select id from users where email=?", (e,)).fetchone()[0]


def mk_ds(name):
    r = c.post("/api/data_sources", headers=H["admin"], json={
        "name": name, "type": "sqlite", "config": {"database": CHINOOK}, "credentials": {},
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False})
    r.raise_for_status()
    ds = r.json()
    assert c.put(f"/api/data_sources/{ds['id']}", headers=H["admin"], json={"is_public": True}).status_code == 200
    return ds["id"]


sfx = uuid.uuid4().hex[:6]
a, b = mk_ds(f"wp2-a-{sfx}"), mk_ds(f"wp2-b-{sfx}")
r = c.post(f"/api/organizations/{org}/resource-grants", headers=H["admin"], json={
    "resource_type": "data_source", "resource_id": a, "principal_type": "user",
    "principal_id": uid("manager@example.com"), "permissions": ["manage"]})
assert r.status_code == 200, r.text
c.get(f"/api/data_sources/{a}/refresh_schema", headers=H["admin"]).raise_for_status()

out = []
def rec(name, status, extra=""):
    out.append(f"{name:<58} {status} {extra}")

# 1. Entity scope
def owner_entity(ds_ids):
    r = c.post("/api/entities", headers=H["admin"], json={
        "type": "model", "title": "draft", "slug": f"d-{uuid.uuid4().hex[:6]}",
        "code": "select 1", "data": {}, "status": "draft", "data_source_ids": ds_ids})
    r.raise_for_status()
    eid = r.json()["id"]
    # Seeding shortcut: no API hands an entity to another owner as a draft.
    db.execute("update entities set owner_id=?, global_status='suggested' where id=?",
               (uid("owner@example.com"), eid)); db.commit()
    return eid
scope = lambda eid: sorted(x[0] for x in db.execute(
    "select data_source_id from entity_data_source_association where entity_id=?", (eid,)))

e = owner_entity([a])
r = c.put(f"/api/entities/{e}", headers=H["owner"], json={"data_source_ids": []})
rec("ATTACK owner PUT entity data_source_ids=[]", r.status_code, f"scope_after={'unchanged' if scope(e)==[a] else 'ORG-WIDE'}")
e2 = owner_entity([a, b])
r = c.put(f"/api/entities/{e2}", headers=H["owner"], json={"title": "renamed", "data_source_ids": [b]})
rec("REGRESSION owner edits title + narrows scope to [B]", r.status_code, f"scope_ok={scope(e2)==[b]}")
r = c.put(f"/api/entities/{e}", headers=H["admin"], json={"data_source_ids": []})
rec("REGRESSION admin makes entity org-wide", r.status_code, f"org_wide={scope(e)==[]}")

# 2. test_connection
def set_inactive():
    db.execute("update data_sources set is_active=0 where id=?", (a,))
    db.execute("update connections set is_active=0 where id in (select connection_id from domain_connection where data_source_id=?)", (a,))
    db.commit()
def state():
    ds = db.execute("select is_active from data_sources where id=?", (a,)).fetchone()[0]
    cs = [x[0] for x in db.execute("select is_active from connections where id in (select connection_id from domain_connection where data_source_id=?)", (a,))]
    return f"ds.is_active={ds} conn.is_active={cs}"
set_inactive()
r = c.get(f"/api/data_sources/{a}/test_connection", headers=H["viewer"])
rec("ATTACK viewer test_connection (state seeded inactive)", r.status_code, f"success={r.json().get('success')} {state()}")
set_inactive()
r = c.get(f"/api/data_sources/{a}/test_connection", headers=H["manager"])
rec("REGRESSION manager test_connection", r.status_code, f"success={r.json().get('success')} {state()}")

# 3. config on GET
for who in ("viewer", "manager", "admin"):
    r = c.get(f"/api/data_sources/{a}", headers=H[who])
    j = r.json()
    cfgs = [bool(x.get("config")) for x in j.get("connections", [])]
    rec(f"{'ATTACK' if who=='viewer' else 'REGRESSION'} {who} GET /data_sources/{{id}}", r.status_code,
        f"connections={len(cfgs)} has_conn_config={cfgs} top_config={bool(j.get('config'))}")

# 4. hidden columns
p = c.get(f"/api/data_sources/{a}/full_schema", headers=H["admin"], params={"page": 1, "page_size": 500}).json()
t = next(x for x in p["tables"] if len(x["columns"]) > 1)
hidden = t["columns"][0]["name"]
c.put(f"/api/data_sources/{a}/update_tables_status", headers=H["admin"],
      json={"activate": [t["id"]], "excluded_columns": {t["id"]: [hidden]}}).raise_for_status()
for who in ("viewer", "manager"):
    p = c.get(f"/api/data_sources/{a}/full_schema", headers=H[who], params={"page": 1, "page_size": 500})
    tt = next(x for x in p.json()["tables"] if x["id"] == t["id"])
    rec(f"{'ATTACK' if who=='viewer' else 'REGRESSION'} {who} paginated full_schema", p.status_code,
        f"hidden_col_present={hidden in [x['name'] for x in tt['columns']]}")
    l = c.get(f"/api/data_sources/{a}/full_schema", headers=H[who])
    tl = next(x for x in l.json() if x["name"] == t["name"])
    rec(f"  {who} non-paginated full_schema", l.status_code,
        f"hidden_col_present={hidden in [x['name'] for x in tl['columns']]}")

print("\n".join(out))
json.dump({"ds_a": a, "ds_b": b, "org": org}, open(sys.argv[3], "w"))
