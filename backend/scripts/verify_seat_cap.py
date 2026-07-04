"""End-to-end verification of license seat-cap enforcement on the AUTO-PROVISIONING
paths, against a running server.

Regression harness for the bug where an EE org with a user cap kept gaining
uninvited members (Entra SCIM / domain signup) while the admin's manual invite —
the only guarded path — was blocked at the overflowed count.

Prereqs: start the server with an active enterprise license whose ``max_users``
is a small cap (the sandbox uses 3). The script reads the cap from
``GET /api/license/usage`` and drives to exactly fill + overflow, so it adapts to
whatever cap the server was started with (must be >= 2).

    BOW_LICENSE_KEY=$(python setup_license.py 3) \
        BOW_DATABASE_URL=sqlite:///db/app.db python main.py &
    python scripts/verify_seat_cap.py

Drives, over real HTTP:
  1. license loaded with the cap (GET /license/usage)
  2. SCIM provisioning fills seats up to the cap, then the next create -> 402
     (Entra's provisioning path — the representative auto-provision bypass)
  3. SCIM re-provisioning an existing member -> 409 (not masked by the 402)
  4. domain-signup registration over the cap -> user created but NO org membership
     (auto-provision refused, not a login error)
  5. the admin's own manual invite is also blocked at the cap -> 402
  6. member count never exceeds the cap
"""
import sys
import uuid

import httpx

BASE = "http://localhost:8000"
ADMIN = {"name": "Admin User", "email": "admin@seatcap-sandbox.com", "password": "supersecret123"}
SIGNUP_DOMAIN = "signup-sandbox.com"


def _rand_email(domain):
    return f"u_{uuid.uuid4().hex[:8]}@{domain}"


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    results = []

    def check(label, cond, extra=""):
        results.append(cond)
        print(("PASS" if cond else "FAIL"), "-", label, ("" if cond else f"  >> {extra}"))

    # 1. Bootstrap admin + org.
    c.post("/api/auth/register", json=ADMIN)
    r = c.post("/api/auth/jwt/login", data={"username": ADMIN["email"], "password": ADMIN["password"]})
    jwt = r.json()["access_token"]
    admin_h = {"Authorization": f"Bearer {jwt}"}
    org_id = c.get("/api/organizations", headers=admin_h).json()[0]["id"]
    org_h = {**admin_h, "X-Organization-Id": org_id}

    # 2. License is active with a seat cap.
    r = c.get("/api/license/usage", headers=org_h)
    usage = r.json()
    cap = usage.get("max_users", -1)
    current = usage.get("current_users", 0)
    check("license loaded with a seat cap", r.status_code == 200 and cap >= 2,
          f"{r.status_code} usage={usage}")
    if cap < 2:
        print("\nServer must run with an enterprise license and max_users>=2. Aborting.")
        sys.exit(1)
    check("org starts below the cap (admin seat only)", current == 1, f"current={current}")

    # 3. Mint a SCIM token (Entra's provisioning credential).
    r = c.post("/api/enterprise/scim/tokens", headers=org_h, json={"name": "entra"})
    check("create SCIM token", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    scim_token = r.json()["token"]
    scim_h = {
        "Authorization": f"Bearer {scim_token}",
        "Content-Type": "application/scim+json",
    }

    def scim_create(email):
        return c.post("/scim/v2/Users", headers=scim_h, json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": email,
            "emails": [{"value": email, "primary": True}],
            "active": True,
        })

    # 4. SCIM fills the remaining seats exactly (block only truly new members: each
    #    fresh provision consumes one seat up to the cap).
    seats_to_fill = cap - 1  # admin already holds one
    first_emails = [_rand_email("scimsandbox.com") for _ in range(seats_to_fill)]
    fill_ok = True
    for email in first_emails:
        rr = scim_create(email)
        if rr.status_code != 201:
            fill_ok = False
            print(f"    unexpected SCIM fill status {rr.status_code}: {rr.text[:160]}")
    check(f"SCIM provisions up to the cap ({seats_to_fill} new members -> 201)", fill_ok)

    # 5. The next SCIM provision is over the cap -> 402 (the bug: this used to be 201).
    over = scim_create(_rand_email("scimsandbox.com"))
    check("SCIM create over the cap -> 402", over.status_code == 402,
          f"{over.status_code} {over.text[:200]}")

    # 6. Re-provisioning an EXISTING member still -> 409, not masked by the 402.
    dup = scim_create(first_emails[0])
    check("SCIM re-provision of existing member -> 409 (ordering preserved)",
          dup.status_code == 409, f"{dup.status_code} {dup.text[:200]}")

    # 7. Domain-signup path: enable the org policy, then a brand-new user registers
    #    with an allowed domain while the org is full -> account is created but gets
    #    NO membership (auto-provision refused, not a hard login error).
    r = c.put("/api/organization/signup-policy", headers=org_h, json={
        "enabled": True, "allowed_domains": [SIGNUP_DOMAIN], "auto_invite_role": "member",
    })
    check("enable domain-signup policy", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    newcomer = {"name": "Newcomer", "email": _rand_email(SIGNUP_DOMAIN), "password": "supersecret123"}
    r = c.post("/api/auth/register", json=newcomer)
    check("over-cap domain signup still registers the user", r.status_code in (200, 201),
          f"{r.status_code} {r.text[:200]}")
    r = c.post("/api/auth/jwt/login",
               data={"username": newcomer["email"], "password": newcomer["password"]})
    newcomer_orgs = c.get("/api/organizations",
                          headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    check("over-cap domain signup grants NO org membership", newcomer_orgs == [],
          f"orgs={newcomer_orgs}")

    # 8. The admin's own manual invite is blocked at the cap too (the reported symptom).
    r = c.post(f"/api/organizations/{org_id}/members",
               json={"organization_id": org_id, "email": _rand_email("invitesandbox.com"), "role": "member"},
               headers=org_h)
    check("admin manual invite over the cap -> 402", r.status_code == 402,
          f"{r.status_code} {r.text[:200]}")

    # 9. Member count never exceeded the cap.
    final = c.get("/api/license/usage", headers=org_h).json().get("current_users", -1)
    check(f"member count held at the cap ({cap})", final == cap, f"current_users={final}")

    passed = sum(1 for x in results if x)
    print("\n==== SUMMARY ====")
    print(f"{passed}/{len(results)} passed")
    if passed == len(results):
        print("ALL PASSED")
        sys.exit(0)
    print("FAILURES PRESENT")
    sys.exit(1)


if __name__ == "__main__":
    main()
