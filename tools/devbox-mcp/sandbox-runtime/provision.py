#!/usr/bin/env python3
"""Provision a Bag of Words instance through its API: admin user + organization,
an LLM provider with one default model, onboarding marked complete.

Idempotent: safe to run on every boot and after a database reset. Inputs come
from the environment (see entrypoint.sh / bow-stack provision):
  BACKEND_PORT, BOW_ADMIN_EMAIL, BOW_ADMIN_PASSWORD, BOW_ADMIN_NAME,
  ANTHROPIC_KEY | OPENAI_API_KEY, *_MODEL_ID, *_MODEL_NAME
"""
import os, sys, httpx

base = f"http://127.0.0.1:{os.environ['BACKEND_PORT']}"
email, password, name = (os.environ["BOW_ADMIN_EMAIL"],
                         os.environ["BOW_ADMIN_PASSWORD"],
                         os.environ["BOW_ADMIN_NAME"])
c = httpx.Client(base_url=base, timeout=60)

def ok(r, *codes):
    if r.status_code not in codes:
        sys.exit(f"{r.request.method} {r.request.url.path} -> {r.status_code} {r.text[:300]}")
    return r

def login():
    return c.post("/api/auth/jwt/login", data={"username": email, "password": password})

# Admin user. Login first: on a container restart (pod recreated with the same
# workspace volume, node reboot, `kubectl delete pod`) the admin already exists
# and the backend refuses /register with "Sign-up is disabled" once any user
# exists and allow_uninvited_signups is false, so registering first is not
# idempotent. The first registered user bootstraps an organization
# automatically (see on_after_register in backend/app/core/auth.py).
r = login()
if r.status_code == 200:
    print(f"[provision] admin {email} already exists")
else:
    r = c.post("/api/auth/register", json={"email": email, "password": password, "name": name})
    if r.status_code in (200, 201):
        print(f"[provision] created admin {email}")
    elif r.status_code == 400 and "ALREADY_EXISTS" in r.text:
        print(f"[provision] admin {email} already exists (password in BOW_ADMIN_PASSWORD does not match?)")
    elif r.status_code == 400 and "Sign-up is disabled" in r.text:
        sys.exit(f"[provision] users exist but {email} cannot log in with BOW_ADMIN_PASSWORD, "
                 "and sign-up is closed. Run `bow-stack reset-db` for a fresh database.")
    else:
        ok(r, 201)
    r = login()

tok = ok(r, 200).json()["access_token"]
orgs = ok(c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}), 200).json()
if not orgs:
    sys.exit("[provision] no organization after registration")
org = orgs[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
print(f"[provision] organization {org}")

# LLM provider + one model (becomes default and small-default). Anthropic is the
# sandbox convention (.claude/skills/sandbox-feedback-loop: $ANTHROPIC_KEY, a
# single "Claude 4.5 Haiku"); OpenAI only when that is the only key available.
anthropic_key = os.environ.get("ANTHROPIC_KEY", "").strip()
openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
if anthropic_key:
    ptype, pname, key = "anthropic", "Anthropic", anthropic_key
    model_id, model_name = os.environ["ANTHROPIC_MODEL_ID"], os.environ["ANTHROPIC_MODEL_NAME"]
elif openai_key:
    ptype, pname, key = "openai", "OpenAI", openai_key
    model_id, model_name = os.environ["OPENAI_MODEL_ID"], os.environ["OPENAI_MODEL_NAME"]
else:
    ptype = None
    print("[provision] no ANTHROPIC_KEY (or OPENAI_API_KEY) set; skipping LLM provider setup")

if ptype:
    model = {"name": model_name, "model_id": model_id, "is_default": True, "is_small_default": True}
    providers = ok(c.get("/api/llm/providers", headers=H), 200).json()
    prov = next((p for p in providers if p.get("provider_type") == ptype), None)
    if prov:
        pid = prov["id"]
        ok(c.put(f"/api/llm/providers/{pid}", json={"credentials": {"api_key": key}, "is_enabled": True}, headers=H), 200)
        print(f"[provision] reusing {ptype} provider {pid}")
        models = ok(c.get("/api/llm/models", headers=H), 200).json()
        existing = next((m for m in models if m.get("model_id") == model_id), None)
        if existing:
            ok(c.patch(f"/api/llm/models/{existing['id']}",
                       json={"is_default": True, "is_small_default": True, "is_enabled": True}, headers=H), 200)
            print(f"[provision] defaulted existing model {model_id}")
        else:
            mid = ok(c.post("/api/llm/models", json={"provider_id": pid, **model}, headers=H), 200, 201).json().get("id")
            print(f"[provision] created default model {model_id} ({mid})")
    else:
        # Provider + its model in one call: the create route creates the listed models.
        r = ok(c.post("/api/llm/providers", json={
            "name": pname, "provider_type": ptype, "credentials": {"api_key": key}, "models": [model]},
            headers=H), 200, 201).json()
        print(f"[provision] created {ptype} provider {r.get('id')} with default model {model_id}")
    t = c.post("/api/llm/test_connection", json={
        "name": pname, "provider_type": ptype, "credentials": {"api_key": key}}, headers=H)
    print(f"[provision] {ptype} test_connection: {t.status_code} {t.text[:120]}")

# Mark onboarding complete so the UI opens on the workspace, not the wizard.
ok(c.put("/api/organization/onboarding", json={"completed": True, "dismissed": True}, headers=H), 200)
print("[provision] onboarding marked complete")
