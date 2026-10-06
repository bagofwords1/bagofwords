"""Local confidential-client demo: Entra sign-in -> BOW token exchange.

Run with the backend virtualenv. Secrets come from environment or hidden prompts.
This binds loopback only; tokens live in memory and are never rendered or logged.
See docs/feedback-loops/external-entra-exchange.md for setup.
"""

import base64
import getpass
import hashlib
import html
import os
import secrets
import time
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()
BASE = os.environ.get("BOW_URL", "http://localhost:3017").rstrip("/")
TENANT = str(UUID(os.environ["ENTRA_TENANT_ID"]))
EXTERNAL = str(UUID(os.environ["EXTERNAL_ENTRA_CLIENT_ID"]))
AUDIENCE = str(UUID(os.environ["BOW_ENTRA_CLIENT_ID"]))
EXTERNAL_SECRET = os.environ.get("EXTERNAL_ENTRA_SECRET") or getpass.getpass("External Entra secret: ")
CLIENT = os.environ["BOW_CLIENT_ID"]
SECRET = os.environ.get("BOW_CLIENT_SECRET") or getpass.getpass("BOW custom app secret: ")
CALLBACK = "http://localhost:3000/entra/callback"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
SESSIONS = {}
PAGE = """<!doctype html><html><head><title>External Entra → BOW</title>
<style>body{font:17px system-ui;max-width:820px;margin:60px auto;background:#f5f5fa;color:#202030}main{padding:32px;background:white;border-radius:16px}a,button{display:inline-block;padding:12px 18px;background:#5141d9;color:white;border:0;border-radius:7px;font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#666}</style></head><body><main>
<h1>External Entra application → BOW</h1><p>Sign in with Microsoft once. This server exchanges your delegated Entra token for a BOW token.</p><p><a href="/login">Sign in with Microsoft</a></p><div>STATUS</div><small>Local test only. Credentials and tokens remain on this server.</small></main></body></html>"""


def session(request):
    s = SESSIONS.get(request.cookies.get("entra_demo", ""))
    if not s or time.time() - s["started"] > 3600:
        raise HTTPException(401, "Start a new sign-in")
    return s


@app.get("/")
async def home(request: Request):
    s = SESSIONS.get(request.cookies.get("entra_demo", ""), {})
    status = html.escape(s.get("status", "Not signed in."))
    return HTMLResponse(PAGE.replace("STATUS", f"<pre>{status}</pre>"), headers={"Cache-Control": "no-store"})


@app.get("/login")
async def login():
    now = time.time()
    for key in list(SESSIONS):
        if now - SESSIONS[key]["started"] > 3600:
            del SESSIONS[key]
    if len(SESSIONS) >= 100:
        raise HTTPException(503, "Restart the demo to clear sessions")
    sid, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    SESSIONS[sid] = {"state": state, "verifier": verifier, "started": now}
    query = {
        "client_id": EXTERNAL,
        "response_type": "code",
        "redirect_uri": CALLBACK,
        "scope": f"openid profile email api://{AUDIENCE}/access_as_user",
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
    }
    response = RedirectResponse(AUTHORITY + "/authorize?" + urlencode(query))
    response.set_cookie("entra_demo", sid, httponly=True, samesite="lax", max_age=3600)
    return response


@app.get("/entra/callback")
async def callback(request: Request):
    s = session(request)
    expected = s.pop("state", None)
    if (
        not expected
        or not secrets.compare_digest(expected, request.query_params.get("state", ""))
        or time.time() - s["started"] > 600
    ):
        raise HTTPException(400, "Invalid or expired state")
    if request.query_params.get("error"):
        s["status"] = "Microsoft sign-in was not completed."
        return RedirectResponse("/", status_code=303)
    async with httpx.AsyncClient(timeout=150) as client:
        result = await client.post(
            AUTHORITY + "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": EXTERNAL,
                "client_secret": EXTERNAL_SECRET,
                "redirect_uri": CALLBACK,
                "code": request.query_params.get("code", ""),
                "code_verifier": s.pop("verifier"),
            },
        )
        if result.status_code != 200:
            s["status"] = (
                f"Microsoft token request failed (HTTP {result.status_code}, codes {result.json().get('error_codes', [])})."
            )
            return RedirectResponse("/", status_code=303)
        assertion = result.json()["access_token"]
        result = await client.post(
            BASE + "/api/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "subject_token": assertion,
                "client_id": CLIENT,
                "client_secret": SECRET,
                "scope": "app",
            },
        )
        if result.status_code != 200:
            payload = result.json()
            s["status"] = (
                f"BOW exchange failed (HTTP {result.status_code}): {payload.get('error')} — {payload.get('error_description')}"
            )
        else:
            s["token"] = result.json()["access_token"]
            identity = await client.get(BASE + "/api/users/whoami", headers={"Authorization": "Bearer " + s["token"]})
            s["status"] = (
                f"Microsoft sign-in succeeded.\nBOW exchange succeeded (HTTP 200).\nAuthenticated BOW API request: HTTP {identity.status_code}.\nToken lifetime: {result.json()['expires_in']} seconds."
            )
            if identity.status_code == 200:
                s["status"] += "\nUser: " + identity.json().get("email", "")
    return RedirectResponse("/", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=3000, access_log=False)
