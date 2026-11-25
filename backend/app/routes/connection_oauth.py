"""
Connection OAuth Routes.

Handles the OAuth authorization code flow for per-user data source authentication.
Two endpoints:
  GET /connections/{connection_id}/oauth/authorize  — start the flow
  GET /connections/oauth/callback                   — handle the redirect
"""
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import get_async_db
from app.models.user import User
from app.models.connection import Connection
from app.core.auth import current_user, SECRET
from app.core.base_url import connection_oauth_redirect_uri
from app.core.url_safety import is_safe_http_url
from app.settings.config import settings
from app.settings.logging_config import get_logger
from app.errors.redact import redact_error
from app.services.connection_oauth_service import (
    generate_pkce_pair,
    get_oauth_params,
    exchange_code_for_tokens,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/connections", tags=["connection-oauth"])


# ---------------------------------------------------------------------------
# Signed state — binds connection_id + user_id to an HMAC-signed JWT. The OAuth
# state parameter is fully client-controlled, so we never trust values returned
# by the callback until we've verified the signature. This prevents:
#   - Tampering with connection_id to store tokens against a different connection
#   - Tampering with user_id to impersonate a different user in the callback
#   - Replaying an expired state
# ---------------------------------------------------------------------------

_STATE_AUDIENCE = "conn_oauth"
_STATE_TTL_SECONDS = 600  # 10 minutes — generous for slow OAuth providers


def _encode_state(connection_id: str, user_id: str, return_to: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "cid": connection_id,
        "uid": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=_STATE_TTL_SECONDS)).timestamp()),
        "aud": _STATE_AUDIENCE,
        "nonce": secrets.token_urlsafe(16),
    }
    if return_to:
        payload["rt"] = return_to
    return pyjwt.encode(payload, SECRET, algorithm="HS256")


def _safe_return_path(raw: str | None) -> str | None:
    """Only accept an app-internal path for the post-OAuth redirect: a single
    leading slash, no scheme/host (`//evil.com` would be protocol-relative)."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return None
    return raw


def _decode_state(state: str) -> dict:
    """Raises HTTPException on any problem."""
    try:
        payload = pyjwt.decode(
            state,
            SECRET,
            algorithms=["HS256"],
            audience=_STATE_AUDIENCE,
            options={"require": ["cid", "uid", "exp", "aud"]},
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="OAuth state expired")
    except pyjwt.InvalidTokenError as e:
        raise HTTPException(status_code=400, detail=f"Invalid OAuth state: {redact_error(e)}")
    return payload


# ---------------------------------------------------------------------------
# Cookie helpers — only the PKCE code_verifier needs a cookie (it must survive
# the browser redirect). It's a per-flow secret known only to this session.
# ---------------------------------------------------------------------------


def _cookie_secure() -> bool:
    base_url = (settings.bow_config.base_url or "").lower()
    return base_url.startswith("https://")


def _set_verifier_cookie(response, code_verifier: str) -> None:
    response.set_cookie(
        key="conn_oauth_verifier",
        value=code_verifier,
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/api/connections",
    )


def _clear_verifier_cookie(response) -> None:
    response.delete_cookie(key="conn_oauth_verifier", path="/api/connections")
    response.delete_cookie(key=_TCSS_STATE_COOKIE, path="/api/connections")


def _error_redirect(frontend_url: str, message: str) -> RedirectResponse:
    """Build a safe redirect back to the frontend with URL-encoded error details."""
    query = urlencode({"oauth": "error", "message": message or "oauth_failed"})
    response = RedirectResponse(url=f"{frontend_url}/agents?{query}")
    _clear_verifier_cookie(response)
    return response


async def _ensure_connection_access(
    db: AsyncSession, user: User, connection: Connection
) -> None:
    """Raise 403 if the user doesn't have access to this connection."""
    from app.routes.connection import _user_can_access_connection, _is_org_admin, _can_manage_connection
    from app.core.permission_resolver import principal_belongs_to_org
    # These routes carry no X-Organization-Id (the callback is a provider
    # redirect), so the organization comes from the connection itself: the
    # caller must be bound to it. Answered like an unknown id.
    if not connection.organization_id or not await principal_belongs_to_org(
        db, user, connection.organization_id
    ):
        raise HTTPException(status_code=404, detail="Connection not found")
    from app.core.workspace_access import assert_workspace_access
    await assert_workspace_access(db, user, str(connection.organization_id))
    org = connection.organization
    is_admin = await _is_org_admin(db, user, org) if org else False
    if is_admin:
        return
    # Connection managers (incl. its owner) may sign in to it before any
    # agent is built on it.
    if org and await _can_manage_connection(db, org, user, connection):
        return
    if await _user_can_access_connection(db, user, connection):
        return
    raise HTTPException(status_code=403, detail="Access denied to this connection")


def _ensure_oauth_policy(connection: Connection) -> None:
    """Raise 400 if the connection is not configured for user OAuth sign-in."""
    if connection.auth_policy != "user_required":
        raise HTTPException(
            status_code=400,
            detail="This connection does not require per-user authentication",
        )
    allowed = connection.allowed_user_auth_modes or []
    if "oauth" not in allowed:
        raise HTTPException(
            status_code=400,
            detail="OAuth sign-in is not enabled for this connection",
        )


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------

def _normalize_scopes(raw: str) -> str:
    """OAuth `scope` is space-delimited (RFC 6749 §3.3). The connect form accepts
    a comma- OR space-separated list (comma reads clearer); normalize either to
    single-space-delimited for the authorize request."""
    return " ".join((raw or "").replace(",", " ").split())


@router.get("/{connection_id}/oauth/authorize")
async def oauth_authorize(
    connection_id: str,
    request: Request,
    return_to: str | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Start OAuth flow for a connection. Returns {authorization_url}."""
    # Load connection
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
        .where(Connection.id == connection_id)
    )
    connection = result.scalars().first()
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Enforce: user can access this connection AND connection is configured for OAuth
    await _ensure_connection_access(db, user, connection)
    _ensure_oauth_policy(connection)

    # For MCP connections with no preconfigured OAuth client, discover the
    # server's authorization server and dynamically register a client (RFC
    # 9728/8414/7591) before building the authorize URL. Idempotent.
    if connection.type == "mcp":
        try:
            from app.services.mcp_dcr_service import ensure_mcp_oauth_config
            await ensure_mcp_oauth_config(db, connection)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"MCP OAuth discovery/registration failed: {redact_error(e)}")

    if connection.type == "teamcenter":
        return _teamcenter_sso_authorize(connection, user, return_to)

    # Get OAuth params for this connection type
    try:
        oauth_params = get_oauth_params(connection)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=redact_error(e))

    # PKCE verifier stays in a cookie (it's per-session, not security-sensitive to tamper).
    code_verifier, code_challenge = generate_pkce_pair()

    # State is a signed JWT binding connection_id + user_id. This is what prevents a
    # callback from being associated with the wrong connection or user.
    # `return_to` (optional, app-internal path only) rides along in the signed
    # state so the callback can land the user back where they started the
    # sign-in (e.g. the agent-creation tables step) instead of /agents.
    state = _encode_state(
        connection_id=str(connection.id),
        user_id=str(user.id),
        return_to=_safe_return_path(return_to),
    )

    redirect_uri = connection_oauth_redirect_uri()

    # Build authorization URL
    params = {
        "response_type": "code",
        "client_id": oauth_params["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    # `access_type=offline` is a Google-only parameter (it prompts Google to
    # return a refresh token). Other providers use the `offline_access` scope
    # instead, and some are strict about unrecognized authorize params — X's
    # OAuth2 consent screen fails with "You weren't able to give access to the
    # App" when it's present. Only send it to Google.
    #
    # `prompt=consent` is required alongside it: Google issues a refresh token
    # only on the FIRST consent for a user+client. On any re-authorization of an
    # already-consented account it returns an access token with no refresh token,
    # so once that ~1h access token expires there is nothing to refresh and every
    # call 401s. Forcing the consent screen makes Google mint a refresh token on
    # every authorization, so reconnecting always self-heals.
    if oauth_params.get("provider_name") == "google":
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    # Only send a scope when we actually have one (Notion DCR issues no scopes).
    if oauth_params.get("provider_name") == "slack":
        # Slack's v2 flow takes USER scopes as a comma-separated `user_scope`;
        # `scope` is for bot scopes and would mint a bot token instead.
        params["user_scope"] = oauth_params["scopes"]
    elif oauth_params.get("scopes"):
        params["scope"] = _normalize_scopes(oauth_params["scopes"])
    # RFC 8707 resource indicator — audience-bind the token to the MCP server.
    if oauth_params.get("audience"):
        params["resource"] = oauth_params["audience"]
    # The frontend navigates the browser here: only an absolute http(s) URL may
    # leave this endpoint, whatever is stored in (or discovered for) the
    # connection — a javascript: value would otherwise run in our origin.
    authorize_base = (oauth_params.get("authorize_url") or "").strip()
    if not is_safe_http_url(authorize_base):
        raise HTTPException(
            status_code=400,
            detail="Connection OAuth authorization URL must be an absolute http(s) URL",
        )
    authorization_url = f"{authorize_base}?{urlencode(params)}"

    response = JSONResponse({"authorization_url": authorization_url})
    _set_verifier_cookie(response, code_verifier)
    return response


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None,
    iss: str = None,
    db: AsyncSession = Depends(get_async_db),
):
    """Handle OAuth callback — exchange code for tokens and store credentials.

    Does NOT use Depends(current_user) because this is a cross-site redirect
    from the OAuth provider; the user's JWT cookie may not be sent (SameSite).
    The user is identified via the HMAC-signed state parameter, and the
    connection is re-authorized before storing tokens.
    """
    frontend_url = settings.bow_config.base_url or ""

    if error:
        logger.error(f"OAuth callback error: {error} — {error_description}")
        return _error_redirect(frontend_url, error_description or error)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    # Verify signed state (raises 400 on any problem).
    payload = _decode_state(state)
    connection_id = payload["cid"]
    user_id = payload["uid"]
    return_path = _safe_return_path(payload.get("rt"))

    # Fail fast if PKCE cookie is missing — the code exchange will fail at the
    # provider without it, returning a confusing error instead.
    code_verifier = request.cookies.get("conn_oauth_verifier")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE code verifier")

    # Resolve user from signed state, not from an unsigned cookie.
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalars().first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Load connection from signed state.
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
        .where(Connection.id == connection_id)
    )
    connection = result.scalars().first()
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Re-authorize: even though state was signed by us, enforce policy + access
    # here as a defense-in-depth check (covers cases where a user's grants were
    # revoked between authorize and callback, or the connection was re-configured).
    try:
        await _ensure_connection_access(db, user, connection)
        _ensure_oauth_policy(connection)
    except HTTPException as e:
        logger.warning(
            f"OAuth callback rejected for user {user.id} connection {connection.id}: {e.detail}"
        )
        return _error_redirect(frontend_url, e.detail)

    # RFC 9207 / MCP 2026-07-28: when the authorization server names itself in
    # the response (``iss``), it must be the issuer this client registered
    # with — otherwise a code from a different server (mix-up attack) would be
    # redeemed at this connection's token endpoint.
    from app.services.mcp_dcr_service import issuer_matches
    if iss is not None and not issuer_matches(connection, iss):
        logger.warning(
            "OAuth callback for connection %s rejected: iss %r does not match the recorded issuer",
            connection.id, iss,
        )
        return _error_redirect(frontend_url, "The sign-in response came from an unexpected authorization server.")

    # Get OAuth params and exchange code
    try:
        oauth_params = get_oauth_params(connection)
        redirect_uri = connection_oauth_redirect_uri()
        tokens = await exchange_code_for_tokens(
            oauth_params, code, redirect_uri, code_verifier
        )
    except ValueError as e:
        logger.error(f"OAuth token exchange failed: {e}")
        # Surface the provider's actual reason (e.g. "unauthorized_client —
        # Missing valid authorization header") so a misconfigured client auth
        # method / secret is diagnosable from the UI toast, not just the logs.
        return _error_redirect(frontend_url, f"Token exchange failed: {e}")

    return await _finish_sign_in(db, connection, user, tokens, return_path, frontend_url)


async def _finish_sign_in(db, connection, user, tokens: dict, return_path, frontend_url) -> RedirectResponse:
    """Store a member's sign-in, verify it, and land the browser back in the app.

    Shared by the OAuth callback and the Teamcenter SSO callback: the credential
    is saved, checked with a real connection test (rolled back when it fails, so
    a broken sign-in never shadows the fallback), tools/catalog work starts, and
    the browser returns to where the sign-in began.
    """
    connection_id = str(connection.id)
    from app.services.credential_coordination import save_oauth_credentials, restore_failed_oauth_credentials
    row, prior_credentials, saved_blob = await save_oauth_credentials(db, connection, user, tokens)
    saved_row_id = str(row.id)

    # Verify with the freshly-saved credentials (test_user_connection uses
    # construct_client, which builds the right client for every OBO type).
    try:
        from app.services.connection_service import ConnectionService
        conn_service = ConnectionService()
        test_result = await conn_service.test_user_connection(
            db=db,
            connection_id=connection_id,
            organization=connection.organization,
            current_user=user,
        )
        test_ok = bool(test_result.get("success"))
        test_msg = test_result.get("message", "Connection test failed")
    except Exception as e:
        test_ok = False
        test_msg = str(e)

    if not test_ok:
        # Roll back so a failed sign-in doesn't leave a broken credential that
        # would then shadow the owner/system fallback and break every query.
        try:
            await restore_failed_oauth_credentials(
                db, str(user.id), saved_row_id, saved_blob, prior_credentials,
            )
        except Exception:
            await db.rollback()
        logger.warning(f"OAuth connection test failed for user {user.id}: {test_msg}")
        return _error_redirect(frontend_url, test_msg)

    logger.info(f"OAuth credentials saved for user {user.id} on connection {connection_id}")

    # For tool-provider connections (mcp/custom_api), discover tools now using the
    # user's freshly-stored token so the integration agent has callable tools.
    try:
        from app.schemas.data_source_registry import tool_provider_types
        if connection.type in tool_provider_types():
            from app.services.connection_service import ConnectionService as _ToolCS
            await _ToolCS().refresh_tools(db, connection, user)
    except Exception as e:
        # This is the ONLY path that populates a per-user connector's tool
        # catalog, so a failure here leaves the connection reading "Connected"
        # with zero callable tools. Non-fatal (the sign-in itself succeeded and
        # a Reload recovers it), but never quiet — this went unnoticed once.
        logger.error(
            f"Tool refresh after OAuth sign-in failed for connection {connection_id} "
            f"user {user.id}: {e} — the connection has no discovered tools until "
            "someone reloads it.",
            exc_info=True,
        )

    # Kick off the per-user catalog sync in the BACKGROUND.
    #
    # This used to run inline, right here: `get_user_data_source_schema` per
    # linked data source, which for a Graph drive is a full recursive walk —
    # one HTTP round-trip per folder over the whole of the user's OneDrive.
    # The browser sat on the callback URL for the length of that walk with no
    # progress indicator, and because the whole block was best-effort, a Graph
    # throttle mid-walk meant minutes of waiting followed by an empty catalog
    # and a log line. Now the walk runs as a tracked job (a per-user
    # ConnectionIndexing row) and sign-in returns immediately; `indexing=1` on
    # the redirect tells the UI to poll it.
    indexing_started = False
    try:
        from app.schemas.data_source_registry import tool_provider_types
        if connection.type not in tool_provider_types() and (connection.data_sources or []):
            from app.services.connection_indexing_service import ConnectionIndexingService
            await ConnectionIndexingService().start(
                db=db, connection=connection, user_id=str(user.id),
            )
            indexing_started = True
    except Exception as e:
        logger.warning(f"Could not start per-user catalog sync after OAuth sign-in: {e}")

    # Redirect back to frontend — to where the sign-in started when the signed
    # state carries a return path (e.g. the agent-creation tables step).
    target = return_path or "/agents"
    sep = "&" if "?" in target else "?"
    indexing_flag = "&indexing=1" if indexing_started else ""
    response = RedirectResponse(
        url=(
            f"{frontend_url}{target}{sep}oauth=success"
            f"&connection_id={connection_id}{indexing_flag}"
        )
    )
    _clear_verifier_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Teamcenter SSO (Teamcenter Security Services)
#
# TcSS is not OAuth: the browser goes to the TcSS login service with this
# application's ID, signs in through the identity provider behind TcSS (Entra
# ID over SAML, Active Directory), and TcSS returns it to the application root
# URL registered for that ID (the callback) with a TcSS token and the
# Teamcenter user ID. TcSS carries none of our state, so nothing in the return
# itself proves it answers THIS browser's sign-in: an attacker could send a
# member's browser to the callback with the attacker's own token while the
# member's sign-in is pending (login CSRF). So the callback stores nothing. It
# checks the token with Teamcenter, keeps it in an encrypted short-lived cookie,
# and shows a same-origin page naming the Teamcenter identity; only that page's
# button (a POST carrying a token only the page holds, from a page that cannot
# be framed) saves the sign-in.
# ---------------------------------------------------------------------------

_TCSS_STATE_COOKIE = "conn_tcss_state"
_TCSS_PENDING_COOKIE = "conn_tcss_pending"
_TCSS_STATE_AUDIENCE = "conn_tcss"
_TCSS_CONFIRM_TTL_SECONDS = 300
_TCSS_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
_TCSS_STRINGS = {
    "en": {"title": "Connect to Teamcenter", "ask": "Connect “{connection}” in Bag of Words to Teamcenter as {identity}?",
           "hint": "Continue only if this is your own Teamcenter account and you just signed in. Otherwise cancel.",
           "connect": "Connect", "cancel": "Cancel"},
    "es": {"title": "Conectar con Teamcenter",
           "ask": "¿Conectar «{connection}» de Bag of Words con Teamcenter como {identity}?",
           "hint": "Continúa solo si es tu propia cuenta de Teamcenter y acabas de iniciar sesión. Si no, cancela.",
           "connect": "Conectar", "cancel": "Cancelar"},
    "he": {"title": "התחברות ל-Teamcenter", "ask": "לחבר את „{connection}” ב-Bag of Words ל-Teamcenter בתור {identity}?",
           "hint": "המשיכו רק אם זה חשבון ה-Teamcenter שלכם ורק עכשיו התחברתם. אחרת בטלו.",
           "connect": "התחברות", "cancel": "ביטול"},
}


def _connection_config(connection: Connection) -> dict:
    import json as _json
    config = connection.config
    if isinstance(config, str):
        try:
            config = _json.loads(config)
        except (TypeError, ValueError):
            config = {}
    return config if isinstance(config, dict) else {}


def _tcss_fernet():
    from cryptography.fernet import Fernet
    key = settings.bow_config.encryption_key
    return Fernet(key.encode() if isinstance(key, str) else key)


def _tcss_confirm_code(nonce: str) -> str:
    import hashlib
    import hmac
    return hmac.new(str(SECRET).encode(), ("tcss-confirm\0" + nonce).encode(), hashlib.sha256).hexdigest()


def _teamcenter_sso_authorize(connection: Connection, user: User, return_to: str | None) -> JSONResponse:
    from app.data_sources.clients.teamcenter_client import TeamcenterError, tcss_login_url
    try:
        url = tcss_login_url(_connection_config(connection))
    except TeamcenterError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not is_safe_http_url(url):
        raise HTTPException(status_code=400, detail="The Teamcenter SSO login URL must be an absolute http(s) URL")
    now = datetime.now(timezone.utc)
    payload = {"cid": str(connection.id), "uid": str(user.id), "iat": int(now.timestamp()),
               "exp": int((now + timedelta(seconds=_STATE_TTL_SECONDS)).timestamp()), "aud": _TCSS_STATE_AUDIENCE,
               "nonce": secrets.token_urlsafe(16)}
    rt = _safe_return_path(return_to)
    if rt:
        payload["rt"] = rt
    response = JSONResponse({"authorization_url": url})
    secure = _cookie_secure()
    # TcSS may return with a cross-site POST: a Lax cookie would not be sent then,
    # so over HTTPS the state cookie is SameSite=None (it is httponly and signed).
    response.set_cookie(key=_TCSS_STATE_COOKIE, value=pyjwt.encode(payload, SECRET, algorithm="HS256"),
                        max_age=_STATE_TTL_SECONDS, httponly=True, secure=secure,
                        samesite="none" if secure else "lax", path="/api/connections")
    return response


async def _tcss_load(db: AsyncSession, user_id: str, connection_id: str):
    user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
        .where(Connection.id == connection_id)
    )
    connection = result.scalars().first()
    if not user or not connection or connection.type != "teamcenter":
        raise HTTPException(status_code=404, detail="Connection not found")
    await _ensure_connection_access(db, user, connection)
    _ensure_oauth_policy(connection)
    return user, connection


def _tcss_error(frontend_url: str, message: str) -> RedirectResponse:
    response = _error_redirect(frontend_url, message)
    response.delete_cookie(key=_TCSS_PENDING_COOKIE, path="/api/connections")
    return response


@router.api_route("/teamcenter/sso/callback", methods=["GET", "POST"])
async def teamcenter_sso_callback(request: Request, db: AsyncSession = Depends(get_async_db)):
    """Where Teamcenter Security Services returns the browser after a member signs in.

    Stores nothing: checks the token with Teamcenter and asks the member to confirm the identity.
    """
    import html
    import json as _json
    frontend_url = settings.bow_config.base_url or ""
    state = request.cookies.get(_TCSS_STATE_COOKIE)
    if not state:
        return _tcss_error(frontend_url, "The Teamcenter sign-in expired or was started elsewhere. "
                                         "Start it again from Bag of Words.")
    try:
        payload = pyjwt.decode(state, SECRET, algorithms=["HS256"], audience=_TCSS_STATE_AUDIENCE,
                               options={"require": ["cid", "uid", "exp", "aud", "nonce"]})
    except pyjwt.InvalidTokenError:
        return _tcss_error(frontend_url, "The Teamcenter sign-in expired. Start it again from Bag of Words.")
    params = list(request.query_params.multi_items())
    if request.method == "POST":
        try:
            form = await request.form()
            params += [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
        except Exception:
            pass
    try:
        user, connection = await _tcss_load(db, payload["uid"], payload["cid"])
    except HTTPException as e:
        return _tcss_error(frontend_url, str(e.detail))

    from app.data_sources.clients.teamcenter_client import extract_sso_result
    config = _connection_config(connection)
    token, tc_user = extract_sso_result(params, config.get("sso_token_param"), config.get("sso_user_param"))
    if not token:
        # Names only, never values: they tell an admin which parameter to configure.
        names = ", ".join(sorted({k for k, _ in params})[:12]) or "none"
        logger.warning(f"Teamcenter SSO callback for connection {connection.id} carried no token (parameters: {names})")
        return _tcss_error(frontend_url, "Teamcenter Security Services returned no sign-in token "
                                         f"(parameters received: {names}). Check the SSO parameter names "
                                         "in the connection settings.")

    # Check the token with Teamcenter first, to show whose account it is (and fail fast).
    credentials = {"access_token": token, **({"tcss_user": tc_user} if tc_user else {})}
    try:
        from app.core.egress import acheck_connection_params
        from app.services.connection_service import ConnectionService
        await acheck_connection_params(connection.type, {**config, **credentials})
        client = ConnectionService()._resolve_client_by_type(data_source_type=connection.type, config=config,
                                                             credentials=credentials)
        outcome = await client.atest_connection()
    except Exception as exc:
        logger.info("Teamcenter SSO token not checked: %s", type(exc).__name__)
        outcome = {"success": False, "message": "Teamcenter could not check the sign-in."}
    details = (outcome or {}).get("details") or {}
    if not details.get("identity"):
        return _tcss_error(frontend_url, (outcome or {}).get("message") or "Teamcenter did not accept the sign-in.")
    identity = details.get("identity")
    if details.get("display_name") and details["display_name"] != identity:
        identity = f"{details['display_name']} ({identity})"

    nonce = secrets.token_urlsafe(24)
    pending = _tcss_fernet().encrypt(_json.dumps({
        "cid": payload["cid"], "uid": payload["uid"], "rt": payload.get("rt"), "nonce": nonce,
        "credentials": credentials,
    }).encode()).decode()
    from app.dependencies import _locale_from_org
    locale = (_locale_from_org(connection.organization) or "en").split("-")[0]
    t = _TCSS_STRINGS.get(locale, _TCSS_STRINGS["en"])
    direction = "rtl" if locale in ("he", "ar") else "ltr"
    cancel = f"{frontend_url}/agents?{urlencode({'oauth': 'error', 'message': 'Teamcenter sign-in cancelled'})}"
    page = f"""<!doctype html>
<html lang="{html.escape(locale)}" dir="{direction}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(t['title'])}</title>
<style>body{{font-family:system-ui,sans-serif;background:#f3f4f6;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{background:#fff;border-radius:12px;padding:28px 32px;max-width:460px;box-shadow:0 1px 3px rgba(0,0,0,.12)}}
h1{{font-size:18px;margin:0 0 12px}}p{{color:#374151;line-height:1.5}}.hint{{font-size:13px;color:#6b7280}}
.row{{display:flex;gap:10px;margin-top:20px}}button,a{{font-size:14px;padding:8px 16px;border-radius:8px;text-decoration:none}}
button{{background:#2563eb;color:#fff;border:0;cursor:pointer}}a{{color:#374151;border:1px solid #d1d5db}}</style></head>
<body><main><h1>{html.escape(t['title'])}</h1>
<p>{html.escape(t['ask'].format(connection=connection.name, identity=identity))}</p>
<p class="hint">{html.escape(t['hint'])}</p>
<form method="post" action="/api/connections/teamcenter/sso/confirm">
<input type="hidden" name="confirm" value="{_tcss_confirm_code(nonce)}">
<div class="row"><button type="submit">{html.escape(t['connect'])}</button><a href="{html.escape(cancel)}">{html.escape(t['cancel'])}</a></div>
</form></main></body></html>"""
    from fastapi.responses import HTMLResponse
    response = HTMLResponse(page, headers=_TCSS_PAGE_HEADERS)
    response.set_cookie(key=_TCSS_PENDING_COOKIE, value=pending, max_age=_TCSS_CONFIRM_TTL_SECONDS, httponly=True,
                        secure=_cookie_secure(), samesite="strict", path="/api/connections")
    response.delete_cookie(key=_TCSS_STATE_COOKIE, path="/api/connections")
    return response


@router.post("/teamcenter/sso/confirm")
async def teamcenter_sso_confirm(request: Request, db: AsyncSession = Depends(get_async_db)):
    """The member confirmed the Teamcenter identity on the callback page: save the sign-in."""
    import hmac
    import json as _json
    from cryptography.fernet import InvalidToken
    frontend_url = settings.bow_config.base_url or ""
    try:
        form = await request.form()
        code = str(form.get("confirm") or "")
    except Exception:
        code = ""
    pending = request.cookies.get(_TCSS_PENDING_COOKIE)
    try:
        data = _json.loads(_tcss_fernet().decrypt(pending.encode(), ttl=_TCSS_CONFIRM_TTL_SECONDS)) if pending else None
    except (InvalidToken, ValueError):
        data = None
    if not data or not code or not hmac.compare_digest(code, _tcss_confirm_code(str(data.get("nonce") or ""))):
        return _tcss_error(frontend_url, "The Teamcenter sign-in could not be confirmed. Start it again from Bag of Words.")
    try:
        user, connection = await _tcss_load(db, data["uid"], data["cid"])
    except HTTPException as e:
        return _tcss_error(frontend_url, str(e.detail))
    response = await _finish_sign_in(db, connection, user, data["credentials"], _safe_return_path(data.get("rt")),
                                     frontend_url)
    response.delete_cookie(key=_TCSS_PENDING_COOKIE, path="/api/connections")
    return response
