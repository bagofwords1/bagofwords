#!/usr/bin/env python3
"""A stand-in customer application ("Northwind ERP") on its OWN origin.

What a real integration has to do, and nothing more:

* serve pages that load the SDK from the BOW origin and mount the assistant;
* know who is signed in (here: an ``erp_user`` cookie set by the user switcher);
* expose ``POST /internal/bow/assertion`` that signs a short-lived RS256 JWT
  for that user with the embed's private key (``BOW_EMBED_PRIVATE_KEY_FILE``,
  ``BOW_EMBED_ID``, ``BOW_EMBED_ISSUER``). The key never reaches the browser.

Pages: ``/`` (floating launcher over an incidents list; clicking a row
updates the assistant's context), ``/inline?user=…`` (inline panel), both with
``?theme=dark`` and ``?locale=he`` variants to exercise appearance options.

Run:  cd backend && uv run python ../tools/agent/embed/erp_host.py --port 4173
"""
from __future__ import annotations

import argparse
import html
import os
import time
import uuid

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

BOW_ORIGIN = os.environ.get("BOW_ORIGIN", "http://localhost:3000")
EMBED_ID = os.environ.get("BOW_EMBED_ID", "")
ISSUER = os.environ.get("BOW_EMBED_ISSUER", "erp-sandbox")
KEY_FILE = os.environ.get("BOW_EMBED_PRIVATE_KEY_FILE", "/tmp/bow-agent/embed-private.pem")

# The ERP's own user directory. ``employee_id`` is what BOW maps to a member
# (Membership.custom_attributes["employee_id"]). carol has no mapping in BOW.
USERS = {
    "alice": {"name": "Alice Nguyen", "employee_id": "E-1001", "role": "Plant manager"},
    "bob": {"name": "Bob Okafor", "employee_id": "E-1002", "role": "Shift supervisor"},
    "carol": {"name": "Carol Díaz", "employee_id": "E-9999", "role": "Contractor (no BOW account)"},
}

INCIDENTS = [
    ("INC-1042", "Line 3 conveyor jam", "High", "Open"),
    ("INC-1043", "Packaging label misprint", "Medium", "In progress"),
    ("INC-1044", "Cold room temperature drift", "High", "Open"),
    ("INC-1045", "Forklift battery recall", "Low", "Closed"),
]


def _private_key() -> str:
    with open(KEY_FILE) as fh:
        return fh.read()


def _current_user(request: Request):
    user = request.query_params.get("user") or request.cookies.get("erp_user") or "alice"
    return user if user in USERS else "alice"


@app.post("/internal/bow/assertion")
async def assertion(request: Request):
    """The one backend endpoint a customer adds. It trusts ITS OWN session
    (the cookie), never a user id posted by the browser."""
    user = request.cookies.get("erp_user")
    if user not in USERS:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    now = int(time.time())
    token = jwt.encode({
        "iss": ISSUER, "aud": EMBED_ID, "sub": USERS[user]["employee_id"],
        "iat": now, "exp": now + 300, "jti": uuid.uuid4().hex,
        "name": USERS[user]["name"],  # ignored by BOW, shows that extra claims grant nothing
    }, _private_key(), algorithm="RS256")
    return JSONResponse({"assertion": token})


def _page(request: Request, inline: bool) -> HTMLResponse:
    user = _current_user(request)
    theme = "dark" if request.query_params.get("theme") == "dark" else "light"
    locale = request.query_params.get("locale", "")
    u = USERS[user]
    rows = "".join(
        f'<tr data-testid="erp-row-{rid}" data-record="{rid}" onclick="selectIncident(this)">'
        f'<td class="mono">{rid}</td><td>{html.escape(title)}</td><td><span class="pill {sev.lower()}">{sev}</span></td><td>{status}</td></tr>'
        for rid, title, sev, status in INCIDENTS
    )
    switcher = "".join(
        f'<option value="{k}" {"selected" if k == user else ""}>{html.escape(v["name"])} · {v["employee_id"]}</option>'
        for k, v in USERS.items()
    )
    mount_mode = "inline" if inline else "floating"
    container = '<div id="assistant" class="inline-assistant" data-testid="erp-inline-container"></div>' if inline else ""
    sdk = f"{BOW_ORIGIN}/api/embed/sdk/v1/bow-embed.js"
    body = f"""<!doctype html>
<html lang="en" data-theme="{theme}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Northwind ERP — Incidents</title>
<style>
  :root {{ --bg:#f3f4f6; --panel:#fff; --ink:#111827; --muted:#6b7280; --line:#e5e7eb; --brand:#0f766e; }}
  [data-theme=dark] {{ --bg:#0b1220; --panel:#111a2e; --ink:#e5e7eb; --muted:#94a3b8; --line:#1f2a44; }}
  * {{ box-sizing:border-box }} body {{ margin:0; font:14px/1.45 system-ui, Segoe UI, sans-serif; background:var(--bg); color:var(--ink); display:flex; min-height:100vh }}
  nav {{ width:220px; background:#0f172a; color:#cbd5e1; padding:20px 14px }} nav .logo {{ font-weight:700; color:#fff; letter-spacing:.3px; margin-bottom:22px }}
  nav a {{ display:block; padding:8px 10px; border-radius:8px; color:#cbd5e1; text-decoration:none }} nav a.active {{ background:#1e293b; color:#fff }}
  main {{ flex:1; padding:22px 28px; display:flex; flex-direction:column; gap:16px }}
  header {{ display:flex; align-items:center; justify-content:space-between }} header h1 {{ font-size:18px; margin:0 }}
  .user {{ display:flex; align-items:center; gap:10px; color:var(--muted) }} select, button {{ font:inherit; padding:6px 10px; border-radius:8px; border:1px solid var(--line); background:var(--panel); color:var(--ink) }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden }}
  table {{ width:100%; border-collapse:collapse }} th, td {{ text-align:left; padding:10px 14px; border-bottom:1px solid var(--line) }} th {{ color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase }}
  tbody tr {{ cursor:pointer }} tbody tr:hover {{ background:rgba(15,118,110,.06) }} tbody tr.selected {{ background:rgba(15,118,110,.14) }}
  .mono {{ font-family:ui-monospace, SFMono-Regular, Menlo, monospace }}
  .pill {{ padding:2px 8px; border-radius:999px; font-size:12px; background:#e5e7eb; color:#111 }} .pill.high {{ background:#fee2e2; color:#991b1b }} .pill.medium {{ background:#fef3c7; color:#92400e }}
  .status {{ color:var(--muted); font-size:12px }} .inline-assistant {{ height:560px; max-width:420px; }}
  .grid {{ display:grid; grid-template-columns: 1fr 420px; gap:16px; align-items:start }}
</style>
</head>
<body>
<nav><div class="logo">Northwind ERP</div><a href="#">Dashboard</a><a class="active" href="#">Incidents</a><a href="#">Work orders</a><a href="#">Inventory</a><a href="#">Reports</a></nav>
<main>
  <header>
    <h1>Incidents</h1>
    <div class="user">
      <span data-testid="erp-user">{html.escape(u["name"])} · {u["role"]}</span>
      <select data-testid="erp-user-switch" onchange="switchUser(this.value)">{switcher}</select>
      <button type="button" data-testid="erp-signout" onclick="signOut()">Sign out</button>
    </div>
  </header>
  <div class="{'grid' if inline else ''}">
    <div class="card">
      <table>
        <thead><tr><th>ID</th><th>Title</th><th>Severity</th><th>Status</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    {container}
  </div>
  <div class="status" data-testid="erp-status">Assistant: <span id="assistant-status">loading…</span> · selected: <span id="selected">none</span></div>
</main>
<script src="{sdk}"></script>
<script>
  document.cookie = "erp_user={user}; path=/; SameSite=Lax";
  const statusEl = document.getElementById('assistant-status');
  const assistant = BOWEmbed.mount({{
    baseUrl: "{BOW_ORIGIN}",
    embedId: "{EMBED_ID}",
    mode: "{mount_mode}",
    {"container: '#assistant'," if inline else ""}
    theme: "{theme}",
    {"locale: '" + html.escape(locale) + "'," if locale else ""}
    getAssertion: async () => {{
      const res = await fetch("/internal/bow/assertion", {{ method: "POST" }});
      if (!res.ok) throw new Error("ERP could not sign you in to the assistant");
      return (await res.json()).assertion;
    }},
    context: {{ page: "incidents", recordType: "incident" }},
    metadata: {{ workflow_id: "incident-review", erp_user: "{user}" }},
  }});
  window.assistant = assistant;
  window.__bow_events = [];
  ['ready','open','close','authenticated','authRequired','error','loggedOut'].forEach(ev =>
    assistant.on(ev, (d) => {{ window.__bow_events.push({{ ev, d }}); statusEl.textContent = ev + (d && d.user && d.user.name ? ' (' + d.user.name + ')' : ''); }}));
  function selectIncident(tr) {{
    document.querySelectorAll('tbody tr').forEach(r => r.classList.remove('selected'));
    tr.classList.add('selected');
    const id = tr.dataset.record;
    document.getElementById('selected').textContent = id;
    assistant.updateContext({{ page: "incidents", recordType: "incident", recordId: id, title: tr.children[1].textContent }});
  }}
  function switchUser(next) {{
    // A real ERP ends the assistant session before the next person signs in.
    assistant.logout();
    setTimeout(() => {{ location.href = location.pathname + '?user=' + next + (location.search.includes('theme=dark') ? '&theme=dark' : ''); }}, 150);
  }}
  function signOut() {{ assistant.logout(); statusEl.textContent = 'signed out'; }}
</script>
</body></html>"""
    return HTMLResponse(body)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _page(request, inline=False)


@app.get("/inline", response_class=HTMLResponse)
async def inline(request: Request):
    return _page(request, inline=True)


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
