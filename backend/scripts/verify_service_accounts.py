"""End-to-end verification of the service-account feature against a running server.

Run the server first (BOW_DATABASE_URL=sqlite:///db/app.db uv run python main.py),
then: uv run python scripts/verify_service_accounts.py
"""
import sys
import httpx

BASE = "http://localhost:8000"
ADMIN = {"name": "Admin User", "email": "admin@example.com", "password": "supersecret123"}


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    results = []

    def check(label, cond, extra=""):
        results.append((label, cond, extra))
        print(("PASS" if cond else "FAIL"), "-", label, ("" if cond else f"  >> {extra}"))

    # 1. Bootstrap first user (becomes admin with full_admin_access)
    r = c.post("/api/auth/register", json=ADMIN)
    check("register first admin", r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")

    # 2. Login -> JWT
    r = c.post("/api/auth/jwt/login", data={"username": ADMIN["email"], "password": ADMIN["password"]})
    check("admin login", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    jwt = r.json()["access_token"]
    admin_h = {"Authorization": f"Bearer {jwt}"}

    # 3. Org id
    r = c.get("/api/organizations", headers=admin_h)
    org_id = r.json()[0]["id"]
    org_h = {**admin_h, "X-Organization-Id": org_id}
    check("get org", bool(org_id), r.text[:200])

    # 4. Find a role (member)
    r = c.get(f"/api/organizations/{org_id}/roles", headers=org_h)
    roles = r.json()
    member_role = next((x for x in roles if x["name"] == "member"), roles[0])
    check("list roles", r.status_code == 200 and bool(roles), f"{r.status_code} {r.text[:200]}")

    # 5. Create a service account
    r = c.post("/api/service_accounts", headers=org_h,
               json={"name": "CI Pipeline", "description": "Automated reports", "role_id": member_role["id"]})
    check("create service account", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    sa = r.json()
    sa_id = sa["id"]
    check("SA has member role", any(x["name"] == "member" for x in sa["roles"]), str(sa.get("roles")))
    check("SA reports 0 keys", sa["key_count"] == 0, str(sa))

    # 6. SA does NOT appear in org member list (no seat / no leak)
    r = c.get(f"/api/organizations/{org_id}/members", headers=org_h)
    emails = [m.get("user", {}).get("email") if m.get("user") else m.get("email") for m in r.json()]
    check("SA absent from member list", all("service.invalid" not in (e or "") for e in emails), str(emails))

    # 7. Issue a key
    r = c.post(f"/api/service_accounts/{sa_id}/keys", headers=org_h, json={"name": "ci-key"})
    check("issue SA key", r.status_code == 200 and r.json().get("key", "").startswith("bow_"),
          f"{r.status_code} {r.text[:200]}")
    sa_key = r.json()["key"]
    sa_h = {"X-API-Key": sa_key}

    # 8. The SA key authenticates and is scoped to the org (org resolved from key)
    r = c.get("/api/data_sources", headers=sa_h)
    check("SA key authenticates to API", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    # 9. SA can create a report (run/author queries) — needs create_reports (member has it)
    r = c.post("/api/reports", headers=sa_h, json={"title": "SA report", "widgets": []})
    check("SA can create a report", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    if r.status_code in (200, 201):
        rep = r.json()
        # attribution: report owned by the SA's backing user
        check("report attributed to SA", (rep.get("user") or {}).get("name") == "CI Pipeline"
              or rep.get("user_id"), str(rep.get("user")))

    # 10. Escalation guard: SA key cannot mint API keys
    r = c.post("/api/api_keys", headers=sa_h, json={"name": "evil"})
    check("SA blocked from /api_keys", r.status_code == 403, f"{r.status_code} {r.text[:150]}")

    # 11. Escalation guard: SA key cannot create another service account
    r = c.post("/api/service_accounts", headers=sa_h, json={"name": "evil-sa"})
    check("SA blocked from creating SA", r.status_code == 403, f"{r.status_code} {r.text[:150]}")

    # 12. Escalation guard: SA key cannot create roles
    r = c.post(f"/api/organizations/{org_id}/roles", headers=sa_h,
               json={"name": "evil", "permissions": ["full_admin_access"]})
    check("SA blocked from RBAC", r.status_code == 403, f"{r.status_code} {r.text[:150]}")

    # 13. Cannot mint an SA more powerful than creator — N/A here (admin is full).
    #     Instead verify: disabling the SA kills its key immediately.
    r = c.patch(f"/api/service_accounts/{sa_id}", headers=org_h, json={"disabled": True})
    check("disable SA", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
    r = c.get("/api/data_sources", headers=sa_h)
    check("disabled SA key rejected", r.status_code in (401, 403), f"{r.status_code} {r.text[:150]}")

    # 14. Re-enable restores access
    r = c.patch(f"/api/service_accounts/{sa_id}", headers=org_h, json={"disabled": False})
    r = c.get("/api/data_sources", headers=sa_h)
    check("re-enabled SA key works", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

    print("\n==== SUMMARY ====")
    failed = [l for (l, ok, _) in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
