"""OAuth 2.1 Authorization Server routes.

Provides the endpoints external apps use to obtain access tokens via the
OAuth 2.1 Authorization Code + PKCE flow. Separate scopes protect the MCP
surface and the main application API.

Two routers:
  - well_known_router: mounted at root for /.well-known/* metadata
  - router: mounted at /api for /api/oauth/* endpoints
"""

import base64
import binascii
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.ee.audit.service import audit_service
from app.models.organization import Organization
from app.models.user import User
from app.services.oauth_server_service import (
    DYNAMIC_AUTH_METHODS,
    MAX_DYNAMIC_REDIRECT_URIS,
    OAuthAuthorizationError,
    OAuthServerService,
    dynamic_registration_enabled,
)
from app.settings.config import settings

logger = logging.getLogger(__name__)

# Scopes this authorization server can issue. Extend this list when adding a
# new protected resource; the well-known metadata handlers read from it.
SUPPORTED_SCOPES = ["mcp", "app"]
DEFAULT_SCOPE = "mcp"

# ── Well-known metadata (mounted at root, no /api prefix) ──────────

well_known_router = APIRouter(tags=["oauth-metadata"])


def _base_url(request: Request) -> str:
    """Derive the public base URL from config, X-Forwarded-* headers, or request."""
    from app.core.base_url import derive_mcp_base_url
    return derive_mcp_base_url(request)


@well_known_router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request):
    """RFC 9728 - Protected Resource Metadata."""
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}/api/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["mcp"],
    })


@well_known_router.get("/.well-known/oauth-protected-resource/api")
async def app_protected_resource_metadata(request: Request):
    """RFC 9728 metadata for the main application API."""
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}/api",
        "authorization_servers": [base],
        "scopes_supported": ["app"],
    })


@well_known_router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request):
    """RFC 8414 - Authorization Server Metadata."""
    base = _base_url(request)
    metadata = {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/api/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": list(DYNAMIC_AUTH_METHODS),
        "scopes_supported": list(SUPPORTED_SCOPES),
    }
    if dynamic_registration_enabled():
        metadata["registration_endpoint"] = f"{base}/api/oauth/register"
    return JSONResponse(metadata)


# ── OAuth API routes (mounted at /api) ─────────────────────────────

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/authorize")
async def authorize_redirect(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    state: Optional[str] = None,
    scope: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
    login_hint: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
):
    """OAuth authorize endpoint.

    Redirects to the Vue frontend consent page, preserving all query params.
    The user logs in there, approves, and the frontend calls POST /api/oauth/authorize.
    """
    if response_type != "code":
        return JSONResponse(
            {"error": "unsupported_response_type"},
            status_code=400,
        )

    if not code_challenge or code_challenge_method != "S256":
        return JSONResponse(
            {"error": "invalid_request", "error_description": "PKCE S256 is required"},
            status_code=400,
        )

    service = OAuthServerService()
    client = await service.validate_client(db, client_id)
    if not client or not service.validate_redirect_uri(client, redirect_uri):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Invalid client or redirect URI"},
            status_code=400,
        )
    normalized_scope = _validate_scopes(scope, client.scopes)

    # Build redirect to frontend consent page. Use a path-only URL so this
    # endpoint can never be used to bounce users to an external origin
    # (Snyk python/OR open-redirect). The actual OAuth redirect_uri the client
    # passes is preserved as a query param and validated against registered
    # URIs when the consent is approved.
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": response_type,
        "scope": normalized_scope,
    }
    if state:
        params["state"] = state
    if code_challenge:
        params["code_challenge"] = code_challenge
    if code_challenge_method:
        params["code_challenge_method"] = code_challenge_method
    # Carried so the consent page can hand it to the SSO round trip: the app
    # embedding BOW knows who it is opening the session for, and naming that
    # user is what keeps the provider from showing an account picker.
    if login_hint:
        params["login_hint"] = login_hint

    consent_url = f"/authorize?{urlencode(params)}"
    return RedirectResponse(url=consent_url, status_code=302)


@router.post("/authorize")
async def authorize_approve(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Called by frontend after user approves the consent.

    Expects JSON body with: client_id, redirect_uri, state, scope, code_challenge, code_challenge_method.
    Returns the redirect URL with the authorization code.
    """
    body = await request.json()
    client_id = body.get("client_id")
    redirect_uri = body.get("redirect_uri")
    state = body.get("state")
    raw_scope = body.get("scope")
    code_challenge = body.get("code_challenge")
    code_challenge_method = body.get("code_challenge_method", "S256")

    if not client_id or not redirect_uri or not code_challenge:
        raise HTTPException(status_code=400, detail="Missing required parameters")

    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="Only S256 code_challenge_method is supported")

    service = OAuthServerService()

    # Validate client
    client = await service.validate_client(db, client_id)
    if not client:
        raise HTTPException(status_code=400, detail="Invalid client_id")

    # Validate redirect_uri
    if not service.validate_redirect_uri(client, redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    # The token's org is the CLIENT's org — never the caller's active-org header.
    # On multi-org deployments a user can be active in org A while approving a
    # client registered under org B; binding to the header would issue a token
    # scoped to the wrong tenant. Gate on membership so a user can only approve
    # clients belonging to an org they're actually in. A self-registered client
    # has no org: the user picks one explicitly in the consent body.
    try:
        organization_id = await service.resolve_consent_organization(
            db, client, user, body.get("organization_id"),
        )
    except OAuthAuthorizationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)

    # Validate requested scope: must be a non-empty subset of both the server's
    # SUPPORTED_SCOPES and the client's registered scopes.
    scope = _validate_scopes(raw_scope, client.scopes)

    # Create authorization code
    code = await service.create_authorization_code(
        db=db,
        client_id=client_id,
        user_id=user.id,
        organization_id=organization_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
    )

    # Build callback URL
    parsed_callback = urlsplit(redirect_uri)
    callback_query = parse_qsl(parsed_callback.query, keep_blank_values=True)
    callback_query.append(("code", code))
    if state:
        callback_query.append(("state", state))
    callback = urlunsplit(parsed_callback._replace(query=urlencode(callback_query)))

    return {"redirect_url": callback}


def _basic_client_credentials(request: Request) -> Optional[tuple[str, str]]:
    """client_secret_basic (RFC 6749 §2.3.1): form-urlencoded id:secret."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, sep, client_secret = decoded.partition(":")
    if not sep:
        return None
    return unquote(client_id.replace("+", " ")), unquote(client_secret.replace("+", " "))


@router.post("/token")
async def token_endpoint(
    request: Request,
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_async_db),
):
    """OAuth token endpoint.

    Supports grant_type=authorization_code and grant_type=refresh_token.
    Clients authenticate with client_secret_post, client_secret_basic, or
    (public clients) just client_id.
    """
    basic = _basic_client_credentials(request)
    if basic:
        basic_id, basic_secret = basic
        if (client_id and client_id != basic_id) or client_secret:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Use one client authentication method"},
                status_code=400,
            )
        client_id, client_secret = basic_id, basic_secret
    if not client_id:
        return JSONResponse(
            {"error": "invalid_client", "error_description": "Missing client_id"},
            status_code=401,
        )

    service = OAuthServerService()

    if grant_type == "authorization_code":
        if not code or not code_verifier or not redirect_uri:
            logger.warning("Token request missing params: code=%s code_verifier=%s redirect_uri=%s", bool(code), bool(code_verifier), bool(redirect_uri))
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Missing code, code_verifier, or redirect_uri"},
                status_code=400,
            )

        logger.info("Token exchange attempt: client_id=%s redirect_uri=%s", client_id, redirect_uri)
        result = await service.exchange_code(
            db=db,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        if not result:
            logger.warning("Token exchange failed: client_id=%s redirect_uri=%s", client_id, redirect_uri)
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired authorization code"},
                status_code=400,
            )
        logger.info("Token exchange succeeded: client_id=%s", client_id)
        return JSONResponse(result)

    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Missing refresh_token"},
                status_code=400,
            )

        logger.info("Refresh token attempt: client_id=%s", client_id)
        result = await service.refresh_access_token(
            db=db,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
        if not result:
            logger.warning("Refresh token failed: client_id=%s", client_id)
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired refresh token"},
                status_code=400,
            )
        logger.info("Refresh token succeeded: client_id=%s", client_id)
        return JSONResponse(result)

    else:
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )


# ── Dynamic Client Registration (RFC 7591) ─────────────────────────

class _RegistrationRateLimiter:
    """Sliding one-hour window per client IP.

    In-process, so each worker counts separately; it bounds abuse of an
    unauthenticated endpoint, it is not an exact quota.
    """

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float = 3600.0) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:
                for stale in [k for k, v in self._hits.items() if not v]:
                    del self._hits[stale]
            return True


_registration_limiter = _RegistrationRateLimiter()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _registration_error(description: str, error: str = "invalid_client_metadata", status_code: int = 400):
    return JSONResponse({"error": error, "error_description": description}, status_code=status_code)


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _valid_dynamic_redirect_uri(uri) -> bool:
    if not isinstance(uri, str) or not uri or len(uri) > 2048:
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    # Native/desktop clients may listen on loopback over plain http.
    return parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS


@router.post("/register")
async def register_client(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """RFC 7591 Dynamic Client Registration.

    Unauthenticated by design: this is how MCP connectors (ChatGPT, Claude)
    obtain a client_id without an admin. What it can register is narrow: the
    MCP scope only, never trusted (consent is always shown), no org until a
    member approves and picks one.
    """
    if not dynamic_registration_enabled():
        return _registration_error(
            "Dynamic client registration is disabled on this server",
            error="access_denied",
            status_code=403,
        )
    limit = settings.bow_config.oauth_server.dynamic_registrations_per_ip_per_hour
    if not _registration_limiter.allow(_client_ip(request), limit):
        return _registration_error("Too many registrations", error="slow_down", status_code=429)

    try:
        body = await request.json()
    except Exception:
        return _registration_error("Body must be a JSON object")
    if not isinstance(body, dict):
        return _registration_error("Body must be a JSON object")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _registration_error("redirect_uris is required", error="invalid_redirect_uri")
    if len(redirect_uris) > MAX_DYNAMIC_REDIRECT_URIS:
        return _registration_error("Too many redirect_uris", error="invalid_redirect_uri")
    if not all(_valid_dynamic_redirect_uri(uri) for uri in redirect_uris):
        return _registration_error(
            "redirect_uris must be https URLs (http is allowed only for localhost)",
            error="invalid_redirect_uri",
        )
    redirect_uris = list(dict.fromkeys(redirect_uris))

    auth_method = body.get("token_endpoint_auth_method") or "client_secret_basic"
    if auth_method not in DYNAMIC_AUTH_METHODS:
        return _registration_error(f"Unsupported token_endpoint_auth_method: {auth_method}")

    # Unsupported grant/response types are dropped rather than rejected (the
    # server may narrow requested metadata, RFC 7591 §3.2.1); the response
    # states what was actually registered.
    grant_types = body.get("grant_types") or ["authorization_code"]
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        return _registration_error("The authorization_code grant is required")
    response_types = body.get("response_types") or ["code"]
    if not isinstance(response_types, list) or "code" not in response_types:
        return _registration_error("The code response type is required")

    raw_name = body.get("client_name")
    name = " ".join(raw_name.split())[:255] if isinstance(raw_name, str) else ""
    if not name:
        name = urlsplit(redirect_uris[0]).hostname or "MCP client"

    # Requested scopes are advisory (RFC 7591 §2): self-registered clients
    # are granted the MCP scope whatever they ask for.
    service = OAuthServerService()
    registered = await service.register_dynamic_client(
        db,
        client_name=name,
        redirect_uris=redirect_uris,
        token_endpoint_auth_method=auth_method,
    )
    logger.info(
        "Dynamic client registered: client_id=%s name=%r redirect_hosts=%s",
        registered["client_id"],
        name,
        sorted({urlsplit(uri).hostname for uri in redirect_uris}),
    )
    return JSONResponse(registered, status_code=201, headers={"Cache-Control": "no-store"})


# ── Client CRUD ────────────────────────────────────────────────────

@router.get("/clients")
@requires_permission("manage_settings")
async def list_clients(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """List OAuth clients for the current organization."""
    service = OAuthServerService()
    return await service.list_clients(db, organization.id)


@router.post("/clients")
@requires_permission("manage_settings")
async def create_client(
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Create an OAuth client for the current organization."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    raw_scopes = body.get("scopes")
    scopes = _validate_scopes(raw_scopes)

    trusted = body.get("trusted", False)
    if not isinstance(trusted, bool):
        raise HTTPException(status_code=400, detail="trusted must be a boolean")

    redirect_uris = _validate_redirect_uris(body.get("redirect_uris"))

    service = OAuthServerService()
    client = await service.create_client(
        db=db,
        organization_id=organization.id,
        name=name,
        scopes=scopes,
        redirect_uris=redirect_uris,
        trusted=trusted,
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="oauth_client.created",
            user_id=current_user.id, resource_type="oauth_client", resource_id=client.get("id"),
            details={"name": name, "client_id": client.get("client_id"), "scopes": scopes},
            request=request,
        )
    except Exception:
        pass
    return client


def _validate_redirect_uris(redirect_uris):
    """Validate an optional redirect_uris payload. Returns the list unchanged,
    or None when absent (caller decides the fallback)."""
    if redirect_uris is None:
        return None
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris must be a non-empty list of strings")
    for uri in redirect_uris:
        if not isinstance(uri, str) or not uri.strip():
            raise HTTPException(status_code=400, detail="redirect_uris must be a non-empty list of strings")
    return redirect_uris


def _validate_scopes(raw_scopes, client_scopes: Optional[str] = None) -> str:
    scopes_source = raw_scopes if isinstance(raw_scopes, str) and raw_scopes.strip() else DEFAULT_SCOPE
    requested = list(dict.fromkeys(scopes_source.split()))
    if not requested:
        raise HTTPException(status_code=400, detail="invalid_scope")
    registered = set((client_scopes or " ".join(SUPPORTED_SCOPES)).split())
    for scope in requested:
        if scope not in SUPPORTED_SCOPES or scope not in registered:
            raise HTTPException(status_code=400, detail=f"invalid_scope: {scope}")
    return " ".join(requested)


@router.patch("/clients/{client_db_id}")
@requires_permission("manage_settings")
async def update_client(
    client_db_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Update an OAuth client's name, redirect URIs, scopes, or trust."""
    body = await request.json()

    name = None
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name must not be empty")

    redirect_uris = _validate_redirect_uris(body.get("redirect_uris"))

    scopes = _validate_scopes(body.get("scopes")) if "scopes" in body else None

    trusted = body.get("trusted") if "trusted" in body else None
    if trusted is not None and not isinstance(trusted, bool):
        raise HTTPException(status_code=400, detail="trusted must be a boolean")

    if name is None and redirect_uris is None and scopes is None and trusted is None:
        raise HTTPException(status_code=400, detail="Nothing to update")

    service = OAuthServerService()
    updated = await service.update_client(
        db=db,
        client_db_id=client_db_id,
        organization_id=organization.id,
        name=name,
        redirect_uris=redirect_uris,
        scopes=scopes,
        trusted=trusted,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="oauth_client.updated",
            user_id=current_user.id, resource_type="oauth_client", resource_id=client_db_id,
            details={
                "name": name,
                "redirect_uris_changed": redirect_uris is not None,
                "scopes": scopes,
                "trusted": trusted,
            },
            request=request,
        )
    except Exception:
        pass
    return updated


@router.get("/clients/{client_id}/info")
async def get_client_info(
    client_id: str,
    redirect_uri: Optional[str] = None,
    scope: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
):
    """Validate and return the public consent-screen client metadata."""
    service = OAuthServerService()
    info = await service.get_client_info(db, client_id)
    if not info:
        raise HTTPException(status_code=404, detail="Client not found")
    client = await service.validate_client(db, client_id)
    if redirect_uri is not None and not service.validate_redirect_uri(client, redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    if scope is not None:
        normalized = _validate_scopes(scope, client.scopes)
        info["requested_scopes"] = normalized.split()
    return info


@router.delete("/clients/{client_db_id}")
@requires_permission("manage_settings")
async def delete_client(
    client_db_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Delete an OAuth client."""
    service = OAuthServerService()
    deleted = await service.delete_client(db, client_db_id, organization.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="oauth_client.deleted",
            user_id=current_user.id, resource_type="oauth_client", resource_id=client_db_id,
            request=request,
        )
    except Exception:
        pass
    return {"ok": True}


@router.post("/clients/{client_db_id}/rotate")
@requires_permission("manage_settings")
async def rotate_client_secret(
    client_db_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Rotate client secret. Returns the new secret (shown only once)."""
    service = OAuthServerService()
    result = await service.rotate_client_secret(db, client_db_id, organization.id)
    if not result:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="oauth_client.secret_rotated",
            user_id=current_user.id, resource_type="oauth_client", resource_id=client_db_id,
            request=request,
        )
    except Exception:
        pass
    return result


@router.post("/clients/{client_db_id}/revoke")
@requires_permission("manage_settings")
async def revoke_client_access(
    client_db_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Sign every user of this org out of a client, keeping the client.

    The only removal available for a self-registered client, which no single
    org owns. Members can reconnect it, unless the org turns self-registered
    apps off.
    """
    service = OAuthServerService()
    revoked = await service.revoke_client_access(db, client_db_id, organization.id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="oauth_client.access_revoked",
            user_id=current_user.id, resource_type="oauth_client", resource_id=client_db_id,
            request=request,
        )
    except Exception:
        pass
    return {"ok": True}
