"""Small API helper for the loop: python api.py <cmd> [args]."""
import json, sys, httpx
BASE = "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=120, trust_env=False)
tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
cmd = sys.argv[1]
if cmd == "ds":
    for d in c.get("/api/data_sources", headers=H).json():
        det = c.get(f"/api/data_sources/{d['id']}", headers=H).json()
        print(d["id"], repr(d["name"]), [(x["id"], x["name"]) for x in det.get("connections", [])])
elif cmd == "rename":
    r = c.put(f"/api/data_sources/{sys.argv[2]}", json={"name": sys.argv[3]}, headers=H); print(r.status_code, r.json().get("name"))
elif cmd == "token":
    print(json.dumps({"token": tok, "org": org}))
elif cmd in ("drefresh", "drun"):
    r = c.post(f"/api/d/{sys.argv[2]}/{'refresh' if cmd == 'drefresh' else 'run'}", headers=H); print(cmd, r.status_code, r.text[:700])
elif cmd == "dsteps":
    r = c.get(f"/api/d/{sys.argv[2]}/queries_with_steps", headers=H)
    for q in r.json():
        st = q.get("default_step") or q.get("step") or {}
        print(q.get("title"), "| status:", st.get("status"), "| reason:", str(st.get("status_reason"))[:200], "| rows:", len(((st.get("data") or {}).get("rows")) or []))
elif cmd == "rename_conn":
    r = c.put(f"/api/connections/{sys.argv[2]}", json={"name": sys.argv[3]}, headers=H); print(r.status_code, r.json().get("name"))
elif cmd == "ownerrefresh":
    r = c.post(f"/api/artifacts/{sys.argv[2]}/refresh", headers=H); print(cmd, r.status_code, r.text[:900])
