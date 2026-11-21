#!/usr/bin/env python3
"""Seed two agents on ONE MCP connection with different tool rules (BOW-206 check).

Needs a running stack and ``tools/agent/mock_jira_mcp.py`` on :8766.

  * connection "Jira" (mock), every tool allowed by default, nothing locked
  * "Ops agent":     create_issue = allow, add_comment = allow
  * "Finance agent": create_issue = deny,  add_comment = off

Before BOW-206 a report with BOTH agents merged them strictest-wins, so
create_issue and add_comment disappeared for Ops' work too. Now each call goes
through the agent that enables it.

    cd backend && uv run python ../tools/agent/two_agents_one_mcp.py
"""
import json
import os

import requests

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
MCP_URL = os.environ.get("MOCK_JIRA_URL", "http://localhost:8766/mcp")


def main():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
    r.raise_for_status()
    s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    s.headers["X-Organization-Id"] = s.get(f"{BASE}/api/users/whoami").json()["organizations"][0]["id"]

    conns = {c["name"]: c for c in s.get(f"{BASE}/api/connections").json()}
    if "Jira" in conns:
        conn_id = conns["Jira"]["id"]
    else:
        r = s.post(f"{BASE}/api/connections", json={
            "name": "Jira", "type": "mcp", "credentials": {}, "auth_policy": "system_only",
            "config": {"server_url": MCP_URL, "transport": "streamable_http"},
        })
        r.raise_for_status()
        conn_id = r.json()["id"]
    s.post(f"{BASE}/api/connections/{conn_id}/refresh-tools").raise_for_status()
    tools = {t["name"]: t for t in s.get(f"{BASE}/api/connections/{conn_id}/tools").json()}
    for t in tools.values():  # connection default: on, allow, unlocked
        s.put(f"{BASE}/api/connections/{conn_id}/tools/{t['id']}",
              json={"is_enabled": True, "policy": "allow", "policy_locked": False}).raise_for_status()

    agents = {a["name"]: a["id"] for a in s.get(f"{BASE}/api/data_sources").json()}

    def agent(name):
        if name in agents:
            return agents[name]
        r = s.post(f"{BASE}/api/data_sources", json={
            "name": name, "type": "mcp", "connection_ids": [conn_id], "is_public": True})
        r.raise_for_status()
        return r.json()["id"]

    ops, fin = agent("Ops agent"), agent("Finance agent")

    def rule(ds, tool, **body):
        s.put(f"{BASE}/api/data_sources/{ds}/tools/{tools[tool]['id']}", json=body).raise_for_status()

    rule(ops, "create_issue", is_enabled=True, policy="allow")
    rule(ops, "add_comment", is_enabled=True, policy="allow")
    rule(fin, "create_issue", is_enabled=True, policy="deny")
    rule(fin, "add_comment", is_enabled=False)

    view = {}
    for name, ds in (("Ops agent", ops), ("Finance agent", fin)):
        view[name] = {t["name"]: (t["is_enabled"], t["effective_policy"])
                      for t in s.get(f"{BASE}/api/data_sources/{ds}/tools").json()}
    print(json.dumps({"connection": conn_id, "ops": ops, "finance": fin, "rules": view}, indent=2))


if __name__ == "__main__":
    main()
