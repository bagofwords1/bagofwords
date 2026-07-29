#!/usr/bin/env python3
"""
Seed the sandbox for a load run: users, org, mock-LLM provider, demo data source.

Writes loadtest/sandbox_state.json which every other script reads, so the
harness and the Playwright drivers all agree on ids and tokens.

Creates N distinct users (not one user hammering the API N times) because the
per-user paths matter: org membership lookups, permission checks and the
per-report queue all key off the acting user, and a single-user run would
under-exercise them.
"""
import argparse
import json
import os
import sys
import time

import httpx

BASE = os.getenv("BOW_BASE", "http://127.0.0.1:8000")
MOCK_LLM = os.getenv("MOCK_LLM_BASE", "http://127.0.0.1:9001/v1")
STATE = os.path.join(os.path.dirname(__file__), "sandbox_state.json")

# The sandbox talks to itself; the agent proxy must not intercept.
CLIENT_KW = dict(timeout=120.0, trust_env=False)


def register(c, email, password, name):
    r = c.post(f"{BASE}/api/auth/register",
               json={"email": email, "password": password, "name": name})
    if r.status_code >= 400 and "REGISTER_USER_ALREADY_EXISTS" not in r.text:
        r.raise_for_status()
    r = c.post(f"{BASE}/api/auth/jwt/login",
               data={"username": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def org_of(c, token):
    r = c.get(f"{BASE}/api/organizations",
              headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    orgs = r.json()
    return orgs[0]["id"] if orgs else None


EXTRA_MEMBER_PERMS = ["manage_instructions", "create_data_source",
                      "manage_connections"]


def grant_member_permissions():
    """Add instruction/agent authoring permissions to the default member role."""
    import sqlalchemy as sa

    url = os.getenv("BOW_DATABASE_URL", "postgresql://bow:bow@127.0.0.1:5432/bow")
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, permissions FROM roles WHERE name = 'member'")).fetchall()
        for rid, perms in rows:
            perms = list(perms or [])
            merged = perms + [p for p in EXTRA_MEMBER_PERMS if p not in perms]
            if merged != perms:
                conn.execute(
                    sa.text("UPDATE roles SET permissions = CAST(:p AS json) "
                            "WHERE id = :id"),
                    {"p": json.dumps(merged), "id": rid})
    eng.dispose()
    print(f"member role granted: {', '.join(EXTRA_MEMBER_PERMS)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=110,
                    help="how many load users to provision")
    ap.add_argument("--password", default="LoadTest123!")
    args = ap.parse_args()

    with httpx.Client(**CLIENT_KW) as c:
        # --- admin / first user (auto-creates the org) ---
        admin_email = "admin@bowloadtest.com"
        admin_token = register(c, admin_email, args.password, "Load Admin")
        org_id = org_of(c, admin_token)
        if not org_id:
            print("ERROR: no organization after admin register", file=sys.stderr)
            return 1
        H = {"Authorization": f"Bearer {admin_token}",
             "X-Organization-Id": org_id, "Content-Type": "application/json"}
        print(f"admin ok  org={org_id}")

        # --- LLM provider pointed at the mock ---
        r = c.get(f"{BASE}/api/llm/models?is_enabled=true", headers=H)
        existing = r.json() if r.status_code == 200 else []
        if not any(m.get("model_id") == "mock-fast" for m in existing):
            r = c.post(f"{BASE}/api/llm/providers", headers=H, json={
                "name": f"MockLLM-{int(time.time())}",
                "provider_type": "custom",
                # For provider_type=custom the service reads base_url out of
                # `credentials` and folds it into additional_config itself.
                "credentials": {"api_key": "mock-key", "base_url": MOCK_LLM},
                "models": [{
                    "name": "Mock Fast", "model_id": "mock-fast",
                    "is_default": True, "is_small_default": True,
                    "context_window_tokens": 200000,
                    "input_cost_per_million_tokens_usd": 1,
                    "output_cost_per_million_tokens_usd": 5,
                }],
            })
            if r.status_code >= 400:
                print("provider create failed:", r.status_code, r.text[:600])
                return 1
            print("mock llm provider created")
        else:
            print("mock llm provider already present")

        # --- demo data source (chinook) ---
        r = c.get(f"{BASE}/api/data_sources/demos", headers=H)
        demos = r.json() if r.status_code == 200 else []
        demo_id = None
        for d in demos:
            if "chinook" in (d.get("id", "") + d.get("name", "")).lower():
                demo_id = d["id"]
                break
        if demo_id is None and demos:
            demo_id = demos[0]["id"]
        ds_id = None
        if demo_id:
            r = c.post(f"{BASE}/api/data_sources/demos/{demo_id}", headers=H, json={})
            if r.status_code < 400:
                ds_id = r.json().get("data_source_id") or r.json().get("id")
                print(f"demo data source installed: {demo_id} -> {ds_id}")
            else:
                print("demo install failed:", r.status_code, r.text[:300])
        if not ds_id:
            r = c.get(f"{BASE}/api/data_sources", headers=H)
            dss = r.json() if r.status_code == 200 else []
            if dss:
                ds_id = dss[0]["id"]
                print(f"using existing data source {ds_id}")

        # --- load users ---
        users = []
        for i in range(args.users):
            email = f"load{i:03d}@bowloadtest.com"
            try:
                tok = register(c, email, args.password, f"Load User {i:03d}")
                users.append({"email": email, "token": tok})
            except Exception as e:
                print(f"user {i} failed: {e}", file=sys.stderr)
        print(f"provisioned {len(users)} load users")

        # The workload under test has ordinary users authoring instructions and
        # creating agents from the /agents page. The stock `member` role has
        # neither permission, and the role-editor API is enterprise-gated, so
        # grant them on the member role directly. This models an org whose
        # members are analysts; without it cohort B would measure 403 handling.
        grant_member_permissions()

        # Every load user must be an actual *member* of the org, otherwise
        # every request 403s and we'd be load-testing the permission check.
        # GET /api/organizations is not proof of membership — it lists orgs the
        # user can see, so verify with a real org-scoped call instead.
        added = 0
        for u in users:
            r = c.post(f"{BASE}/api/organizations/{org_id}/members", headers=H,
                       json={"organization_id": org_id, "email": u["email"],
                             "role": "member"})
            if r.status_code < 400:
                added += 1
            elif "Already a member" not in r.text:
                print(f"  add_member {u['email']}: {r.status_code} {r.text[:120]}")
        print(f"added {added} memberships")

        verified = 0
        for u in users:
            u["org_id"] = org_id
            r = c.get(f"{BASE}/api/data_sources",
                      headers={"Authorization": f"Bearer {u['token']}",
                               "X-Organization-Id": org_id})
            u["verified"] = r.status_code < 400
            verified += 1 if u["verified"] else 0
        print(f"{verified}/{len(users)} users verified against the seeded org")
        # Drop unverified users rather than let them generate 403s that would
        # look like load. The default license caps the org at 100 seats, so
        # L100 is served by 99 members + the admin.
        users = [u for u in users if u.get("verified")]
        users.append({"email": admin_email, "token": admin_token,
                      "org_id": org_id, "verified": True})
        print(f"usable identities: {len(users)}")

    state = {
        "base": BASE, "org_id": org_id, "admin_token": admin_token,
        "admin_email": admin_email, "password": args.password,
        "data_source_id": ds_id, "users": users,
        "mock_llm": MOCK_LLM, "seeded_at": time.time(),
    }
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"wrote {STATE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
