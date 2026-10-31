"""Customer-style ERP with real Entra sign-in and a floating BOW assistant.

Run from repository root with the backend Python: python -m tools.agent.embed.erp_entra
Uses entra_exchange_demo's authorization-code + PKCE + BOW RFC8693 exchange.
All credentials are environment variables; session/token storage is memory-only.
Loopback development harness, not a production ERP/session implementation.
"""

import html
import json
import os
import time
import uuid

import httpx
import jwt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from tools.agent import entra_exchange_demo as auth

app = FastAPI()
app.add_api_route("/login", auth.login, methods=["GET"])
app.add_api_route("/entra/callback", auth.callback, methods=["GET"])
HOST = os.environ["BOW_HOST_APP"]
ARTIFACT = os.environ["BOW_ASSISTANT"]
KEY = os.environ["BOW_HOST_PRIVATE_KEY"]
ISSUER = "chinook-entra-erp"


@app.post("/bow/assertion")
async def assertion(request: Request):
    s = auth.session(request)
    if not s.get("user") or not s.get("subject"):
        raise HTTPException(401, "Sign in to the ERP first")
    # Recheck the server-held BOW exchange token before minting another
    # assertion. BOW separately enforces host admission and membership.
    async with httpx.AsyncClient(timeout=15) as client:
        result = await client.get(
            auth.BASE + "/api/users/whoami",
            headers={"Authorization": "Bearer " + s["token"]},
        )
    if result.status_code != 200:
        raise HTTPException(401, "Sign in again to renew delegated access")
    now = int(time.time())
    value = jwt.encode(
        {
            "iss": ISSUER,
            "aud": HOST,
            "sub": s["subject"],
            "iat": now,
            "exp": now + 60,
            "jti": uuid.uuid4().hex,
        },
        KEY,
        algorithm="RS256",
    )
    return JSONResponse({"assertion": value}, headers={"Cache-Control": "no-store"})


@app.post("/logout")
async def logout(request: Request):
    auth.SESSIONS.pop(request.cookies.get("entra_demo", ""), None)
    response = JSONResponse({"signed_out": True})
    response.delete_cookie("entra_demo")
    return response


@app.get("/")
async def home(request: Request):
    try:
        s = auth.session(request)
    except HTTPException:
        s = {}
    user = s.get("user")
    status = html.escape(s.get("status", "Sign into your ERP account to continue."))
    name = html.escape((user or {}).get("name") or (user or {}).get("email") or "Guest")
    config = json.dumps(
        {
            "baseUrl": auth.BASE,
            "hostApp": HOST,
            "artifact": ARTIFACT,
            "mode": "floating",
            "title": "Music Store Assistant",
            "context": {"customerId": "14", "page": "Customers"},
        }
    ).replace("<", "\\u003c")
    script = (
        ""
        if not user
        else f"""
<script src="{html.escape(auth.BASE)}/api/embed/sdk/v1/bow-embed.js"></script>
<script>
const widget = BOWEmbed.mount({{...{config}, identity: {{getAssertion: async () => {{
 const r = await fetch('/bow/assertion', {{method:'POST'}});
 if (!r.ok) throw new Error('Please sign into the ERP again');
 return (await r.json()).assertion;
}}}}}});
window.widget = widget;
widget.on('authenticated', () => document.querySelector('#status').textContent = 'Assistant connected');
widget.on('error', e => document.querySelector('#status').textContent = e.message);
document.querySelector('#logout').onclick = async () => {{ widget.logout(); await fetch('/logout', {{method:'POST'}}); location.href='/'; }};
document.querySelectorAll('[data-customer]').forEach(row => row.onclick = () => {{
 document.querySelectorAll('tr').forEach(r => r.classList.remove('selected'));
 row.classList.add('selected');
 widget.updateContext({{customerId:row.dataset.customer, customerName:row.cells[0].textContent, page:'Customers'}});
}});
</script>"""
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chinook ERP · Customer workspace</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:#18243a;font:15px/1.5 system-ui}}nav{{background:#17243d;color:#fff;width:220px;position:fixed;inset:0 auto 0 0;padding:30px 20px}}nav strong{{font-size:22px}}nav p{{padding:12px;border-radius:8px}}nav .active{{background:#304564}}main{{margin-left:220px;padding:32px 40px}}header{{display:flex;justify-content:space-between;align-items:center}}h1{{font-size:28px;margin-bottom:6px}}.muted{{color:#64748b}}.cards{{display:flex;gap:20px;margin:25px 0}}.card,section{{background:white;border:1px solid #dfe5ed;border-radius:12px;padding:24px}}.card{{flex:1}}.card strong{{font-size:26px;display:block}}table{{width:100%;border-collapse:collapse}}th,td{{padding:15px;text-align:left;border-bottom:1px solid #e7ebf1}}tbody tr{{cursor:pointer}}tr.selected{{background:#edf2ff}}a,button{{background:#375ada;color:white;border:0;border-radius:7px;padding:10px 16px;text-decoration:none;cursor:pointer}}pre{{white-space:pre-wrap;font:12px system-ui}}#status{{margin-top:22px;color:#64748b}}
</style></head><body><nav><strong>Chinook ERP</strong><p>Overview</p><p class="active">Customers</p><p>Orders</p><p>Music catalog</p></nav><main>
<header><div><div class="muted">SALES / CUSTOMER WORKSPACE</div><h1>Customers</h1></div><div>{name} · {('<button id="logout">Sign out</button>' if user else '<a href="/login">Sign in with Microsoft</a>')}</div></header>
<p class="muted">Your customer workspace, with a music store assistant one click away.</p>
<div class="cards"><div class="card">Accounts in view<strong>3</strong></div><div class="card">Workspace<strong>Music store</strong></div><div class="card">Identity<strong>Microsoft Entra</strong></div></div>
<section><h2>Customer directory</h2><table><thead><tr><th>Customer</th><th>Country</th><th>Account</th></tr></thead><tbody><tr data-customer="14" class="selected"><td>Mark Philips</td><td>Canada</td><td>C-14</td></tr><tr data-customer="25"><td>Victor Stevens</td><td>USA</td><td>C-25</td></tr><tr data-customer="6"><td>Helena Holý</td><td>Czech Republic</td><td>C-6</td></tr></tbody></table></section>
<div id="status">{"Assistant starting…" if user else status}</div></main>{script}</body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app, host="127.0.0.1", port=int(os.getenv("ERP_PORT", "4173")), access_log=False
    )
