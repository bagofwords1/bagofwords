"""Shared base-URL derivation for routes that build externally-visible URLs.

Used by /mcp (OAuth WWW-Authenticate metadata URLs), /excel (Office add-in
manifest URLs), and /.well-known/oauth-* endpoints. All three need to
return the URL the *client* sees, not the internal upstream URL uvicorn
sees, so they need to honor X-Forwarded-* when running behind a reverse
proxy (Caddy, ALB, etc.).

Priority:
  1. bow_config.base_url (operator-configured — always wins if set to a
     real value). The default ``http://0.0.0.0:3000`` (and the legacy
     ``http://0.0.0.0:8000``) is treated as "unconfigured".
  2. X-Forwarded-Proto + X-Forwarded-Host (set by reverse proxies; for
     comma-separated chains we take the leftmost = the external client).
  3. The request's own scheme/Host header (fallback for direct dev
     connections without a proxy).
"""

from __future__ import annotations

from fastapi import Request


# bow_config defaults that should be treated as "no base_url set" so
# request-derived fallback kicks in instead of returning the placeholder.
_DEFAULT_PLACEHOLDERS = (
    "http://0.0.0.0:3000",
    "http://0.0.0.0:8000",
)


def derive_base_url(request: Request) -> str:
    """Return the externally-reachable base URL with no trailing slash."""
    from app.settings.config import settings
    configured = (settings.bow_config.base_url or "").rstrip("/")
    if configured and configured not in _DEFAULT_PLACEHOLDERS:
        return configured

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_proto and forwarded_host:
        proto = forwarded_proto.split(",", 1)[0].strip()
        host = forwarded_host.split(",", 1)[0].strip()
        if proto and host:
            return f"{proto}://{host}"

    scheme = request.url.scheme
    host = request.headers.get("host", request.url.netloc or "localhost")
    return f"{scheme}://{host}"


def derive_request_base_url(request: Request) -> str:
    """Derive base URL from the incoming request headers only, ignoring config.

    Unlike derive_base_url, bow_config.base_url is NOT consulted. Use when the
    URL must reflect the domain the user actually arrived from — e.g. OAuth
    redirect_uri must match the initiating domain, not the configured default.

    Uses X-Forwarded-Proto for scheme (set by Cloudflare Tunnel and most
    reverse proxies) and the Host header for the hostname.
    """
    host = request.headers.get("host", request.url.netloc or "localhost")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        proto = forwarded_proto.split(",", 1)[0].strip()
        return f"{proto}://{host}"
    return f"{request.url.scheme}://{host}"


def derive_mcp_base_url(request: Request) -> str:
    """Like derive_base_url but checks mcp_public_url first.

    Use for OAuth/MCP well-known endpoints so a Cloudflare Tunnel (or any
    reverse-proxy) public URL can be configured independently of base_url.
    """
    from app.settings.config import settings
    configured = (settings.bow_config.mcp_public_url or "").rstrip("/")
    if configured:
        return configured
    return derive_base_url(request)
