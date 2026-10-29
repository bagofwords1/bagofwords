"""Sandbox setup for the agents-push-to-git loop (run with the backend venv).

    uv run python <this file> connect   # connect the local repo (PAT + write access) and add an instruction
    uv run python <this file> write off # turn write access off (button should go disabled)
    uv run python <this file> write on
"""
import sys
import httpx

REPO_URL = "https://127.0.0.1:9419/acme/instructions.git"
c = httpx.Client(base_url="http://localhost:8000", timeout=120)
tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

repos = c.get("/api/git/repositories", headers=H).json()
cmd = sys.argv[1] if len(sys.argv) > 1 else "connect"
if cmd == "connect":
    if not repos:
        r = c.post("/api/git/repositories", headers=H, json={
            "provider": "custom", "repo_url": REPO_URL, "branch": "main",
            "access_token": "sandbox-token", "access_token_username": "bow", "write_enabled": True})
        r.raise_for_status()
        repos = c.get("/api/git/repositories", headers=H).json()
    print("repo", repos[0]["id"], "can_push", repos[0]["can_push"])
    ds = c.get("/api/data_sources", headers=H).json()
    r = c.post("/api/instructions", headers=H, json={
        "text": "Fiscal quarters start in February. Q1 = Feb-Apr.", "title": "Fiscal calendar",
        "status": "published", "category": "general", "data_source_ids": [ds[0]["id"]] if ds else []})
    print("instruction", r.status_code)
elif cmd == "write":
    r = c.put(f"/api/git/repositories/{repos[0]['id']}", headers=H, json={"write_enabled": sys.argv[2] == "on"})
    print("can_push", r.json().get("can_push"))
elif cmd == "member-push":
    mt = c.post("/api/auth/jwt/login", data={"username": "member@example.com", "password": "Password123!"}).json()["access_token"]
    r = c.post(f"/api/git/{repos[0]['id']}/push", headers={"Authorization": f"Bearer {mt}", "X-Organization-Id": org},
               json={"build_id": "any", "create_pr": True})
    print("member API push ->", r.status_code, r.json().get("detail"))
