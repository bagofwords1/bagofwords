"""Seed a report with a page-mode artifact through the real API (for the
in-app rendering check). Prints {"report_id", "artifact_id"}.

    python seed_artifact.py http://127.0.0.1:18431 admin@example.com
"""
import json
import sys

import httpx

BASE, EMAIL = sys.argv[1], sys.argv[2]
c = httpx.Client(base_url=BASE, timeout=30)
tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": "Password123!"}).json()["access_token"]
org = c.get("/api/users/whoami", headers={"Authorization": f"Bearer {tok}"}).json()["organizations"][0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
# Skip the onboarding flow so the report page is reachable in the UI.
c.put("/api/organization/onboarding", json={"dismissed": True, "completed": True}, headers=H)
r = c.post("/api/reports", json={"title": "Artifact render check", "data_sources": []}, headers=H)
assert r.status_code == 200, r.text
rid = r.json()["id"]
# Stored artifact code is a <script type="text/babel"> block (what create_artifact writes).
code = (
    "<script type=\"text/babel\">\n"
    "function App(){ return (<div className='p-6'><h1 id='inapp-ok' className='text-2xl font-bold text-emerald-700'>"
    "In-app artifact renders</h1><p className='text-slate-600'>srcdoc iframe built by utils/artifactIframe.ts</p>"
    "</div>); }\nReactDOM.createRoot(document.getElementById('root')).render(<App/>);\n</script>"
)
r = c.post("/api/artifacts", json={"report_id": rid, "title": "Render check", "mode": "page", "content": {"code": code}},
           headers=H)
assert r.status_code == 200, r.text
print(json.dumps({"report_id": rid, "artifact_id": r.json()["id"], "org_id": org}))
