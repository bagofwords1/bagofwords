"""End-to-end reproduction of the role-management / RBAC divergence bug.

Bag of Words keeps a user's role in TWO places that are written by different
code paths and can drift apart:

  * legacy  ``Membership.role``  ('admin' | 'member') — a plain string column
  * RBAC     ``role_assignments`` — the real source of truth for permission
             checks (resolved by ``permission_resolver``)

This script drives a live server and demonstrates the drift (and the login
lockout it causes) as concrete, asserted failures. It is designed to run RED
against the current code and GREEN once the stores are reconciled.

It runs in two phases against the SAME database:

  PHASE=divergence   (server started with auth.mode = local_only|hybrid)
     Reproduces the store drift in every direction:
       - alice: admin -> member via the Members UI path (role-assignments).
                RBAC becomes member; Membership.role stays 'admin' (stale).
       - carol: member -> admin via the Members UI path (role-assignments).
                RBAC becomes admin; Membership.role stays 'member' (stale).
       - bob:   admin -> member via the legacy PUT /members/{id} (update_member).
                Membership.role becomes 'member'; RBAC stays admin (retained
                full_admin_access — a privilege bug).

  PHASE=sso          (server RESTARTED with auth.mode = sso_only, same DB)
     Shows the break-glass local-login gate keys off the WRONG store:
       - carol (real RBAC admin, stale role='member') is BLOCKED from password
         login -> a genuine admin locked out of their own deployment.
       - alice (now only RBAC member, stale role='admin') is ALLOWED password
         login -> a demoted user keeps break-glass access they shouldn't have.

Usage:
  # phase 1 (local_only server on :8000)
  BOW_PHASE=divergence uv run python scripts/verify_role_demotion.py
  # then restart the server in sso_only mode on :8000 and:
  BOW_PHASE=sso        uv run python scripts/verify_role_demotion.py
"""
import os
import sys
import httpx

BASE = os.environ.get("BOW_BASE", "http://localhost:8000")
PHASE = os.environ.get("BOW_PHASE", "divergence")
PW = "supersecret123"

ADMIN = {"name": "Boot Admin", "email": "admin@example.com", "password": PW}
ALICE = {"name": "Alice Invited", "email": "alice@example.com", "password": PW}
BOB = {"name": "Bob Invited", "email": "bob@example.com", "password": PW}
CAROL = {"name": "Carol Invited", "email": "carol@example.com", "password": PW}

results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), "-", label, ("" if cond else f"  >> {extra}"))


def summary_and_exit():
    ok = sum(results)
    print("\n==== SUMMARY ====")
    print(f"{ok}/{len(results)} checks matched expectation")
    print("ALL EXPECTATIONS MET" if ok == len(results) else "SOME EXPECTATIONS NOT MET")
    sys.exit(0 if ok == len(results) else 1)


def login(c, email, password=PW):
    r = c.post("/api/auth/jwt/login", data={"username": email, "password": password})
    return r


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def whoami(c, token):
    r = c.get("/api/users/whoami", headers=bearer(token))
    return r.json()


def org_role_and_perms(profile):
    o = profile["organizations"][0]
    return o.get("role"), set(o.get("permissions") or []), o


# ---------------------------------------------------------------------------
# Shared setup helpers
# ---------------------------------------------------------------------------

def register_first_admin(c):
    r = c.post("/api/auth/register", json=ADMIN)
    # already-registered on a re-run is fine
    ok = r.status_code in (200, 201) or "REGISTER_USER_ALREADY_EXISTS" in r.text
    check("bootstrap admin registered", ok, f"{r.status_code} {r.text[:200]}")
    r = login(c, ADMIN["email"])
    check("bootstrap admin can log in", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    token = r.json()["access_token"]
    org_id = c.get("/api/organizations", headers=bearer(token)).json()[0]["id"]
    return token, org_id


def roles_map(c, admin_h):
    rs = c.get("/api/organizations/{}/roles".format(ORG), headers=admin_h).json()
    return {x["name"]: x["id"] for x in rs}


def members_map(c, admin_h):
    ms = c.get("/api/organizations/{}/members".format(ORG), headers=admin_h).json()
    out = {}
    for m in ms:
        email = (m.get("user") or {}).get("email") or m.get("email")
        out[email] = m
    return out


def invite_and_register(c, admin_h, person, role):
    """Invite `person` with legacy role string, accept the invite, register."""
    r = c.post(f"/api/organizations/{ORG}/members", headers=admin_h,
               json={"organization_id": ORG, "email": person["email"], "role": role})
    if not (r.status_code in (200, 201)):
        # tolerate "already a member" on re-runs
        if "Already a member" not in r.text:
            check(f"invite {person['email']} as {role}", False, f"{r.status_code} {r.text[:200]}")
            return
    mid = None
    for email, m in members_map(c, admin_h).items():
        if email == person["email"]:
            mid = m["id"]
    link = c.get(f"/api/organizations/{ORG}/members/{mid}/invite-link", headers=admin_h)
    token = link.json().get("token") if link.status_code == 200 else None
    body = dict(person)
    if token:
        body["invite_token"] = token
    r = c.post("/api/auth/register", json=body)
    ok = r.status_code in (200, 201) or "ALREADY_EXISTS" in r.text
    check(f"invited {person['email']} (role={role}) registers", ok, f"{r.status_code} {r.text[:200]}")


def assign_role(c, admin_h, user_id, role_id):
    return c.post(f"/api/organizations/{ORG}/role-assignments", headers=admin_h,
                  json={"role_id": role_id, "principal_type": "user", "principal_id": user_id})


def unassign_role(c, admin_h, user_id, role_id):
    lst = c.get(f"/api/organizations/{ORG}/role-assignments",
                headers=admin_h, params={"principal_type": "user", "principal_id": user_id}).json()
    for a in lst:
        if a["role_id"] == role_id:
            c.delete(f"/api/organizations/{ORG}/role-assignments/{a['id']}", headers=admin_h)


# ---------------------------------------------------------------------------
# PHASE: divergence
# ---------------------------------------------------------------------------

def phase_divergence(c):
    global ORG
    admin_token, ORG = register_first_admin(c)
    admin_h = {**bearer(admin_token), "X-Organization-Id": ORG}
    roles = roles_map(c, admin_h)
    check("system roles admin+member exist", "admin" in roles and "member" in roles, str(list(roles)))

    # Create the three invited users.
    invite_and_register(c, admin_h, ALICE, "admin")
    invite_and_register(c, admin_h, BOB, "admin")
    invite_and_register(c, admin_h, CAROL, "member")

    mm = members_map(c, admin_h)
    alice_uid = (mm[ALICE["email"]].get("user") or {}).get("id") or mm[ALICE["email"]].get("user_id")
    bob_uid = (mm[BOB["email"]].get("user") or {}).get("id") or mm[BOB["email"]].get("user_id")
    carol_uid = (mm[CAROL["email"]].get("user") or {}).get("id") or mm[CAROL["email"]].get("user_id")

    # Sanity: alice started as a working admin.
    a = whoami(c, login(c, ALICE["email"]).json()["access_token"])
    role, perms, _ = org_role_and_perms(a)
    check("alice starts as a working admin (full_admin_access)",
          "full_admin_access" in perms, f"role={role} perms={perms}")

    # --- alice: admin -> member via the Members UI path (role-assignments) ---
    assign_role(c, admin_h, alice_uid, roles["member"])
    unassign_role(c, admin_h, alice_uid, roles["admin"])

    # --- carol: member -> admin via the Members UI path (role-assignments) ---
    assign_role(c, admin_h, carol_uid, roles["admin"])
    unassign_role(c, admin_h, carol_uid, roles["member"])

    # --- bob: admin -> member via the LEGACY update_member (PUT /members) ---
    bob_mid = mm[BOB["email"]]["id"]
    c.put(f"/api/organizations/{ORG}/members/{bob_mid}", headers=admin_h, json={"role": "member"})

    # ---- Assertions: read the two stores back and show they disagree ----
    mm = members_map(c, admin_h)

    def legacy_role(email):
        return mm[email].get("role")

    def rbac_role_names(email):
        return sorted(r["name"] for r in (mm[email].get("roles") or []))

    # alice: legacy still 'admin', RBAC now 'member'  -> DRIFT
    a_legacy, a_rbac = legacy_role(ALICE["email"]), rbac_role_names(ALICE["email"])
    check("BUG alice: legacy role vs RBAC DRIFT after UI demotion",
          a_legacy == "admin" and a_rbac == ["member"],
          f"legacy={a_legacy} rbac={a_rbac} (expected legacy=admin, rbac=[member])")
    _, aperms, _ = org_role_and_perms(whoami(c, login(c, ALICE["email"]).json()["access_token"]))
    check("alice effective perms are member (no full_admin_access)",
          "full_admin_access" not in aperms, f"perms={aperms}")

    # carol: legacy still 'member', RBAC now 'admin'  -> DRIFT
    c_legacy, c_rbac = legacy_role(CAROL["email"]), rbac_role_names(CAROL["email"])
    check("BUG carol: legacy role vs RBAC DRIFT after UI promotion",
          c_legacy == "member" and c_rbac == ["admin"],
          f"legacy={c_legacy} rbac={c_rbac} (expected legacy=member, rbac=[admin])")

    # bob: legacy 'member' but RBAC untouched -> still full_admin_access (privilege bug)
    b_legacy, b_rbac = legacy_role(BOB["email"]), rbac_role_names(BOB["email"])
    _, bperms, _ = org_role_and_perms(whoami(c, login(c, BOB["email"]).json()["access_token"]))
    check("BUG bob: legacy update_member demoted to 'member' but RBAC still admin",
          b_legacy == "member" and b_rbac == ["admin"] and "full_admin_access" in bperms,
          f"legacy={b_legacy} rbac={b_rbac} perms_has_admin={'full_admin_access' in bperms}")

    print("\n[divergence] Reproduced: the legacy Membership.role and RBAC role_assignments")
    print("             disagree after every kind of role change. Now restart the server")
    print("             with auth.mode=sso_only and run BOW_PHASE=sso to see the lockout.")


# ---------------------------------------------------------------------------
# PHASE: sso  (server must be running in auth.mode = sso_only)
# ---------------------------------------------------------------------------

def phase_sso(c):
    # In sso_only, local/password login is break-glass, allowed only for
    # superusers or Membership.role == 'admin' (auth.py _user_can_use_local_login).
    # Because that gate reads the LEGACY string, the drift from phase 1 misfires.

    # Bootstrap admin (role='admin', really admin) -> should still log in.
    r = login(c, ADMIN["email"])
    check("sso_only: real bootstrap admin can break-glass login", r.status_code == 200,
          f"{r.status_code} {r.text[:160]}")

    # carol: RBAC admin but legacy role='member' -> WRONGLY BLOCKED.
    r = login(c, CAROL["email"])
    check("BUG sso_only: real RBAC admin (carol) is LOCKED OUT of password login",
          r.status_code != 200,
          f"expected != 200 (blocked); got {r.status_code} {r.text[:160]}")

    # alice: only RBAC member but legacy role='admin' -> WRONGLY ALLOWED.
    r = login(c, ALICE["email"])
    check("BUG sso_only: demoted member (alice) still gets break-glass admin login",
          r.status_code == 200,
          f"expected 200 (wrongly allowed); got {r.status_code} {r.text[:160]}")


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    print(f"### phase={PHASE} base={BASE}\n")
    if PHASE == "divergence":
        phase_divergence(c)
    elif PHASE == "sso":
        phase_sso(c)
    else:
        print(f"unknown BOW_PHASE={PHASE!r}")
        sys.exit(2)
    summary_and_exit()


ORG = None

if __name__ == "__main__":
    main()
