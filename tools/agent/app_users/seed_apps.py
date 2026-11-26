#!/usr/bin/env python3
"""Seed the app-user sandbox through the real API (no LLM): host applications,
published artifacts ("apps") allowed in them, and who each is shared with.

Run after seed_org.py (admin + member exist):

    cd backend && uv run python ../tools/agent/app_users/seed_apps.py > /tmp/apps.json

Creates, idempotently by name:

* host applications  "Acme Portal" (SSO popup, new people join as app users),
                     "Chinook Back Office" (host assertion, member attribute),
                     "Retired Intranet" (disabled);
* apps  "Sales Assistant"   organization-wide, in Acme Portal + Back Office
        "Customer Health"   shared with member@example.com, in Acme Portal
        "Inventory Lookup"  shared with nobody (admin only), in Acme Portal
        "Quarterly Review"  organization-wide, in NO host application
        "Support Desk"      organization-wide, only in the disabled host.

So the launcher of member@example.com must list exactly Sales Assistant and
Customer Health. Prints a JSON summary (ids, no tokens).
"""
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", os.environ.get("BOW_EMAIL", "admin@example.com"))
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", os.environ.get("BOW_PASSWORD", "Password123!"))
MEMBER = os.environ.get("BOW_MEMBER_EMAIL", "member@example.com")
PORTAL_ORIGIN = os.environ.get("PORTAL_ORIGIN", "http://localhost:4173")


def page(title: str, subtitle: str, accent: str, body: str) -> str:
    return f'''<script type="text/babel">
function App() {{
  return <div className="min-h-screen bg-slate-50 p-8 font-sans">
    <div className="mx-auto max-w-4xl">
      <div className="flex items-center gap-3">
        <div className="h-10 w-10 rounded-xl {accent}"></div>
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
          <p className="text-sm text-slate-500">{subtitle}</p>
        </div>
      </div>
      {body}
    </div>
  </div>;
}}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>'''


def bars(color: str) -> str:
    rows = [("Northwind", 92), ("Contoso", 78), ("Fabrikam", 64), ("Adventure Works", 51), ("Tailspin", 37)]
    items = "".join(
        f'<div className="flex items-center gap-3"><span className="w-36 text-sm text-slate-600">{name}</span>'
        f'<div className="h-3 flex-1 rounded-full bg-slate-100"><div className="h-3 rounded-full {color}" style={{{{width: "{value}%"}}}}></div></div>'
        f'<span className="w-10 text-end text-sm font-medium text-slate-800">{value}</span></div>'
        for name, value in rows)
    return f'<div className="mt-8 space-y-4 rounded-2xl bg-white p-6 shadow-sm">{items}</div>'


def tiles(color: str) -> str:
    stats = [("Open deals", "128"), ("Won this month", "$412k"), ("Avg. cycle", "23 days")]
    cells = "".join(
        f'<div className="rounded-2xl bg-white p-5 shadow-sm"><p className="text-xs uppercase tracking-wide text-slate-400">{k}</p>'
        f'<p className="mt-2 text-3xl font-semibold {color}">{v}</p></div>' for k, v in stats)
    return (f'<div className="mt-8 grid grid-cols-3 gap-4">{cells}</div>'
            '<div className="mt-4 rounded-2xl bg-white p-6 shadow-sm text-sm text-slate-600">'
            'Ask about any account, pipeline stage or rep to get started.</div>')


APPS = [
    {"title": "Sales Assistant", "description": "Ask about accounts, pipeline and reps",
     "code": page("Sales Assistant", "Pipeline at a glance", "bg-blue-500", tiles("text-blue-600")),
     "visibility": "internal", "hosts": ["Acme Portal", "Chinook Back Office"]},
    {"title": "Customer Health", "description": "Health score by account",
     "code": page("Customer Health", "Health score by account", "bg-emerald-500", bars("bg-emerald-500")),
     "visibility": "shared", "share_member": True, "hosts": ["Acme Portal"]},
    {"title": "Inventory Lookup", "description": "Stock by warehouse",
     "code": page("Inventory Lookup", "Stock by warehouse", "bg-amber-500", bars("bg-amber-500")),
     "visibility": "shared", "hosts": ["Acme Portal"]},
    {"title": "Quarterly Review", "description": "Not an app: allowed in no host application",
     "code": page("Quarterly Review", "Board pack", "bg-violet-500", bars("bg-violet-500")),
     "visibility": "internal", "hosts": []},
    {"title": "Support Desk", "description": "Only in a disabled host application",
     "code": page("Support Desk", "Tickets", "bg-rose-500", bars("bg-rose-500")),
     "visibility": "internal", "hosts": ["Retired Intranet"]},
]

HOSTS = [
    {"name": "Acme Portal", "identity_mode": "sso", "allowed_origins": [PORTAL_ORIGIN], "new_user_role": "app_user"},
    {"name": "Chinook Back Office", "identity_mode": "assertion", "identity_source": "member_attribute:erp_id",
     "issuer": "chinook-backoffice", "allowed_origins": [PORTAL_ORIGIN]},
    {"name": "Retired Intranet", "identity_mode": "sso", "allowed_origins": [PORTAL_ORIGIN], "enabled": False},
]

client = httpx.Client(base_url=BASE, timeout=120)
login = client.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD})
if login.status_code != 200:
    sys.exit(f"admin login failed: {login.status_code} {login.text}")
H = {"Authorization": "Bearer " + login.json()["access_token"]}
org = client.get("/api/organizations", headers=H).json()[0]["id"]
H["X-Organization-Id"] = org


def call(method, path, body=None, ok=(200, 201, 202, 204)):
    response = client.request(method, path, json=body, headers=H)
    if response.status_code not in ok:
        sys.exit(f"{method} {path} -> {response.status_code} {response.text[:400]}")
    return response.json() if response.content else None


hosts = {h["name"]: h for h in call("GET", "/api/host_applications")}
keys = {}
for spec in HOSTS:
    if spec["name"] not in hosts:
        created = call("POST", "/api/host_applications", spec)
        hosts[spec["name"]] = created
        if created.get("private_key"):
            keys[spec["name"]] = created["private_key"]

members = call("GET", f"/api/organizations/{org}/members")
member = next((m for m in members if (m.get("user") or {}).get("email") == MEMBER), None)
member_user_id = (member or {}).get("user", {}).get("id")

existing = {}
page_no = 1
listed = call("GET", "/api/artifacts?filter=my&limit=100")
for card in listed.get("items", listed.get("dashboards", [])):
    existing[card.get("title")] = card.get("id")

out = []
for spec in APPS:
    artifact_id = existing.get(spec["title"])
    if not artifact_id:
        report = call("POST", "/api/reports", {"title": spec["title"]})
        artifact = call("POST", "/api/artifacts", {
            "report_id": report["id"], "title": spec["title"], "mode": "page",
            "content": {"code": spec["code"], "runtime_version": 11},
        })
        artifact_id = artifact["artifact_id"]
        call("POST", f"/api/artifacts/{artifact_id}/publish", {})
        for _ in range(240):
            if call("GET", f"/api/artifacts/{artifact_id}/publish").get("published"):
                break
            time.sleep(.5)
        else:
            sys.exit(f"{spec['title']} did not publish")
    sharing = {"visibility": spec["visibility"]}
    if spec["visibility"] == "shared":
        sharing["user_ids"] = [member_user_id] if spec.get("share_member") and member_user_id else []
    call("PUT", f"/api/artifacts/{artifact_id}/sharing", sharing)
    call("PUT", f"/api/artifacts/{artifact_id}/embedding",
         {"host_application_ids": [hosts[name]["id"] for name in spec["hosts"]]})
    out.append({"title": spec["title"], "id": artifact_id, "visibility": spec["visibility"], "hosts": spec["hosts"]})

print(json.dumps({
    "organization_id": org,
    "member": {"email": MEMBER, "user_id": member_user_id, "membership_id": (member or {}).get("id")},
    "hosts": {name: {"id": h["id"], "public_id": h["public_id"], "identity_mode": h["identity_mode"],
                     "enabled": h["enabled"], "new_user_role": h.get("new_user_role")} for name, h in hosts.items()},
    "private_keys": keys,
    "apps": out,
}, indent=2))
