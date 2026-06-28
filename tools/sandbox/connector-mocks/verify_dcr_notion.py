"""
Verify the backend's outbound MCP DCR client against the REAL Notion MCP server.

Exercises the actual backend functions (no mocks) end-to-end up to the user
consent step (which is inherently interactive):
  discover -> dynamically register -> persist config -> build authorize URL.

Run from backend/:  uv run python ../tools/sandbox/connector-mocks/verify_dcr_notion.py
"""
import asyncio, json, sys
from urllib.parse import urlsplit, parse_qs

NOTION = "https://mcp.notion.com/mcp"
R = []
def ck(name, ok, d=""):
    ok = bool(ok); R.append(ok)
    print(f"  [{'PASS ✅' if ok else 'FAIL ❌'}] {name}" + (f" — {d}" if d else ""))


class StubConn:
    """Minimal Connection stand-in for ensure_mcp_oauth_config / get_oauth_params."""
    def __init__(self):
        self.id = "stub-notion"
        self.type = "mcp"
        self.config = json.dumps({"server_url": NOTION, "transport": "streamable_http"})
        self._creds = {}
    def decrypt_credentials(self):
        return dict(self._creds)
    def encrypt_credentials(self, payload):
        self._creds = dict(payload)


class StubDB:
    async def commit(self): pass
    async def refresh(self, _obj): pass


async def main():
    from app.services.mcp_dcr_service import discover_mcp_oauth, register_client, ensure_mcp_oauth_config, _redirect_uri
    from app.services.connection_oauth_service import get_oauth_params, generate_pkce_pair

    print("=== Outbound DCR vs REAL Notion MCP ===")

    # 1) Discovery (RFC 9728 -> 8414)
    meta = await discover_mcp_oauth(NOTION)
    ck("discovered authorize_url", "notion.com/authorize" in (meta.get("authorize_url") or ""), meta.get("authorize_url"))
    ck("discovered token_url", bool(meta.get("token_url")), meta.get("token_url"))
    ck("discovered registration_endpoint", bool(meta.get("registration_endpoint")), meta.get("registration_endpoint"))
    ck("discovered resource (audience)", bool(meta.get("resource")), meta.get("resource"))

    # 2) Dynamic Client Registration (RFC 7591) — live against Notion
    reg = await register_client(meta["registration_endpoint"], _redirect_uri())
    ck("DCR returned a client_id", bool(reg.get("client_id")), reg.get("client_id"))
    ck("Notion issued a PUBLIC client (no secret)", reg.get("token_endpoint_auth_method") == "none" or not reg.get("client_secret"),
       f"auth_method={reg.get('token_endpoint_auth_method')}")

    # 3) ensure_mcp_oauth_config persists everything onto the connection
    conn = StubConn()
    did = await ensure_mcp_oauth_config(StubDB(), conn)
    ck("ensure_mcp_oauth_config performed registration", did is True)
    creds = conn.decrypt_credentials()
    ck("config persisted: client_id + endpoints + audience",
       all(creds.get(k) for k in ("client_id", "authorize_url", "token_url", "audience")),
       f"client_id={creds.get('client_id')}")
    # Idempotent second call -> no-op
    did2 = await ensure_mcp_oauth_config(StubDB(), conn)
    ck("ensure_mcp_oauth_config is idempotent (2nd call no-op)", did2 is False)

    # 4) get_oauth_params reads the DCR config (public client -> secret may be None)
    op = get_oauth_params(conn)
    ck("get_oauth_params returns client_id + urls", bool(op["client_id"] and op["authorize_url"] and op["token_url"]))
    ck("get_oauth_params tolerates missing client_secret (public client)", "client_secret" in op)

    # 5) Build the authorize URL exactly as the route does
    _, code_challenge = generate_pkce_pair()
    from urllib.parse import urlencode
    params = {
        "response_type": "code", "client_id": op["client_id"],
        "redirect_uri": _redirect_uri(), "state": "test",
        "code_challenge": code_challenge, "code_challenge_method": "S256",
    }
    if op.get("scopes"): params["scope"] = op["scopes"]
    if op.get("audience"): params["resource"] = op["audience"]
    authz = f"{op['authorize_url']}?{urlencode(params)}"
    q = parse_qs(urlsplit(authz).query)
    ck("authorize URL targets Notion", urlsplit(authz).netloc == "mcp.notion.com", urlsplit(authz).netloc)
    ck("authorize URL has client_id + PKCE + resource",
       q.get("client_id", [None])[0] == op["client_id"] and "code_challenge" in q and "resource" in q)

    print(f"\n  {sum(R)}/{len(R)} Notion DCR checks passed")
    print("\n  Manual step (interactive consent, cannot be automated):")
    print("  Open this URL, approve, and Notion redirects to the callback which exchanges")
    print("  the code (PKCE, no secret) for a token stored in UserConnectionCredentials:")
    print(f"\n  {authz}\n")
    return 0 if all(R) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
