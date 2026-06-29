"""
Mock "true DCR" MCP connector server (tier C) for sandbox verification.

Advertises the OAuth metadata an MCP client needs to connect WITHOUT any
admin-preregistered OAuth app:
  - GET /.well-known/oauth-protected-resource   (RFC 9728)
  - GET /.well-known/oauth-authorization-server (RFC 8414, incl. registration_endpoint)
  - POST /register   (RFC 7591 Dynamic Client Registration) -> issues client_id
  - GET  /authorize  (test shortcut: immediately issues an auth code)
  - POST /token      (authorization_code / refresh_token) -> issues access_token
  - /mcp             (FastMCP streamable-http) gated by the issued bearer token

The MCP tool surface is a Gmail-like connector (search_emails / send_email).

Run:
    MOCK_PORT=9302 PUBLIC_URL=http://localhost:9302 \
      uv run python tools/sandbox/connector-mocks/mock_mcp_dcr.py
"""
import os, secrets, time
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware

PUBLIC = os.environ.get("PUBLIC_URL", f"http://localhost:{os.environ.get('MOCK_PORT','9302')}")

# In-memory state (per process)
CLIENTS: dict = {}     # client_id -> {client_secret, redirect_uris}
CODES: dict = {}       # code -> {client_id}
TOKENS: dict = {}      # access_token -> {client_id, exp}

mcp = FastMCP("Mock Gmail (DCR)", stateless_http=True)

_EMAILS = [
    {"id": "e1", "from": "alice@corp.com", "subject": "Q2 numbers", "snippet": "Revenue up 8% MoM"},
    {"id": "e2", "from": "ci@github.com", "subject": "Build passed", "snippet": "All checks green"},
]

@mcp.tool()
def search_emails(query: str) -> list:
    """Search the mailbox; returns matching emails."""
    q = (query or "").lower()
    return [e for e in _EMAILS if q in (e["subject"] + e["snippet"]).lower()] or _EMAILS

@mcp.tool()
def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email (WRITE action — should be policy=confirm)."""
    return {"sent": True, "to": to, "subject": subject}


# ---- OAuth / DCR endpoints ----
async def protected_resource(request: Request):
    return JSONResponse({
        "resource": f"{PUBLIC}/mcp",
        "authorization_servers": [PUBLIC],
    })

async def as_metadata(request: Request):
    return JSONResponse({
        "issuer": PUBLIC,
        "authorization_endpoint": f"{PUBLIC}/authorize",
        "token_endpoint": f"{PUBLIC}/token",
        "registration_endpoint": f"{PUBLIC}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    })

async def register(request: Request):
    body = await request.json()
    client_id = "dcr-" + secrets.token_hex(8)
    client_secret = secrets.token_hex(16)
    CLIENTS[client_id] = {
        "client_secret": client_secret,
        "redirect_uris": body.get("redirect_uris", []),
    }
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": body.get("grant_types", ["authorization_code"]),
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "client_secret_post"),
    }, status_code=201)

async def authorize(request: Request):
    # Test shortcut: skip the consent UI, immediately issue a code.
    p = request.query_params
    client_id = p.get("client_id")
    redirect_uri = p.get("redirect_uri")
    state = p.get("state", "")
    if client_id not in CLIENTS:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    code = "code-" + secrets.token_hex(8)
    CODES[code] = {"client_id": client_id}
    sep = "&" if "?" in (redirect_uri or "") else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)

async def token(request: Request):
    form = await request.form()
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = form.get("code")
        if code not in CODES:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        del CODES[code]
    elif grant != "refresh_token":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    access = "at-" + secrets.token_hex(16)
    refresh = "rt-" + secrets.token_hex(16)
    TOKENS[access] = {"exp": time.time() + 3600}
    return JSONResponse({
        "access_token": access, "refresh_token": refresh,
        "token_type": "Bearer", "expires_in": 3600,
    })


class McpBearerGate(BaseHTTPMiddleware):
    """Gate only the /mcp endpoint on a valid issued access token."""
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/mcp"):
            auth = request.headers.get("authorization", "")
            tok = auth.split(" ", 1)[1] if auth.startswith("Bearer ") else None
            if not tok or tok not in TOKENS:
                return JSONResponse(
                    {"error": "invalid_token"}, status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource"'},
                )
        return await call_next(request)


def build_app():
    app = mcp.streamable_http_app()
    app.add_middleware(McpBearerGate)
    app.router.routes += [
        Route("/.well-known/oauth-protected-resource", protected_resource),
        Route("/.well-known/oauth-authorization-server", as_metadata),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize),
        Route("/token", token, methods=["POST"]),
    ]
    return app


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MOCK_PORT", "9302"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="warning")
