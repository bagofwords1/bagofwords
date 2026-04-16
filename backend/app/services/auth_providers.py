import base64
import hashlib
import os
import time
import uuid
import urllib.parse

from typing import Optional, Dict, Any, Tuple

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from httpx_oauth.clients.openid import OpenID
from httpx_oauth.clients.google import GoogleOAuth2

from app.settings.config import settings
from app.core.auth import get_jwt_strategy

import logging as _logging

_auth_logger = _logging.getLogger(__name__)


async def _audit_auth_event(
    action: str,
    request: Request,
    user_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Fire-and-forget audit log for auth events (login/failure).

    Uses its own session since auth handlers have no injected db session.
    """
    try:
        from app.dependencies import async_session_maker
        from app.ee.audit.service import audit_service

        ip_address = None
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            ip_address = forwarded.split(",")[0].strip()
        else:
            ip_address = request.client.host if request.client else None

        async with async_session_maker() as session:
            await audit_service.log(
                db=session,
                organization_id=None,
                action=action,
                user_id=user_id,
                resource_type="auth",
                details=details,
                request=request,
            )
    except Exception:
        _auth_logger.debug("_audit_auth_event failed", exc_info=True)


def _cookie_secure() -> bool:
    base_url = (settings.bow_config.base_url or "").lower()
    return base_url.startswith("https://")


def _get_scopes(scopes: Optional[list]) -> list:
    return scopes or ["openid", "profile", "email"]


def _get_redirect_uri(provider: str, redirect_path: Optional[str] = None) -> str:
    path = redirect_path or f"/api/auth/{provider}/callback"
    return f"{settings.bow_config.base_url}{path}"


def _issue_state_cookie(provider: str, response: JSONResponse, state: str) -> None:
    response.set_cookie(
        key=f"oidc_{provider}_state",
        value=state,
        max_age=300,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path=f"/api/auth/{provider}",
    )


def _read_state_cookie(provider: str, request: Request) -> Optional[str]:
    return request.cookies.get(f"oidc_{provider}_state")


def _issue_pkce_cookies(provider: str, response: JSONResponse, code_verifier: str) -> None:
    response.set_cookie(
        key=f"oidc_{provider}_verifier",
        value=code_verifier,
        max_age=300,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path=f"/api/auth/{provider}",
    )


def _read_pkce_cookie(provider: str, request: Request) -> Optional[str]:
    return request.cookies.get(f"oidc_{provider}_verifier")


def _generate_pkce_pair() -> Tuple[str, str]:
    # verifier (43-128 chars) and S256 challenge
    verifier_bytes = os.urandom(64)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).decode().rstrip("=")
    challenge = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).decode().rstrip("=")
    return code_verifier, code_challenge


def _get_oidc_config(provider_name: str):
    providers = getattr(settings.bow_config, "oidc_providers", []) or []
    for p in providers:
        if p.name == provider_name:
            return p
    return None


def _is_entra_provider(provider_name: str) -> bool:
    """Check if an OIDC provider is Microsoft Entra ID based on its issuer URL."""
    cfg = _get_oidc_config(provider_name)
    if not cfg:
        return False
    issuer = (cfg.issuer or "").lower()
    return "login.microsoftonline.com" in issuer or "sts.windows.net" in issuer


async def build_authorize_url(provider: str, request: Request) -> JSONResponse:
    # Google
    if provider == "google":
        g = settings.bow_config.google_oauth
        if not g or not g.enabled:
            raise HTTPException(status_code=404, detail="Google OAuth not enabled")
        if not (g.client_id and g.client_secret):
            raise HTTPException(status_code=400, detail="Google OAuth is misconfigured")

        client = GoogleOAuth2(g.client_id, g.client_secret)
        state = uuid.uuid4().hex
        redirect_uri = _get_redirect_uri(provider)
        authorization_url = await client.get_authorization_url(
            redirect_uri=redirect_uri,
            state=state,
            scope=["openid", "profile", "email"],
        )
        response = JSONResponse({"authorization_url": authorization_url})
        _issue_state_cookie(provider, response, state)
        return response

    # OIDC providers
    cfg = _get_oidc_config(provider)
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=404, detail="OIDC provider not found")
    if not (cfg.client_id and cfg.client_secret and cfg.issuer):
        raise HTTPException(status_code=400, detail="OIDC provider is misconfigured")

    issuer = cfg.issuer.rstrip("/")
    openid_cfg_endpoint = issuer if "well-known" in issuer else f"{issuer}/.well-known/openid-configuration"
    client = OpenID(cfg.client_id, cfg.client_secret, openid_configuration_endpoint=openid_cfg_endpoint, name=provider)

    code_verifier, code_challenge = _generate_pkce_pair()
    state = uuid.uuid4().hex
    redirect_uri = _get_redirect_uri(provider, getattr(cfg, "redirect_path", None))

    authorization_url = await client.get_authorization_url(
        redirect_uri=redirect_uri,
        state=state,
        scope=_get_scopes(getattr(cfg, "scopes", None)),
        extras_params={
            **(getattr(cfg, "extra_authorize_params", {}) or {}),
            **({"code_challenge": code_challenge, "code_challenge_method": "S256"} if getattr(cfg, "pkce", True) else {}),
        },
    )

    response = JSONResponse({"authorization_url": authorization_url})
    _issue_state_cookie(provider, response, state)
    if getattr(cfg, "pkce", True):
        _issue_pkce_cookies(provider, response, code_verifier)
    return response


async def handle_callback(provider: str, request: Request, code: Optional[str], state: Optional[str], user_manager) -> RedirectResponse:
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")

    cookie_state = _read_state_cookie(provider, request)
    if not cookie_state or cookie_state != state:
        raise HTTPException(status_code=400, detail="Invalid state")

    # Google
    if provider == "google":
        g = settings.bow_config.google_oauth
        if not g or not g.enabled:
            raise HTTPException(status_code=404, detail="Google OAuth not enabled")
        client = GoogleOAuth2(g.client_id, g.client_secret)
        redirect_uri = _get_redirect_uri(provider)
        try:
            token = await client.get_access_token(code, redirect_uri)
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.json()
            except Exception:
                body = e.response.text
            await _audit_auth_event(
                action="auth.login_failed",
                request=request,
                details={"provider": provider, "reason": "token_exchange_failed"},
            )
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {body}")

        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        expires_in = token.get("expires_in")
        expires_at = int(time.time()) + int(expires_in) if isinstance(expires_in, int) else None

        try:
            account_id, account_email = await client.get_id_email(access_token)
        except Exception as e:
            await _audit_auth_event(
                action="auth.login_failed",
                request=request,
                details={"provider": provider, "reason": "user_info_fetch_failed"},
            )
            raise HTTPException(status_code=400, detail=f"Failed to fetch user info: {e}")

        # Use user manager to link/create
        try:
            user = await user_manager.oauth_callback(
                oauth_name=provider,
                access_token=access_token,
                account_id=str(account_id),
                account_email=str(account_email),
                expires_at=expires_at,
                refresh_token=refresh_token,
                request=request,
            )
        except HTTPException as e:
            if isinstance(e.detail, dict) and e.detail.get("code") == "invitation_required":
                msg = urllib.parse.quote(e.detail.get("message") or "You must be invited to create an account.")
                return RedirectResponse(f"{settings.bow_config.base_url}/users/sign-in?error={msg}", status_code=303)
            raise

        await _audit_auth_event(
            action="auth.login",
            request=request,
            user_id=str(user.id),
            details={"provider": provider, "email": str(account_email)},
        )

        strategy = get_jwt_strategy()
        jwt_token = await strategy.write_token(user)
        return RedirectResponse(f"{settings.bow_config.base_url}/users/sign-in?access_token={jwt_token}&email={user.email}", status_code=303)

    # OIDC providers
    cfg = _get_oidc_config(provider)
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=404, detail="OIDC provider not found")

    issuer = cfg.issuer.rstrip("/")
    openid_cfg_endpoint = issuer if "well-known" in issuer else f"{issuer}/.well-known/openid-configuration"
    client = OpenID(cfg.client_id, cfg.client_secret, openid_configuration_endpoint=openid_cfg_endpoint, name=provider)
    redirect_uri = _get_redirect_uri(provider, getattr(cfg, "redirect_path", None))

    try:
        token_endpoint = (await _discover_endpoints(openid_cfg_endpoint))["token_endpoint"]
        async with httpx.AsyncClient(timeout=10) as http:
            data: Dict[str, Any] = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
            scopes = _get_scopes(getattr(cfg, "scopes", None))
            if scopes:
                data["scope"] = " ".join(scopes)
            if getattr(cfg, "pkce", True):
                code_verifier = _read_pkce_cookie(provider, request)
                if not code_verifier:
                    raise HTTPException(status_code=400, detail="Missing PKCE verifier")
                data["code_verifier"] = code_verifier
            data.update(getattr(cfg, "extra_token_params", {}) or {})

            auth = None
            if getattr(cfg, "client_auth_method", "basic") == "basic":
                auth = httpx.BasicAuth(cfg.client_id, cfg.client_secret)
            else:
                data["client_id"] = cfg.client_id
                data["client_secret"] = cfg.client_secret

            resp = await http.post(token_endpoint, data=data, auth=auth, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise HTTPException(status_code=400, detail=f"Token exchange failed: {detail}")
            token = resp.json()
    except Exception as e:
        await _audit_auth_event(
            action="auth.login_failed",
            request=request,
            details={"provider": provider, "reason": "token_exchange_failed"},
        )
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    access_token = token.get("access_token")
    refresh_token = token.get("refresh_token")
    expires_in = token.get("expires_in")
    expires_at = int(time.time()) + int(expires_in) if isinstance(expires_in, int) else None

    # Extract user identity from id_token first (reliable for Entra/OIDC),
    # then fall back to userinfo endpoint.
    import jwt as pyjwt
    account_id = None
    account_email = None

    id_token_raw = token.get("id_token")
    if id_token_raw:
        id_claims = pyjwt.decode(id_token_raw, options={"verify_signature": False})
        uid_claim = getattr(cfg, "uid_claim", "sub") or "sub"
        account_id = id_claims.get(uid_claim) or id_claims.get("sub")
        account_email = (
            id_claims.get("email")
            or id_claims.get("preferred_username")
            or id_claims.get("upn")
        )
        _auth_logger.info(f"OIDC id_token claims: sub={account_id}, email={account_email}")

    # Fall back to userinfo endpoint if id_token didn't provide what we need
    if not account_id or not account_email:
        try:
            uid, email = await client.get_id_email(access_token)
            account_id = account_id or uid
            account_email = account_email or email
        except Exception as e:
            _auth_logger.warning(f"OIDC userinfo fallback failed: {e}")
            if not account_id or not account_email:
                await _audit_auth_event(
                    action="auth.login_failed",
                    request=request,
                    details={"provider": provider, "reason": "user_info_fetch_failed"},
                )
                raise HTTPException(status_code=400, detail=f"Failed to fetch user info: {e}")

    if not account_email:
        await _audit_auth_event(
            action="auth.login_failed",
            request=request,
            details={"provider": provider, "reason": "no_email_in_token"},
        )
        raise HTTPException(status_code=400, detail="Could not determine email from OIDC provider. Ensure the 'email' scope is configured.")

    try:
        user = await user_manager.oauth_callback(
            oauth_name=provider,
            access_token=access_token,
            account_id=str(account_id),
            account_email=str(account_email),
            expires_at=expires_at,
            refresh_token=refresh_token,
            request=request,
        )
    except HTTPException as e:
        if isinstance(e.detail, dict) and e.detail.get("code") == "invitation_required":
            msg = urllib.parse.quote(e.detail.get("message") or "You must be invited to create an account.")
            return RedirectResponse(f"{settings.bow_config.base_url}/users/sign-in?error={msg}", status_code=303)
        raise

    await _audit_auth_event(
        action="auth.login",
        request=request,
        user_id=str(user.id),
        details={"provider": provider, "email": str(account_email)},
    )

    # OIDC group sync — sync group claims from id_token into BOW Groups
    if getattr(cfg, 'sync_groups', False):
        try:
            await _sync_oidc_groups_on_login(
                cfg=cfg,
                token=token,
                access_token=access_token,
                user=user,
            )
        except Exception as e:
            _auth_logger.warning(f"OIDC group sync failed for user {user.id}: {e}", exc_info=True)

    # Phase 2: Auto-provision OAuth credentials for Entra-based data sources via OBO
    if access_token and _is_entra_provider(provider):
        try:
            from app.services.connection_oauth_service import auto_provision_connection_credentials
            from app.dependencies import async_session_maker
            async with async_session_maker() as db:
                await auto_provision_connection_credentials(db, user, access_token)
        except Exception as e:
            _auth_logger.warning(f"OBO auto-provision after login failed for user {user.id}: {e}")

    strategy = get_jwt_strategy()
    jwt_token = await strategy.write_token(user)
    return RedirectResponse(f"{settings.bow_config.base_url}/users/sign-in?access_token={jwt_token}&email={user.email}", status_code=303)


async def _sync_oidc_groups_on_login(cfg, token: dict, access_token: str, user) -> None:
    """Extract group claims from id_token and sync into BOW Groups."""
    import jwt as pyjwt
    from app.dependencies import async_session_maker
    from app.ee.oidc.group_sync_service import sync_user_oidc_groups

    id_token_raw = token.get("id_token")
    if not id_token_raw:
        _auth_logger.debug("OIDC group sync: no id_token in token response, skipping")
        return

    # Decode without signature verification — token was already validated by the provider
    id_claims = pyjwt.decode(id_token_raw, options={"verify_signature": False})
    group_claim = getattr(cfg, 'group_claim', 'groups')
    group_ids = id_claims.get(group_claim, [])

    # Handle Entra group overage (>200 groups — groups omitted, _claim_names present)
    if not group_ids and "_claim_names" in id_claims:
        _auth_logger.info("OIDC group sync: group overage detected, falling back to Graph API")
        # For overage, we need to get all groups via Graph — use /me/memberOf with delegated token first,
        # fall back to client credentials
        try:
            from app.ee.oidc.graph_client import resolve_group_names
            group_names_map = await resolve_group_names(access_token)
        except Exception:
            group_names_map = {}
        group_ids = list(group_names_map.keys())
    else:
        group_names_map = None

    if not group_ids:
        _auth_logger.debug(f"OIDC group sync: no groups in claim '{group_claim}', skipping")
        return

    # Resolve group display names via Graph API using client credentials
    if group_names_map is None and getattr(cfg, 'resolve_group_names', False):
        try:
            from app.ee.oidc.graph_client import resolve_group_names_by_ids
            # Extract tenant_id from issuer URL
            issuer = getattr(cfg, 'issuer', '') or ''
            parts = issuer.rstrip('/').split('/')
            tenant_id = parts[-2] if len(parts) >= 2 and parts[-1] == 'v2.0' else parts[-1]
            group_names_map = await resolve_group_names_by_ids(
                group_ids=group_ids,
                tenant_id=tenant_id,
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
            )
        except Exception as e:
            _auth_logger.warning(f"OIDC group sync: Graph API name resolution failed: {e}")
            group_names_map = None

    # Get user's org — use the first membership's org
    async with async_session_maker() as db:
        from sqlalchemy import select
        from app.models.membership import Membership
        stmt = select(Membership.organization_id).where(
            Membership.user_id == str(user.id),
            Membership.deleted_at.is_(None),
        )
        org_id = (await db.execute(stmt)).scalar_one_or_none()

        if not org_id:
            _auth_logger.debug(f"OIDC group sync: user {user.id} has no org membership, skipping")
            return

        await sync_user_oidc_groups(
            db=db,
            user_id=str(user.id),
            organization_id=str(org_id),
            group_ids=group_ids,
            group_names=group_names_map,
        )


async def _discover_endpoints(openid_cfg_endpoint: str) -> Dict[str, str]:
    # openid_cfg_endpoint may already be the well-known URL
    url = openid_cfg_endpoint if "well-known" in openid_cfg_endpoint else f"{openid_cfg_endpoint}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.json()


