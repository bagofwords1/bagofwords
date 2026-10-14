"""Outbound MCP OAuth discovery + Dynamic Client Registration.

Lets a ``type=mcp`` connection obtain its OAuth client and endpoints WITHOUT an
admin pre-registering an app, per the MCP authorization spec:

  - 401 challenge  ``WWW-Authenticate``     -> resource-metadata URL + required scope
  - RFC 9728  protected-resource metadata  -> authorization server(s) + resource + scopes
  - RFC 8414  AS metadata                  -> authorize / token / registration eps
  - RFC 7591  Dynamic Client Registration  -> client_id (public client + PKCE)

Scope selection follows the spec's priority order: the ``scope`` the MCP server
names in its 401 challenge, else the resource's own ``scopes_supported``. The
*authorization server's* ``scopes_supported`` is not a source of resource
scopes: a generic identity provider (Auth0, Okta, Entra) fronting many APIs
lists the standard OIDC scopes there, which say nothing about what this MCP
server needs — requesting them yields a token the server then rejects. That
document is consulted only for ``offline_access`` (so a refresh token is
issued) and as a last resort when the server advertises nothing at all.

The registered client + endpoints are persisted (encrypted) on the connection
so registration runs once. Scopes are re-derived from the server on every
sign-in, because the spec makes the live challenge authoritative and because a
value baked in at registration time cannot be corrected without re-creating
the connection. The per-user authorization-code + PKCE dance afterward reuses
the existing connection-OAuth flow unchanged (only the client comes from DCR).
"""
import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.settings.config import settings

logger = logging.getLogger(__name__)

_WK_PR = "/.well-known/oauth-protected-resource"
_WK_AS = "/.well-known/oauth-authorization-server"
_WK_OIDC = "/.well-known/openid-configuration"

# The unauthenticated request the spec's flow opens with. Any JSON-RPC body
# would do — the point is to be refused with a WWW-Authenticate challenge.
_PROBE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "bagofwords", "version": "discovery"},
    },
}

# auth-param = token "=" ( token / quoted-string )   (RFC 9110 §11.2)
_AUTH_PARAM_RE = re.compile(r'([A-Za-z0-9_\-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


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


def split_scopes(value) -> list:
    """A scope list from a JSON array or a space-/comma-separated string.
    Deduplicated, order preserved."""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace(",", " ").split()
    out = []
    for s in items:
        s = str(s).strip()
        if s and s not in out:
            out.append(s)
    return out


def parse_www_authenticate(values) -> dict:
    """The auth-params of the Bearer challenge in one or more WWW-Authenticate
    header values, lower-cased keys, quotes stripped — e.g.
    ``{"resource_metadata": "https://.../.well-known/oauth-protected-resource",
    "scope": "mcp:read"}``. Empty when there is no Bearer challenge."""
    for raw in values or []:
        m = re.match(r"\s*bearer\b(.*)", raw or "", re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        params = {}
        for key, quoted, bare in _AUTH_PARAM_RE.findall(m.group(1)):
            params[key.lower()] = quoted.replace('\\"', '"') if quoted else bare
        return params
    return {}


async def probe_auth_challenge(client: httpx.AsyncClient, server_url: str) -> dict:
    """Make the unauthenticated MCP request the spec's flow starts with and
    return the parsed Bearer challenge, or ``{}`` when the server doesn't
    answer 401 (open server, or one that challenges some other way)."""
    try:
        r = await client.post(
            server_url,
            json=_PROBE_BODY,
            headers={"Accept": "application/json, text/event-stream"},
        )
    except Exception as e:
        logger.debug("DCR discovery probe of %s failed: %s", server_url, e)
        return {}
    if r.status_code != 401:
        return {}
    return parse_www_authenticate(r.headers.get_list("www-authenticate"))


def select_scopes(challenge_scopes, resource_scopes, as_scopes) -> tuple:
    """The scopes to request, and where they came from.

    Spec priority: the 401 challenge's ``scope`` is authoritative; otherwise the
    protected-resource metadata's ``scopes_supported``. The authorization
    server's list is a last resort only (a server that advertises nothing
    resource-specific), plus the single scope it legitimately owns:
    ``offline_access`` is an AS concern, not a resource one — the spec tells
    resources NOT to list it and clients to add it when the AS supports it, or
    no refresh token is issued and the credential dies with the access token.
    """
    challenge = split_scopes(challenge_scopes)
    resource = split_scopes(resource_scopes)
    as_ = split_scopes(as_scopes)
    if challenge:
        chosen, source = list(challenge), "challenge"
    elif resource:
        chosen, source = list(resource), "resource_metadata"
    elif as_:
        chosen, source = list(as_), "authorization_server"
    else:
        chosen, source = [], "none"
    if "offline_access" in as_ and "offline_access" not in chosen:
        chosen.append("offline_access")
    return " ".join(chosen), source


async def discover_mcp_oauth(server_url: str) -> dict:
    """Discover OAuth metadata for an MCP server (401 challenge -> RFC 9728 -> RFC 8414).

    Returns {issuer, authorize_url, token_url, registration_endpoint, resource,
    scopes, scopes_source, challenge_scopes, resource_scopes,
    authorization_server_scopes}. ``scopes`` is what a sign-in should request.
    Raises ValueError if the authorization server can't be resolved.
    """
    p = urlsplit(server_url)
    origin = _origin(server_url)
    path = p.path.rstrip("/")  # e.g. "/mcp"

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # 0) The 401 challenge: names the resource-metadata URL and the scope
        #    this server requires. Not every server sends one.
        challenge = await probe_auth_challenge(client, server_url)

        # 1) Protected-resource metadata: where the challenge points, then
        #    path-aware (RFC 9728 §3.1), then the origin root.
        pr_urls = []
        for u in (
            [challenge["resource_metadata"]] if challenge.get("resource_metadata") else []
        ) + ([f"{origin}{_WK_PR}{path}"] if path else []) + [f"{origin}{_WK_PR}"]:
            if u not in pr_urls:
                pr_urls.append(u)
        pr = None
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

    scopes, source = select_scopes(
        challenge.get("scope"),
        (pr or {}).get("scopes_supported"),
        md.get("scopes_supported"),
    )
    return {
        "issuer": md.get("issuer") or as_base,
        "authorize_url": md["authorization_endpoint"],
        "token_url": md["token_endpoint"],
        "registration_endpoint": md.get("registration_endpoint"),
        "resource": resource,
        "scopes": scopes,
        "scopes_source": source,
        "challenge_scopes": " ".join(split_scopes(challenge.get("scope"))),
        "resource_scopes": " ".join(split_scopes((pr or {}).get("scopes_supported"))),
        "authorization_server_scopes": " ".join(split_scopes(md.get("scopes_supported"))),
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


def _migrate_legacy_discovered_scopes(creds: dict) -> bool:
    """Before discovered scopes had their own key, registration wrote them into
    ``scopes`` — the same key an admin's override uses. A DCR-registered row
    with ``scopes`` but no ``discovered_scopes`` therefore holds a discovery
    result (the form never offered a scopes field for DCR), not an admin's
    choice. Move it, so a fresh discovery can supersede it."""
    if creds.get("dcr_registered") and "discovered_scopes" not in creds and creds.get("scopes"):
        creds["discovered_scopes"] = creds.pop("scopes")
        return True
    return False


async def ensure_mcp_oauth_config(db, connection) -> bool:
    """Ensure a ``type=mcp`` connection has an OAuth client + endpoints, and
    that a DCR-registered one carries current scopes.

    - Admin-supplied client (client_id + endpoints, not ``dcr_registered``):
      nothing to do.
    - Already DCR-registered: re-derive ``discovered_scopes`` from the server
      (the client is reused, never re-registered). An admin ``scopes`` override,
      when present, still wins at authorize time.
    - Otherwise: discover, register, persist.

    Returns True iff it performed registration.
    """
    if getattr(connection, "type", None) != "mcp":
        return False
    creds = connection.decrypt_credentials() or {}

    config = connection.config
    config = json.loads(config) if isinstance(config, str) else (config or {})
    server_url = config.get("server_url") or creds.get("server_url")

    has_client = bool(creds.get("client_id") and creds.get("authorize_url") and creds.get("token_url"))
    if has_client and not creds.get("dcr_registered"):
        return False

    if has_client:
        changed = _migrate_legacy_discovered_scopes(creds)
        if server_url:
            try:
                meta = await discover_mcp_oauth(server_url)
            except Exception as e:
                # Keep whatever we have; the sign-in will use it and report
                # its own error if the server is genuinely unhappy.
                logger.warning(
                    "DCR: could not refresh scopes for connection %s from %s: %s",
                    connection.id, server_url, e,
                )
            else:
                if (creds.get("discovered_scopes") != meta["scopes"]
                        or creds.get("scopes_source") != meta["scopes_source"]):
                    logger.info(
                        "DCR: scopes for connection %s changed %r -> %r (%s)",
                        connection.id, creds.get("discovered_scopes"), meta["scopes"],
                        meta["scopes_source"],
                    )
                    creds["discovered_scopes"] = meta["scopes"]
                    creds["scopes_source"] = meta["scopes_source"]
                    changed = True
        if changed:
            connection.encrypt_credentials(creds)
            await db.commit()
            await db.refresh(connection)
        return False

    if not server_url:
        raise ValueError(f"MCP connection {connection.id} has no server_url to discover")

    # Any MCP server an admin configures is fair game: choosing which third-party
    # services to integrate is the org admin's call, exactly as it is in every
    # other MCP client. Discovery + registration therefore run against whatever
    # host the connection points at, catalog preset or not.
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
        # `scopes` (an admin override) is left untouched; discovery has its own key.
        "discovered_scopes": meta["scopes"],
        "scopes_source": meta["scopes_source"],
        "registration_client_uri": reg.get("registration_client_uri"),
        "dcr_registered": True,
    })
    connection.encrypt_credentials(creds)
    await db.commit()
    await db.refresh(connection)
    logger.info(
        "DCR: registered MCP client %s for connection %s (scopes %r from %s)",
        reg.get("client_id"), connection.id, meta["scopes"], meta["scopes_source"],
    )
    return True
