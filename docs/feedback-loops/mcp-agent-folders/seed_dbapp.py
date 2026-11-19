"""Per-database folders (db-app, db-usage), one agent per environment inside.
Run after seed.py:  cd backend && uv run python ../docs/feedback-loops/mcp-agent-folders/seed_dbapp.py"""
import json, pathlib, httpx
HERE = pathlib.Path(__file__).resolve().parent
seed = json.loads((HERE / "seed.json").read_text())
H = {"Authorization": "Bearer " + seed["users"]["admin"]["token"], "X-Organization-Id": seed["org_id"]}
c = httpx.Client(base_url="http://localhost:8000", timeout=120)
def api(m, p, b=None):
    r = c.request(m, "/api/" + p, json=b, headers=H); assert r.is_success, (p, r.text); return r.json()
out = {}
for db in ("db-app", "db-usage"):
    fid = api("POST", "folders", {"name": db})["id"]; out[db] = {"folder": fid, "agents": {}}
    for env in ("prod", "squad-a-dev"):
        ds = api("POST", "data_sources", {"name": f"{db}-{env}", "type": "sqlite",
             "config": {"database": str(HERE / "data" / "disputes.db")}, "credentials": {}, "auth_policy": "system_only",
             "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False})
        api("PUT", f"data_sources/{ds['id']}", {"is_public": False})
        api("PUT", f"folders/items/agent/{ds['id']}/placement", {"parent_id": fid})
        api("GET", f"data_sources/{ds['id']}/refresh_schema"); api("POST", f"data_sources/{ds['id']}/bulk_update_tables", {"action": "activate"})
        out[db]["agents"][env] = ds["id"]
    # Alex manages db-app (like a DB owner), Dana can query it
    for who, role in (("alex", "Manager"), ("dana", "Viewer")):
        api("PUT", f"folders/{fid}/share", {"principal_type": "user", "principal_id": seed["users"][who]["user_id"], "role": role})
(HERE / "dbapp.json").write_text(json.dumps(out, indent=2)); print(json.dumps(out, indent=2))
