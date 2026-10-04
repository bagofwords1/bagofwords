#!/usr/bin/env python3
"""Live Entra-provisioning loop for SCIM Groups against a running backend.

Drives a real stack over HTTP with the Entra provisioning-engine stand-in
(backend/tests/mocks/entra_scim_provisioner.py) and checks every step at three
layers: what Entra sees (SCIM responses), what BOW users can do (permissions in
/api/users/whoami, through a role the admin assigned to the IdP group), and
what is stored (rows in groups / group_memberships / role_assignments /
report_shares, read straight from the database).

Run from backend/ after booting the backend with an enterprise license
(see docs/feedback-loops/scim-groups.md):

    uv run python ../tools/agent/verify_scim_groups_entra.py \\
        --database-url postgresql://postgres@127.0.0.1:5433/bow_live \\
        --wire-log /tmp/entra-wire.json

Exit code 0 only when every check passes. Creates a fresh admin/org per run.
"""
import argparse
import json
import pathlib
import sys
import uuid

import httpx

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from tests.mocks.entra_scim_provisioner import EntraProvisioningApp, EntraTenant  # noqa: E402

PASSWORD = "Password123!"  # same default as tools/agent/seed_org.py
results = []


def check(label, condition, detail=""):
    results.append(bool(condition))
    print(("PASS" if condition else "FAIL"), "-", label, "" if condition else f"  >> {detail}", flush=True)


def hdr(token, org_id=None):
    h = {"Authorization": f"Bearer {token}"}
    if org_id:
        h["X-Organization-Id"] = org_id
    return h


def register_and_login(client, email, name, invite_token=None):
    body = {"name": name, "email": email, "password": PASSWORD}
    if invite_token:
        body["invite_token"] = invite_token
    r = client.post("/api/auth/register", json=body)
    # Re-runs reuse the admin: "already exists" / "Sign-up is disabled" fall through to login.
    if r.status_code not in (200, 201) and not (
        r.status_code == 400 and ("ALREADY_EXISTS" in r.text or "Sign-up is disabled" in r.text)
    ):
        sys.exit(f"register {email}: {r.status_code} {r.text}")
    r = client.post("/api/auth/jwt/login", data={"username": email, "password": PASSWORD})
    if r.status_code != 200:
        sys.exit(f"login {email}: {r.status_code} {r.text}")
    return r.json()["access_token"]


def perms(client, token, org_id):
    info = client.get("/api/users/whoami", headers=hdr(token)).json()
    return set(next(o for o in info["organizations"] if o["id"] == org_id)["permissions"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--database-url", required=True,
                    help="the backend's database (sqlalchemy URL): invite tokens + stored-row checks")
    ap.add_argument("--admin-email", default="admin@example.com",
                    help="registered on first run (first user gets an org), logged in afterwards")
    ap.add_argument("--legacy-patch", action="store_true")
    ap.add_argument("--wire-log")
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    client = httpx.Client(base_url=args.base_url, timeout=30)

    from sqlalchemy import create_engine, text
    engine = create_engine(args.database_url)

    def db(sql, **params):
        with engine.connect() as conn:
            return conn.execute(text(sql), params).fetchall()

    # ── BOW side: an admin, an org, a SCIM token, two existing members ────
    admin_token = register_and_login(client, args.admin_email, "Admin")
    orgs = client.get("/api/organizations", headers=hdr(admin_token)).json()
    if orgs:
        org_id = orgs[0]["id"]
    else:
        org_id = client.post("/api/organizations", json={"name": f"Entra Sim {run}"},
                             headers=hdr(admin_token)).json()["id"]
    r = client.post("/api/enterprise/scim/tokens", json={"name": "Entra"}, headers=hdr(admin_token, org_id))
    if r.status_code != 201:
        sys.exit(f"SCIM token: {r.status_code} {r.text} (is the backend running with an enterprise license?)")
    scim_token = r.json()["token"]

    members = {}
    for who in ("alice", "bob"):
        email = f"{who}_{run}@example.com"
        r = client.post(f"/api/organizations/{org_id}/members",
                        json={"organization_id": org_id, "email": email, "role": "member"},
                        headers=hdr(admin_token, org_id))
        if r.status_code != 200:
            sys.exit(f"invite {email}: {r.status_code} {r.text}")
        invite = db("select invite_token from memberships where email = :e and user_id is null "
                    "order by created_at desc limit 1", e=email)
        token = register_and_login(client, email, who.title(), invite[0][0] if invite else None)
        members[who] = {"email": email, "token": token,
                        "id": client.get("/api/users/whoami", headers=hdr(token)).json()["id"]}

    # ── Entra side: tenant + enterprise app ───────────────────────────────
    tenant = EntraTenant(domain=f"entra-{run}.example.com")
    d_alice = tenant.add_user("Alice", "Analyst", mail=members["alice"]["email"])
    d_bob = tenant.add_user("Bob", "Builder", mail=members["bob"]["email"])
    d_carol = tenant.add_user("Carol", "Chen")
    team = tenant.add_group(f"Data Team {run}", [d_alice, d_bob, d_carol])
    staff = tenant.add_group(f"All Staff {run}", [d_alice, d_bob, d_carol])
    app = EntraProvisioningApp(tenant, client, scim_token, legacy_patch=args.legacy_patch)

    def admin_groups():
        return {g["id"]: g for g in client.get(f"/api/organizations/{org_id}/groups",
                                               headers=hdr(admin_token, org_id)).json()}

    try:
        app.test_connection()
        check("Entra: test connection", True)
    except Exception as exc:  # noqa: BLE001
        check("Entra: test connection", False, exc)

    app.assign_group(team)
    app.assign_group(staff)
    rep = app.run_cycle()
    check("cycle 1 (initial): no provisioning errors", rep.ok, rep.errors)
    check("cycle 1: existing members adopted, Carol created",
          (rep.users_matched, rep.users_created) == (2, 1), rep)
    if team.object_id not in app.group_ids:
        print(f"\nEntra could not provision the group; stopping.\n{sum(results)}/{len(results)} checks passed")
        return 1
    team_id = app.group_ids[team.object_id]
    carol_id = app.user_ids[d_carol.object_id]
    g = admin_groups().get(team_id, {})
    check("cycle 1: BOW group carries the Entra name/objectId and is SCIM-owned",
          (g.get("name"), g.get("external_id"), g.get("external_provider"))
          == (team.display_name, team.object_id, "scim"), g)
    check("cycle 1: all three members landed",
          set(g.get("member_user_ids", [])) == {members["alice"]["id"], members["bob"]["id"], carol_id}, g)
    rows = db("select count(*) from group_memberships where group_id = :g", g=team_id)
    check("DB: group_memberships rows == 3", rows[0][0] == 3, rows)

    role = client.post(f"/api/organizations/{org_id}/roles",
                       json={"name": f"conn-mgr-{run}", "permissions": ["manage_connections"]},
                       headers=hdr(admin_token, org_id)).json()
    r = client.post(f"/api/organizations/{org_id}/role-assignments",
                    json={"role_id": role["id"], "principal_type": "group", "principal_id": team_id},
                    headers=hdr(admin_token, org_id))
    check("admin assigns a role to the IdP group", r.status_code == 200, r.text)
    check("Alice gains manage_connections through the group",
          "manage_connections" in perms(client, members["alice"]["token"], org_id))
    r = client.post(f"/api/organizations/{org_id}/groups/{team_id}/members",
                    json={"user_id": members["alice"]["id"]}, headers=hdr(admin_token, org_id))
    check("admin API refuses hand edits to the IdP group (409 group.managed_by_scim)",
          r.status_code == 409 and r.json().get("error_code") == "group.managed_by_scim", r.text)

    report = client.post("/api/reports", json={"title": f"Shared {run}", "files": [], "data_sources": []},
                         headers=hdr(admin_token, org_id)).json()
    r = client.put(f"/api/reports/{report['id']}/visibility/artifact",
                   json={"visibility": "shared", "shared_user_ids": [], "shared_group_ids": [team_id]},
                   headers=hdr(admin_token, org_id))
    check("report shared with the IdP group", r.status_code == 200, r.text)

    before = len(app.wire_log)
    rep = app.run_cycle()
    writes = [e["method"] for e in app.wire_log[before:] if e["method"] != "GET"]
    check("cycle 2 (no directory change): no writes", rep.ok and writes == [], (rep, writes))

    team.members.discard(d_alice.object_id)
    team.display_name = f"Data Platform {run}"
    rep = app.run_cycle()
    check("cycle 3 (Alice leaves, rename): no errors", rep.ok, rep.errors)
    g = admin_groups().get(team_id, {})
    check("cycle 3: rename applied", g.get("name") == team.display_name, g)
    check("cycle 3: Alice removed, others kept",
          set(g.get("member_user_ids", [])) == {members["bob"]["id"], carol_id}, g)
    check("cycle 3: Alice loses manage_connections",
          "manage_connections" not in perms(client, members["alice"]["token"], org_id))
    check("cycle 3: Bob keeps manage_connections",
          "manage_connections" in perms(client, members["bob"]["token"], org_id))

    app.unassign_group(team)
    rep = app.run_cycle()
    check("cycle 4 (unassign): group deleted without errors", rep.ok and rep.groups_deleted == 1, rep)
    check("cycle 4: group gone from BOW", team_id not in admin_groups())
    check("cycle 4: Bob loses manage_connections",
          "manage_connections" not in perms(client, members["bob"]["token"], org_id))
    leftovers = {
        "groups": db("select count(*) from groups where id = :g", g=team_id)[0][0],
        "group_memberships": db("select count(*) from group_memberships where group_id = :g", g=team_id)[0][0],
        "role_assignments": db("select count(*) from role_assignments where principal_type='group' "
                               "and principal_id = :g", g=team_id)[0][0],
        "report_shares": db("select count(*) from report_shares where group_id = :g", g=team_id)[0][0],
    }
    check("DB: nothing points at the deleted group", not any(leftovers.values()), leftovers)

    staff.members.discard(d_carol.object_id)
    rep = app.run_cycle()
    check("cycle 5 (Carol out of scope): soft-deleted", rep.ok and rep.users_disabled == 1, rep)
    carol = client.get(f"/scim/v2/Users/{carol_id}", headers=hdr(scim_token)).json()
    check("cycle 5: Carol active=false", carol.get("active") is False, carol)

    non_scim = [e for e in app.wire_log if e["response"] is not None
                and not (e["content_type"] or "").startswith("application/scim+json")]
    check("every SCIM response is application/scim+json", not non_scim, non_scim[:1])

    if args.wire_log:
        pathlib.Path(args.wire_log).write_text(json.dumps(app.wire_log, indent=2, default=str))
        print(f"wire log: {args.wire_log} ({len(app.wire_log)} exchanges)")
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
