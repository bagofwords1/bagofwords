"""Outbound MCP OAuth discovery + Dynamic Client Registration.

Lets a ``type=mcp`` connection obtain its OAuth client and endpoints WITHOUT an
admin pre-registering an app, per the MCP authorization spec:

  - RFC 9728  protected-resource metadata  -> authorization server(s) + resource
  - RFC 8414  AS metadata                  -> authorize / token / registration eps
  - RFC 7591  Dynamic Client Registration  -> client_id (public client + PKCE)

Results are persisted (encrypted) on the connection so this runs once. The
per-user authorization-code + PKCE dance afterward reuses the existing
connection-OAuth flow unchanged (only the client now comes from DCR).
"""
import json
import logging
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.settings.config import settings

logger = logging.getLogger(__name__)

_WK_PR = "/.well-known/oauth-protected-resource"
_WK_AS = "/.well-known/oauth-authorization-server"
_WK_OIDC = "/.well-known/openid-configuration"


def _origin(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


async def _get_json(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(url, headers={"Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("DCR discovery GET %s failed: %s", url, e)
    return None


async def discover_mcp_oauth(server_url: str) -> dict:
    """Discover OAuth metadata for an MCP server (RFC 9728 -> RFC 8414).

    Returns {issuer, authorize_url, token_url, registration_endpoint, resource,
    scopes_supported}. Raises ValueError if metadata can't be resolved.
    """
    p = urlsplit(server_url)
    origin = _origin(server_url)
    path = p.path.rstrip("/")  # e.g. "/mcp"

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # 1) Protected-resource metadata: path-aware first (RFC 9728 §3.1), then root.
        pr = None
        pr_urls = ([f"{origin}{_WK_PR}{path}"] if path else []) + [f"{origin}{_WK_PR}"]
        for u in pr_urls:
            pr = await _get_json(client, u)
            if pr:
                break
        as_list = (pr or {}).get("authorization_servers") or []
        resource = (pr or {}).get("resource") or server_url
        as_base = as_list[0] if as_list else origin

        # 2) AS metadata (RFC 8414, then OIDC fallback); path-aware then root.
        asp = urlsplit(as_base)
        as_origin = _origin(as_base)
        as_path = asp.path.rstrip("/")
        md = None
        candidates = []
        for wk in (_WK_AS, _WK_OIDC):
            if as_path:
                candidates.append(f"{as_origin}{wk}{as_path}")
            candidates.append(f"{as_origin}{wk}")
        for u in candidates:
            md = await _get_json(client, u)
            if md and md.get("authorization_endpoint") and md.get("token_endpoint"):
                break

    if not md or not md.get("authorization_endpoint") or not md.get("token_endpoint"):
        raise ValueError(f"Could not discover OAuth metadata for MCP server {server_url}")

    return {
        "issuer": md.get("issuer") or as_base,
        "authorize_url": md["authorization_endpoint"],
        "token_url": md["token_endpoint"],
        "registration_endpoint": md.get("registration_endpoint"),
        "resource": resource,
        "scopes_supported": " ".join(md.get("scopes_supported") or []),
    }


async def register_client(
    registration_endpoint: str, redirect_uri: str, client_name: str = "BOW"
) -> dict:
    """RFC 7591 Dynamic Client Registration. Returns the registered client doc."""
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        # Public client + PKCE; the AS may downgrade/override in its response.
        "token_endpoint_auth_method": "none",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            registration_endpoint, json=body, headers={"Accept": "application/json"}
        )
    if r.status_code not in (200, 201):
        raise ValueError(f"DCR registration failed: {r.status_code} {r.text}")
    return r.json()


def _redirect_uri() -> str:
    base = settings.bow_config.base_url or "http://localhost:8000"
    return f"{base}/api/connections/oauth/callback"


async def ensure_mcp_oauth_config(db, connection) -> bool:
    """Ensure a ``type=mcp`` connection has an OAuth client + endpoints.

    Idempotent. No-op if the connection already carries client_id + endpoints
    (admin-app tier, or a prior DCR run). Otherwise discovers metadata,
    dynamically registers a client, and persists the result (encrypted) onto the
    connection credentials. Returns True iff it performed registration.
    """
    if getattr(connection, "type", None) != "mcp":
        return False
    creds = connection.decrypt_credentials() or {}
    if creds.get("client_id") and creds.get("authorize_url") and creds.get("token_url"):
        return False

    config = connection.config
    config = json.loads(config) if isinstance(config, str) else (config or {})
    server_url = config.get("server_url") or creds.get("server_url")
    if not server_url:
        raise ValueError(f"MCP connection {connection.id} has no server_url to discover")

    # SSRF guard: only run discovery + dynamic registration against known catalog
    # hosts (their resource + authorization-server hosts). Custom/admin URLs that
    # aren't in the catalog must supply a client manually rather than DCR.
    from app.schemas.connector_catalog import allowed_dcr_hosts
    host = urlsplit(server_url).netloc
    if host not in allowed_dcr_hosts():
        raise ValueError(
            f"DCR is not allowed for host '{host}'. Use a catalog connector, or "
            "configure an OAuth client manually for this connection."
        )

    meta = await discover_mcp_oauth(server_url)
    if not meta.get("registration_endpoint"):
        raise ValueError(
            f"MCP server {server_url} does not advertise a registration_endpoint; "
            "supply client_id/secret manually (no DCR support)."
        )
    reg = await register_client(meta["registration_endpoint"], _redirect_uri())

    creds.update({
        "authorize_url": meta["authorize_url"],
        "token_url": meta["token_url"],
        "client_id": reg["client_id"],
        "client_secret": reg.get("client_secret"),  # None for public clients
        "audience": meta.get("resource"),
        "scopes": creds.get("scopes") or meta.get("scopes_supported") or "",
        "registration_client_uri": reg.get("registration_client_uri"),
        "dcr_registered": True,
    })
    connection.encrypt_credentials(creds)
    await db.commit()
    await db.refresh(connection)
    logger.info(
        "DCR: registered MCP client %s for connection %s",
        reg.get("client_id"), connection.id,
    )
    return True
