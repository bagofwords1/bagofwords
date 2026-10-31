#!/usr/bin/env python3
"""Loop B for 2026-10-31-instruction-application-scope.md.

Against a running stack (tools/agent/boot_stack.sh) with a seeded admin
(tools/agent/seed_org.py --sqlite-sources 1) and $ANTHROPIC_KEY set:

1. adds an Anthropic provider (key from env only),
2. creates two connected apps ("Northwind ERP", "Support Portal") with OAuth,
3. creates one instruction scoped to Northwind ERP only, telling the agent to
   start every reply with a marker word,
4. asks the same question three times: in BOW, through Northwind ERP, and
   through Support Portal (each with that app's own OAuth token),
5. prints whether the marker reached the agent's prompt (the stored context
   snapshot) and whether the model followed it.

    cd backend && uv run python ../docs/feedback-loops/2026-10-31-instruction-application-scope.live.py
"""
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL, PASSWORD = os.environ.get("BOW_EMAIL", "admin@example.com"), os.environ.get("BOW_PASSWORD", "Password123!")
MODEL = os.environ.get("BOW_LIVE_MODEL", "claude-sonnet-5-5")
DB = Path(__file__).resolve().parents[2] / "backend" / "db" / "agent.db"
REDIRECT = "http://localhost:4173/callback"
MARKER = "NORTHWIND-OK"

c = httpx.Client(base_url=BASE, timeout=60)


def h(token, org):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def ok(r, *codes):
    if r.status_code not in (codes or (200, 201)):
        sys.exit(f"{r.request.method} {r.request.url} -> {r.status_code} {r.text[:400]}")
    return r.json() if r.content else None


token = ok(c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}))["access_token"]
org = ok(c.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}))["organizations"][0]["id"]
H = h(token, org)

providers = ok(c.get("/api/llm/providers", headers=H))
if not any(p.get("provider_type") == "anthropic" for p in providers):
    key = os.environ.get("ANTHROPIC_KEY") or sys.exit("ANTHROPIC_KEY not set")
    ok(c.post("/api/llm/providers", headers=H, json={
        "name": "anthropic-live", "provider_type": "anthropic", "credentials": {"api_key": key},
        "models": [{"model_id": MODEL, "name": MODEL, "is_custom": False, "is_default": True}],
    }))


def app_with_token(name):
    app = ok(c.post("/api/connected_applications", headers=H, json={"name": name, "agent_ids": None}))
    client = ok(c.post("/api/oauth/clients", headers=H, json={
        "application_id": app["id"], "name": name, "redirect_uris": [REDIRECT], "scopes": "app", "trusted": True}))
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth = ok(c.post("/api/oauth/authorize", headers=H, json={
        "client_id": client["client_id"], "redirect_uri": REDIRECT, "state": "s", "scope": "app",
        "code_challenge": challenge, "code_challenge_method": "S256"}))
    code = parse_qs(urlparse(auth["redirect_url"]).query)["code"][0]
    tok = ok(c.post("/api/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "client_secret": client.get("client_secret") or "",
        "code_verifier": verifier}))
    return app, tok["access_token"]


suffix = uuid.uuid4().hex[:4]
erp, erp_token = app_with_token(f"Northwind ERP {suffix}")
portal, portal_token = app_with_token(f"Support Portal {suffix}")

inst = ok(c.post("/api/instructions/global", headers=H, json={
    "title": "Northwind ERP reply format",
    "text": f"These users are warehouse managers inside Northwind ERP. Always begin every reply with the exact word {MARKER} on its own line.",
    "status": "published", "load_mode": "always",
    "applicable_channels": [f"connected_app:{erp['id']}"],
}))
print("instruction", inst["id"], "channels", inst["applicable_channels"])
# Control: an unscoped rule must reach every surface's prompt.
CONTROL = "CONTROL-" + suffix.upper()
ok(c.post("/api/instructions/global", headers=H, json={
    "title": "Org-wide control", "text": f"Org-wide rule {CONTROL}: be concise.",
    "status": "published", "load_mode": "always"}))


def ask(label, tok):
    """Stream one turn and collect the agent's own `instructions.context`
    event: the instructions it loaded into the prompt, emitted before the
    model is called (so it holds even if the LLM call later fails)."""
    hh = h(tok, org)
    report = ok(c.post("/api/reports", headers=hh, json={"title": f"live {label}", "files": [], "data_sources": []}))
    loaded, reply, event = [], "", None
    with c.stream("POST", f"/api/reports/{report['id']}/completions", headers={**hh, "Accept": "text/event-stream"},
                  timeout=300, json={"prompt": {"content": "In one short sentence, what can you help me with?",
                                                "mentions": []}, "stream": True}) as r:
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[5:])
                except ValueError:
                    continue
                payload = data.get("data", data) if isinstance(data, dict) else {}
                if event == "instructions.context":
                    loaded += [i.get("title") for i in payload.get("instructions", [])]
                reply += json.dumps(payload) if event and "block" in event else ""
    with sqlite3.connect(DB) as db:
        origin = db.execute("select origin_app_id from reports where id=?", (report["id"],)).fetchone()[0]
    print(json.dumps({"surface": label, "origin_app": origin and origin[:8], "loaded_instructions": loaded,
                      "reply_has_marker": MARKER in reply}))


ask("BOW web app", token)
ask("Northwind ERP", erp_token)
ask("Support Portal", portal_token)
