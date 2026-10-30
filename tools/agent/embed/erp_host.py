#!/usr/bin/env python3
"""A stand-in customer application ("Chinook Back Office") on its OWN origin,
embedding BOW artifacts the way a real integration does, and nothing more:

* its pages load the SDK from the BOW origin and mount artifacts;
* it knows who is signed in (an ``erp_user`` cookie set by its sign-in page);
* ``POST /bow/assertion`` signs a 60-second RS256 JWT for THAT user with the
  host application's private key (never sent to the browser).

Pages: ``/`` customers + the embedded account dashboard (inline);
``/assistant`` a chat-assistant artifact (inline); ``/signin?user=…`` switches
user; ``?locale=he`` passes a locale to the embed.

Run:
  BOW_HOST_APP=host_… BOW_ARTIFACT=account-managers BOW_ASSISTANT=… \\
  BOW_HOST_PRIVATE_KEY_FILE=/tmp/bow-agent/host-private.pem \\
  uv run python ../tools/agent/embed/erp_host.py --port 4173
"""
from __future__ import annotations

import argparse
import html
import os
import time
import uuid

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI()

BOW_ORIGIN = os.environ.get("BOW_ORIGIN", "http://localhost:3000")
HOST_APP = os.environ.get("BOW_HOST_APP", "")
ISSUER = os.environ.get("BOW_HOST_ISSUER", "chinook-backoffice")
KEY_FILE = os.environ.get("BOW_HOST_PRIVATE_KEY_FILE", "/tmp/bow-agent/host-private.pem")
DASHBOARD = os.environ.get("BOW_ARTIFACT", "account-managers")
ASSISTANT = os.environ.get("BOW_ASSISTANT", "")
# A second host application in SSO mode: no backend step, people sign in to BOW.
SSO_HOST_APP = os.environ.get("BOW_SSO_HOST_APP", "")

# The back office's own directory; employee_id is what BOW maps to a member.
USERS = {
    "alice": {"name": "Alice Nguyen", "employee_id": "E-1001", "role": "Account manager"},
    "bob": {"name": "Bob Okafor", "employee_id": "E-1002", "role": "Account manager"},
    "carol": {"name": "Carol Díaz", "employee_id": "E-9999", "role": "Contractor (no BOW account)"},
}
CUSTOMERS = [("C-14", "Mark Philips", "Canada"), ("C-25", "Victor Stevens", "USA"),
             ("C-6", "Helena Holý", "Czech Republic"), ("C-57", "Luis Rojas", "Chile")]


def _user(request: Request):
    user = request.cookies.get("erp_user")
    return user if user in USERS else None


@app.post("/bow/assertion")
async def assertion(request: Request):
    """Trusts this app's own session (the cookie), never a user id from the page."""
    user = _user(request)
    if user is None:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    now = int(time.time())
    with open(KEY_FILE) as fh:
        key = fh.read()
    token = jwt.encode({"iss": ISSUER, "aud": HOST_APP, "sub": USERS[user]["employee_id"],
                        "iat": now, "exp": now + 60, "jti": uuid.uuid4().hex}, key, algorithm="RS256")
    return JSONResponse({"assertion": token})


@app.get("/signin")
async def signin(user: str, next: str = "/"):
    resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    if user in USERS:
        resp.set_cookie("erp_user", user, httponly=True, samesite="lax")
    else:
        resp.delete_cookie("erp_user")
    return resp


STYLE = """
:root{--bg:#f4f5f7;--panel:#fff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--brand:#7c3aed}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--ink);display:flex;min-height:100vh}
nav{width:210px;background:#1e1b4b;color:#c7d2fe;padding:18px 12px}nav .logo{font-weight:700;color:#fff;margin-bottom:20px}
nav a{display:block;padding:8px 10px;border-radius:8px;color:#c7d2fe;text-decoration:none}nav a.active{background:#312e81;color:#fff}
main{flex:1;padding:18px 24px;display:flex;flex-direction:column;gap:14px;min-width:0}
header{display:flex;align-items:center;justify-content:space-between}header h1{font-size:18px;margin:0}
.who{color:var(--muted);display:flex;gap:8px;align-items:center}.who a{color:var(--brand)}
.grid{display:grid;grid-template-columns:300px 1fr;gap:14px;align-items:start}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card h2{font-size:13px;margin:0;padding:10px 14px;border-bottom:1px solid var(--line);color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
table{width:100%;border-collapse:collapse}td{padding:9px 14px;border-bottom:1px solid var(--line)}tr{cursor:pointer}tr.sel{background:#ede9fe}
.embed{height:780px}.status{font-size:12px;color:var(--muted)}
"""


def _page(request: Request, active: str, body: str, script: str) -> HTMLResponse:
    user = _user(request)
    if user is None:
        links = " · ".join(f'<a data-testid="signin-{k}" href="/signin?user={k}&next={request.url.path}">{html.escape(v["name"])}</a>'
                           for k, v in USERS.items())
        return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8><title>Chinook Back Office</title><style>{STYLE}</style></head>"
                            f"<body><main><h1>Chinook Back Office — sign in</h1><p>{links}</p></main></body></html>")
    u = USERS[user]
    nav = "".join(f'<a class="{"active" if k == active else ""}" href="{href}">{label}</a>'
                  for k, href, label in (("customers", "/", "Customers"), ("assistant", "/assistant", "Sales assistant"),
                                         ("orders", "#", "Orders"), ("inventory", "#", "Inventory")))
    locale = request.query_params.get("locale", "")
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Chinook Back Office</title><style>{STYLE}</style></head><body>
<nav><div class="logo">Chinook Back Office</div>{nav}</nav>
<main><header><h1>{'Customers' if active == 'customers' else 'Sales assistant'}</h1>
<div class="who" data-testid="erp-user">{html.escape(u['name'])} · {u['role']} · <a href="/signin?user=&next=/" data-testid="erp-signout">Sign out</a></div></header>
{body}<div class="status" data-testid="bow-status">BOW: starting</div></main>
<script src="{BOW_ORIGIN}/api/embed/sdk/v1/bow-embed.js"></script>
<script>
const getAssertion = () => fetch('/bow/assertion', {{ method: 'POST' }}).then(r => r.json()).then(b => b.assertion);
const status = document.querySelector('[data-testid=bow-status]');
function track(embed) {{
  embed.on('authenticated', ({{ user }}) => {{ status.textContent = 'BOW: signed in as ' + (user && user.name); }});
  embed.on('authRequired', (e) => {{ status.textContent = 'BOW: ' + e.status + (e.message ? ' — ' + e.message : ''); }});
  embed.on('error', (e) => {{ status.textContent = 'BOW error: ' + e.message; }});
  embed.on('unavailable', (e) => {{ window.bowUnavailable = e; status.textContent = 'BOW: unavailable (' + e.reason + ')'; }});
  window.embed = embed;
  return embed;
}}
const LOCALE = {locale!r} || null;
{script}
</script></body></html>""")


@app.get("/", response_class=HTMLResponse)
async def customers(request: Request):
    rows = "".join(f'<tr data-testid="customer-{cid}" onclick="pick(this, \'{cid}\')"><td>{html.escape(n)}</td><td>{c}</td></tr>'
                   for cid, n, c in CUSTOMERS)
    body = f'<div class="grid"><div class="card"><h2>Customers</h2><table>{rows}</table></div>' \
           f'<div class="card embed" id="bow-dashboard" data-testid="erp-embed"></div></div>'
    script = f"""
track(BOWEmbed.mount({{ baseUrl: '{BOW_ORIGIN}', hostApp: '{HOST_APP}', artifact: '{DASHBOARD}',
  container: '#bow-dashboard', identity: {{ getAssertion }}, locale: LOCALE, title: 'Account dashboard' }}));
function pick(row, id) {{
  document.querySelectorAll('tr').forEach(r => r.classList.remove('sel')); row.classList.add('sel');
  window.embed.updateContext({{ customerId: id, customerName: row.cells[0].textContent }});
}}"""
    return _page(request, "customers", body, script)


@app.get("/assistant", response_class=HTMLResponse)
async def assistant(request: Request):
    body = '<div class="card embed" id="bow-assistant" data-testid="erp-assistant"></div>'
    script = f"""
track(BOWEmbed.mount({{ baseUrl: '{BOW_ORIGIN}', hostApp: '{HOST_APP}', artifact: '{ASSISTANT}',
  container: '#bow-assistant', identity: {{ getAssertion }}, locale: LOCALE, title: 'Sales assistant' }}));"""
    return _page(request, "assistant", body, script)


@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request):
    """The dashboard through an SSO-mode host application: no getAssertion."""
    body = '<div class="card embed" id="bow-portal" data-testid="erp-portal"></div>'
    script = f"""
track(BOWEmbed.mount({{ baseUrl: '{BOW_ORIGIN}', hostApp: '{SSO_HOST_APP}', artifact: '{DASHBOARD}',
  container: '#bow-portal', locale: LOCALE, title: 'Account dashboard' }}));"""
    return _page(request, "customers", body, script)


if __name__ == "__main__":
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=4173)
    uvicorn.run(app, host="127.0.0.1", port=p.parse_args().port, log_level="warning")
