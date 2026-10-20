"""WP10 live probe: seeds two orgs through the real API, then prints one line
per check. Run against a fresh, migrated SQLite DB:

    cd backend && .venv/bin/python ../docs/feedback-loops/metadata-oracles/probe.py \
        http://127.0.0.1:8047 <db-file> <chinook.sqlite>

Eval runs are inserted straight into SQLite: results only come from executing
a live eval run (an LLM call), which this sandbox does not make.
"""
import io
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta

import httpx
from PIL import Image

BASE, DB, CHINOOK = sys.argv[1], sys.argv[2], sys.argv[3]
PW = "Password123!"
c = httpx.Client(base_url=BASE, timeout=60)


def h(tok, org=None):
    d = {"Authorization": f"Bearer {tok}"}
    if org:
        d["X-Organization-Id"] = org
    return d


def register(email, invite=None):
    body = {"name": email.split("@")[0], "email": email, "password": PW}
    if invite:
        body["invite_token"] = invite
    r = c.post("/api/auth/register", json=body)
    assert r.status_code in (201, 400), r.text
    r = c.post("/api/auth/jwt/login", data={"username": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def invite(admin, org, email):
    r = c.post(f"/api/organizations/{org}/members",
               json={"organization_id": org, "email": email, "role": "member"}, headers=h(admin, org))
    assert r.status_code == 200, r.text
    con = sqlite3.connect(DB)
    tok = con.execute("SELECT invite_token FROM memberships WHERE email=? AND user_id IS NULL "
                      "ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()[0]
    con.close()
    t = register(email, tok)
    me = c.get("/api/users/whoami", headers=h(t)).json()
    return t, me["id"]


def ds(admin, org, name):
    r = c.post("/api/data_sources", json={
        "name": name, "type": "sqlite", "config": {"database": CHINOOK}, "credentials": {},
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
    }, headers=h(admin, org))
    assert r.status_code == 200, r.text
    d = r.json()
    r = c.put(f"/api/data_sources/{d['id']}", json={"is_public": False}, headers=h(admin, org))
    assert r.status_code == 200, r.text
    return d


def grant(admin, org, ds_id, uid, perms):
    r = c.post(f"/api/organizations/{org}/resource-grants", json={
        "resource_type": "data_source", "resource_id": ds_id,
        "principal_type": "user", "principal_id": uid, "permissions": perms}, headers=h(admin, org))
    assert r.status_code == 200, r.text


def out(label, value):
    print(f"{label:<58} {value}")


tag = uuid.uuid4().hex[:4]
admin = register(f"admin_{tag}@example.com")
me = c.get("/api/users/whoami", headers=h(admin)).json()
org_a = me["organizations"][0]["id"] if me.get("organizations") else \
    c.post("/api/organizations", json={"name": "Org A"}, headers=h(admin)).json()["id"]
secret = ds(admin, org_a, "Payroll Secret Agent")
other = ds(admin, org_a, "Sales Agent")
member, member_id = invite(admin, org_a, f"member_{tag}@example.com")

# Org B with an outsider who manages instructions on one of B's agents.
org_b = c.post("/api/organizations", json={"name": f"Org B {tag}"}, headers=h(admin)).json()["id"]
outsider, outsider_id = invite(admin, org_b, f"outsider_{tag}@example.com")
b_ds = ds(admin, org_b, "Org B Agent")
grant(admin, org_b, b_ds["id"], outsider_id, ["manage_instructions"])

# ── 1. Rollback oracle ────────────────────────────────────────────────
for text, ids in (("payroll rule", [secret["id"]]), ("sales rule", [other["id"]])):
    r = c.post("/api/instructions", json={"text": text, "status": "published", "category": "general",
                                          "data_source_ids": ids}, headers=h(admin, org_a))
    assert r.status_code == 200, r.text
builds = c.get("/api/builds", params={"status": "all"}, headers=h(admin, org_a)).json()["items"]
main_a = c.get("/api/builds/main", headers=h(admin, org_a)).json()["id"]
older_a = next(b["id"] for b in builds if b["id"] != main_a and b["status"] == "approved")
for who, tok in (("outsider (org B agent manager)", outsider), ("admin of org B", admin)):
    f = c.post(f"/api/builds/{older_a}/rollback", headers=h(tok, org_b))
    m = c.post(f"/api/builds/{uuid.uuid4()}/rollback", headers=h(tok, org_b))
    out(f"rollback org-A build as {who}", f"{f.status_code} {f.json().get('detail')!r}")
    out(f"rollback random id as {who}", f"{m.status_code} {m.json().get('detail')!r}")
    out("  -> agent name leaked", "Payroll Secret Agent" in f.text)
r = c.post(f"/api/builds/{older_a}/rollback", headers=h(admin, org_a))
out("REGRESSION rollback in own org (admin A)", f"{r.status_code} new_main={r.json().get('is_main')}")

# ── 2. Eval baseline ─────────────────────────────────────────────────
grant(admin, org_a, other["id"], member_id, ["manage_evals"])
suite = c.post("/api/tests/suites", json={"name": "WP10"}, headers=h(admin, org_a)).json()
cases = {}
for name, d in (("sales-case", other), ("payroll-case", secret)):
    r = c.post(f"/api/tests/suites/{suite['id']}/cases", json={
        "name": name, "prompt_json": {"content": "q"},
        "expectations_json": {"spec_version": 1, "rules": [], "order_mode": "flexible"},
        "data_source_ids_json": [d["id"]]}, headers=h(admin, org_a))
    assert r.status_code == 200, r.text
    cases[name] = r.json()["id"]
rep = c.post("/api/reports", json={"title": "eval", "files": [], "data_sources": []}, headers=h(admin, org_a)).json()
t0 = datetime.utcnow() - timedelta(hours=3)
sys.path.insert(0, ".")
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
import main  # noqa: E402,F401  (registers every model)
from app.models.completion import Completion  # noqa: E402
from app.models.eval import TestRun, TestResult  # noqa: E402

engine = create_engine(f"sqlite:///{DB}")


def seed_run(title, statuses, at):
    with Session(engine) as s:
        run = TestRun(title=title, suite_ids=suite["id"], status="success", created_at=at)
        s.add(run)
        s.flush()
        for cid, st in statuses.items():
            head = Completion(prompt={"content": "q"}, completion={"content": ""}, role="user",
                              message_type="user_message", report_id=rep["id"])
            s.add(head)
            s.flush()
            s.add(TestResult(run_id=run.id, case_id=cid, head_completion_id=head.id, status=st))
        s.commit()
        return str(run.id)


visible = seed_run("visible baseline", {cases["sales-case"]: "fail"}, t0)
hidden = seed_run("HIDDEN payroll baseline", {cases["sales-case"]: "pass", cases["payroll-case"]: "fail"},
                  t0 + timedelta(minutes=30))
current = seed_run("current", {cases["sales-case"]: "pass"}, t0 + timedelta(hours=1))
engine.dispose()
for who, tok in (("member (manage_evals on Sales only)", member), ("REGRESSION admin", admin)):
    r = c.get(f"/api/tests/runs/{current}/compare", headers=h(tok, org_a))
    ar = (r.json().get("against_run") or {}) if r.status_code == 200 else {}
    out(f"compare baseline as {who}", f"{r.status_code} {ar.get('title')!r}")

# ── 3. Project default agents ────────────────────────────────────────
p = c.post("/api/projects", json={"name": "Shared project"}, headers=h(admin, org_a)).json()
c.put(f"/api/projects/{p['id']}", json={"access": "org"}, headers=h(admin, org_a))
r = c.put(f"/api/projects/{p['id']}/data_sources", json={"data_source_ids": [secret["id"], other["id"]]},
          headers=h(admin, org_a))
assert r.status_code == 200, r.text
for who, tok in (("member", member), ("REGRESSION admin", admin)):
    r = c.get(f"/api/projects/{p['id']}", headers=h(tok, org_a))
    out(f"GET project data_sources as {who}", f"{r.status_code} {sorted(d['name'] for d in r.json()['data_sources'])}")

# ── 4. Avatar lifecycle ──────────────────────────────────────────────


def png(color):
    b = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(b, format="PNG")
    return b.getvalue()


u1 = c.post("/api/users/me/avatar", files={"avatar": ("a.png", png("red"), "image/png")}, headers=h(member)).json()["image_url"]
u2 = c.post("/api/users/me/avatar", files={"avatar": ("b.png", png("blue"), "image/png")}, headers=h(member)).json()["image_url"]
out("avatar: GET replaced (old) url", c.get(u1).status_code)
out("REGRESSION avatar: GET current url", c.get(u2).status_code)
c.delete("/api/users/me/avatar", headers=h(member))
out("avatar: GET url after DELETE", c.get(u2).status_code)
u3 = c.post("/api/users/me/avatar", files={"avatar": ("c.png", png("green"), "image/png")}, headers=h(member)).json()["image_url"]
out("REGRESSION avatar: upload after delete, GET", c.get(u3).status_code)

json.dump({"org_a": org_a, "member_token": member, "admin_token": admin, "project_id": p["id"],
           "member_email": f"member_{tag}@example.com"}, open(DB + ".ids.json", "w"))
