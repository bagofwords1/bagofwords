# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=2.3,<3"]
# ///
"""Mock MCP connector: connect to BOW the way ChatGPT / Claude connectors do.

Uses the official MCP Python SDK's OAuth client, which performs the MCP
authorization spec end to end with no pre-registered client:

  401 + WWW-Authenticate -> RFC 9728 resource metadata -> RFC 8414 AS metadata
  -> RFC 7591 Dynamic Client Registration -> authorization code + PKCE
  -> token exchange -> MCP initialize / tools/list -> refresh on expiry

The "user" part (sign in + approve on the consent page) is done either through
the same API calls the consent page makes (--consent api, default) or in a real
browser via tools/agent/mock_mcp_consent.mjs (--consent browser).

Then it probes the token lifecycle against the live server:
  - an expired access token is rejected with error="invalid_token" and the SDK
    recovers by refreshing (needs the server's MCP access TTL <= --ttl-wait)
  - two refreshes racing on the same refresh token both succeed (grace window)
  - replaying a rotated refresh token after the grace window revokes the session
  - the org admin sees the self-registered client in /api/oauth/clients

Usage (stack from tools/agent/boot_stack.sh; short TTLs via BOW_CONFIG_PATH):
  uv run tools/agent/mock_mcp_oauth_client.py --server http://localhost:3000 \\
      --email admin@example.com --password '...' [--org-name 'Acme'] \\
      [--auth-method none|client_secret_basic|client_secret_post] \\
      [--consent api|browser] [--ttl-wait 62] [--grace 5]

Exits non-zero if any check fails. Prints one PASS/FAIL line per check.
"""

import argparse
import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

CALLBACK_PORT = 33418
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"
HERE = Path(__file__).resolve().parent

results: list[tuple[bool, str]] = []
token_calls: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}{f'  ({detail})' if detail else ''}", flush=True)
    return ok


class MemoryStorage(TokenStorage):
    def __init__(self):
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


class CallbackServer:
    """Loopback redirect target, like a desktop MCP client's."""

    def __init__(self):
        self.result: asyncio.Future = asyncio.get_event_loop().create_future()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", CALLBACK_PORT)

    async def _handle(self, reader, writer):
        request_line = (await reader.readline()).decode()
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        target = request_line.split(" ")[1] if " " in request_line else "/"
        params = parse_qs(urlparse(target).query)
        body = b"<html><body>Connected. You can close this window.</body></html>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        if not self.result.done() and urlparse(target).path == "/callback":
            self.result.set_result(params)

    async def close(self):
        self.server.close()


async def bow_login(server: str, email: str, password: str) -> str:
    async with httpx2.AsyncClient(base_url=server, timeout=30) as http:
        r = await http.post("/api/auth/jwt/login", data={"username": email, "password": password})
        r.raise_for_status()
        return r.json()["access_token"]


async def pick_org(server: str, jwt: str, org_name: str | None) -> dict:
    async with httpx2.AsyncClient(base_url=server, timeout=30) as http:
        me = (await http.get("/api/users/whoami", headers={"Authorization": f"Bearer {jwt}"})).json()
    orgs = me.get("organizations") or []
    if org_name:
        orgs = [o for o in orgs if o["name"] == org_name]
    if not orgs:
        raise SystemExit(f"user has no organization named {org_name!r}")
    return orgs[0]


async def consent_via_api(server: str, authorize_url: str, jwt: str, org_id: str):
    """Do what frontend/pages/authorize.vue does after the user clicks Approve."""
    q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
    async with httpx2.AsyncClient(base_url=server, timeout=30) as http:
        info = await http.get(f"/api/oauth/clients/{q['client_id']}/info",
                              params={"redirect_uri": q["redirect_uri"], "scope": q.get("scope", "mcp")})
        info.raise_for_status()
        check(info.json().get("dynamic") is True and info.json().get("trusted") is False,
              "consent page sees an unverified, self-registered client", json.dumps(info.json()))
        body = {k: q.get(k) for k in ("client_id", "redirect_uri", "state", "scope",
                                       "code_challenge", "code_challenge_method")}
        body["organization_id"] = org_id
        r = await http.post("/api/oauth/authorize", json=body,
                            headers={"Authorization": f"Bearer {jwt}"})
        check(r.status_code == 200, "user approves consent for the chosen org", f"HTTP {r.status_code}")
        r.raise_for_status()
    # Follow the redirect back to the client, as the browser would.
    async with httpx2.AsyncClient(timeout=30) as http:
        await http.get(r.json()["redirect_url"])


def consent_via_browser(authorize_url: str, args) -> None:
    cmd = ["node", str(HERE / "mock_mcp_consent.mjs"), authorize_url, args.email, args.password,
           args.org_name or "", args.shots]
    print("  launching browser consent:", " ".join(cmd[:2]), flush=True)
    subprocess.Popen(cmd, cwd=str(HERE.parent.parent / "frontend"))  # resolves @playwright/test


def client_auth(client_info: OAuthClientInformationFull, data: dict) -> tuple[dict, dict]:
    method = client_info.token_endpoint_auth_method
    data = {**data}
    headers = {}
    if method == "client_secret_basic":
        raw = f"{client_info.client_id}:{client_info.client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        data["client_id"] = client_info.client_id
        if method == "client_secret_post":
            data["client_secret"] = client_info.client_secret
    return data, headers


async def raw_refresh(server: str, client_info, refresh_token: str) -> httpx2.Response:
    data, headers = client_auth(client_info, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    async with httpx2.AsyncClient(base_url=server, timeout=30) as http:
        return await http.post("/api/oauth/token", data=data, headers=headers)


async def raw_tools_list(mcp_url: str, access_token: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=30) as http:
        return await http.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               headers={"Authorization": f"Bearer {access_token}",
                                        "Accept": "application/json, text/event-stream"})


async def log_token_call(request):
    if request.url.path.endswith("/api/oauth/token"):
        grant = parse_qs(request.content.decode()).get("grant_type", ["?"])[0]
        token_calls.append(grant)
        print(f"  -> token endpoint: grant_type={grant}", flush=True)
    elif request.url.path.endswith("/api/oauth/register"):
        print("  -> dynamic client registration", flush=True)


async def main(args) -> int:
    server = args.server.rstrip("/")
    mcp_url = f"{server}/api/mcp"

    # ── Discovery, as the connector sees it ──
    async with httpx2.AsyncClient(timeout=30) as http:
        unauth = await http.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        www = unauth.headers.get("www-authenticate", "")
        check(unauth.status_code == 401 and "resource_metadata=" in www,
              "unauthenticated MCP call -> 401 with resource_metadata", www)
        asm = (await http.get(f"{server}/.well-known/oauth-authorization-server")).json()
        check("registration_endpoint" in asm, "AS metadata advertises registration_endpoint",
              asm.get("registration_endpoint", "missing"))

    jwt = await bow_login(server, args.email, args.password)
    org = await pick_org(server, jwt, args.org_name)
    print(f"  user {args.email} will connect org {org['name']} ({org['id']})", flush=True)

    storage = MemoryStorage()
    callback = CallbackServer()
    await callback.start()

    async def redirect_handler(authorize_url: str) -> None:
        print(f"  -> authorize: {authorize_url.split('?')[0]}", flush=True)
        if args.consent == "browser":
            consent_via_browser(authorize_url, args)
        else:
            await consent_via_api(server, authorize_url, jwt, org["id"])

    async def callback_handler():
        params = await asyncio.wait_for(callback.result, timeout=180)
        if "error" in params:
            raise RuntimeError(f"authorization denied: {params}")
        return AuthorizationCodeResult(code=params["code"][0], state=params.get("state", [None])[0])

    provider = OAuthClientProvider(
        server_url=mcp_url,
        client_metadata=OAuthClientMetadata(
            client_name=args.client_name,
            redirect_uris=[CALLBACK_URL],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method=args.auth_method,
            scope="mcp",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    async with httpx2.AsyncClient(auth=provider, timeout=60,
                                  event_hooks={"request": [log_token_call]}) as http:
        async with streamable_http_client(mcp_url, http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                check(True, "MCP initialize through OAuth", f"server={init.server_info.name}")
                tools = await session.list_tools()
                check(len(tools.tools) > 0, "tools/list with the issued token", f"{len(tools.tools)} tools")

                info = storage.client_info
                check(info is not None and info.client_id.startswith("bow_client_"),
                      "client obtained via Dynamic Client Registration", info.client_id if info else "")
                check(bool(info.client_secret) == (args.auth_method != "none"),
                      f"secret issued only for confidential clients ({args.auth_method})")
                first = storage.tokens
                check(first.expires_in is not None and first.expires_in <= 3600,
                      "access token is short-lived", f"expires_in={first.expires_in}s")
                check(bool(first.refresh_token), "refresh token issued")

                # ── Expiry -> transparent refresh ──
                if args.ttl_wait:
                    print(f"  waiting {args.ttl_wait}s for the access token to expire...", flush=True)
                    await asyncio.sleep(args.ttl_wait)
                    stale = await raw_tools_list(mcp_url, first.access_token)
                    check(stale.status_code == 401 and 'error="invalid_token"' in stale.headers.get("www-authenticate", ""),
                          "expired access token -> 401 invalid_token", f"HTTP {stale.status_code}")
                    before = token_calls.count("refresh_token")
                    tools = await session.list_tools()
                    check(token_calls.count("refresh_token") > before and len(tools.tools) > 0,
                          "SDK refreshed and kept working without re-consent")

        # ── Refresh race + replay detection, on the live session's family ──
        info, current = storage.client_info, storage.tokens
        if args.keep_connected:
            await admin_view(server, jwt, org, info, expect_listed=True)
            await callback.close()
            return summarize(storage)
        r1, r2 = await asyncio.gather(raw_refresh(server, info, current.refresh_token),
                                      raw_refresh(server, info, current.refresh_token))
        check(r1.status_code == 200 and r2.status_code == 200,
              "two refreshes racing on one refresh token both succeed", f"{r1.status_code}/{r2.status_code}")
        pair = r1.json() if r1.status_code == 200 else {}
        if pair:
            ok = await raw_tools_list(mcp_url, pair["access_token"])
            check(ok.status_code == 200, "refreshed access token works")

        print(f"  waiting {args.grace + 2}s to pass the reuse grace window...", flush=True)
        await asyncio.sleep(args.grace + 2)
        replay = await raw_refresh(server, info, current.refresh_token)
        check(replay.status_code == 400, "replayed rotated refresh token is rejected", f"HTTP {replay.status_code}")
        if pair:
            revoked = await raw_tools_list(mcp_url, pair["access_token"])
            check(revoked.status_code == 401, "replay revoked the whole sign-in (newest token dead)",
                  f"HTTP {revoked.status_code}")

    await admin_view(server, jwt, org, storage.client_info, expect_listed=False)
    await callback.close()
    return summarize(storage)


async def admin_view(server, jwt, org, client_info, *, expect_listed: bool) -> None:
    """What the org admin sees in Settings -> Integrations -> OAuth apps."""
    async with httpx2.AsyncClient(base_url=server, timeout=30) as http:
        listed = await http.get("/api/oauth/clients",
                                headers={"Authorization": f"Bearer {jwt}", "X-Organization-Id": org["id"]})
    if listed.status_code != 200:
        print(f"  (skipping admin list check: HTTP {listed.status_code}, user is not an admin)")
        return
    mine = [c for c in listed.json() if c["client_id"] == client_info.client_id]
    if expect_listed:
        check(len(mine) == 1 and mine[0]["dynamic"] and mine[0]["active_token_count"] >= 1,
              "org admin sees the connected self-registered client", json.dumps(mine[0] if mine else {}))
    else:
        # After the replay revoked everything, the client has no live tokens
        # left in this org, so it drops out of the admin list again.
        check(not mine, "revoked self-registered client no longer listed for the org")


def summarize(storage) -> int:
    failed = [label for ok, label in results if not ok]
    print(json.dumps({"passed": len(results) - len(failed), "failed": failed,
                      "token_grants": token_calls, "client_id": storage.client_info.client_id}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="http://localhost:3000")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--org-name")
    p.add_argument("--client-name", default="Mock MCP Connector")
    p.add_argument("--auth-method", default="none",
                   choices=["none", "client_secret_basic", "client_secret_post"])
    p.add_argument("--consent", default="api", choices=["api", "browser"])
    p.add_argument("--ttl-wait", type=int, default=0,
                   help="seconds to wait for access-token expiry (server TTL + a margin); 0 skips")
    p.add_argument("--grace", type=int, default=60, help="server refresh_reuse_grace_seconds")
    p.add_argument("--shots", default="", help="screenshot dir for --consent browser")
    p.add_argument("--keep-connected", action="store_true",
                   help="stop after connecting (skip the replay probe, which revokes the sign-in)")
    sys.exit(asyncio.run(main(p.parse_args())))
