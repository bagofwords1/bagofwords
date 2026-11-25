#!/usr/bin/env python3
"""Seed the connector-actions sandbox: Outlook + Teams against the fake Microsoft 365.

Prereqs: a running stack whose backend was started with
    BOW_GRAPH_BASE_URL=http://localhost:8765/v1.0
    BOW_GRAPH_TOKEN_BASE_URL=http://localhost:8765
and ``tools/agent/mock_graph_server.py`` listening on :8765.

Creates (idempotent by name):
  * "Outlook" connection (Sign in with Microsoft) with two agents on it —
    "Inbox assistant" and "Finance desk" — to show per-agent rules;
  * "Microsoft Teams" connection (posts allowed only to "Alerts") with a
    "SOC agent";
then turns on actions per the demo, and signs the admin in to both
connections through the real OAuth code flow (authorize → fake IdP → BOW
callback), so the stored token carries exactly the scopes BOW requested.

    cd backend && uv run python ../tools/agent/connector_actions_sandbox.py
Prints the ids as JSON.
"""
import json
import os
import sys

import requests
from urllib.parse import urlparse

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
ENTRA_APP = {"tenant_id": "contoso-tenant", "client_id": "sandbox-client", "client_secret": "sandbox-secret"}


def main():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
    r.raise_for_status()
    s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    org = s.get(f"{BASE}/api/users/whoami").json()["organizations"][0]["id"]
    s.headers["X-Organization-Id"] = org

    existing = {c["name"]: c for c in s.get(f"{BASE}/api/connections").json()}

    def connection(name, conn_type, config=None):
        if name in existing:
            return existing[name]["id"]
        r = s.post(f"{BASE}/api/connections", json={
            "name": name, "type": conn_type, "credentials": ENTRA_APP, "config": config or {},
            "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
        })
        r.raise_for_status()
        return r.json()["id"]

    agents = {a["name"]: a for a in s.get(f"{BASE}/api/data_sources").json()}

    def agent(name, conn_type, conn_id):
        if name in agents:
            return agents[name]["id"]
        r = s.post(f"{BASE}/api/data_sources", json={
            "name": name, "type": conn_type, "connection_ids": [conn_id], "is_public": True,
            "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
        })
        r.raise_for_status()
        return r.json()["id"]

    outlook = connection("Outlook", "outlook_mail")
    teams = connection("Microsoft Teams", "teams_messages", {"allowed_post_channels": "Alerts"})
    inbox = agent("Inbox assistant", "outlook_mail", outlook)
    finance = agent("Finance desk", "outlook_mail", outlook)
    soc = agent("SOC agent", "teams_messages", teams)

    def tools(conn_id):
        return {t["name"]: t for t in s.get(f"{BASE}/api/connections/{conn_id}/tools").json()}

    def set_tool(conn_id, name, **body):
        tid = tools(conn_id)[name]["id"]
        s.put(f"{BASE}/api/connections/{conn_id}/tools/{tid}", json=body).raise_for_status()

    # Outlook: triage on (allow), drafts ask, sending ask + locked.
    set_tool(outlook, "flag_email", is_enabled=True, policy="allow")
    set_tool(outlook, "move_email", is_enabled=True, policy="allow")
    set_tool(outlook, "create_draft", is_enabled=True, policy="ask")
    set_tool(outlook, "send_email", is_enabled=True, policy="ask", policy_locked=True)
    # Teams: channel posts ask.
    set_tool(teams, "post_message", is_enabled=True, policy="ask")
    # Finance desk: read-only on the same mailbox (its own rule).
    ftools = {t["name"]: t for t in s.get(f"{BASE}/api/data_sources/{finance}/tools").json()}
    for name in ("flag_email", "move_email", "create_draft", "send_email"):
        s.put(f"{BASE}/api/data_sources/{finance}/tools/{ftools[name]['id']}", json={"is_enabled": False}).raise_for_status()

    def sign_in(conn_id):
        auth = s.get(f"{BASE}/api/connections/{conn_id}/oauth/authorize")
        url = auth.json()["authorization_url"]
        verifier = auth.cookies.get("conn_oauth_verifier") or next(
            (c.value for c in s.cookies if c.name == "conn_oauth_verifier"), "")
        # authorize → fake IdP (302 to BOW callback with a code) → BOW callback.
        r = requests.get(url, allow_redirects=False)
        assert r.status_code == 302, r.text
        # The callback URL carries the configured base_url (often the frontend
        # origin); call the backend directly with the PKCE cookie a browser
        # would have sent.
        loc = urlparse(r.headers["location"])
        cb = requests.get(f"{BASE}{loc.path}?{loc.query}", allow_redirects=False,
                          cookies={"conn_oauth_verifier": verifier})
        assert cb.status_code in (302, 303, 307), cb.text
        return cb.headers.get("location", "")

    signed = {"outlook": sign_in(outlook), "teams": sign_in(teams)}
    print(json.dumps({"org": org, "outlook": outlook, "teams": teams, "agents": {
        "inbox": inbox, "finance": finance, "soc": soc}, "signed_in_redirects": signed}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
