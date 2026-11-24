import base64
import hashlib
import os
import time
import uuid

from typing import Optional, Dict, Any, Tuple

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from httpx_oauth.clients.openid import OpenID
from httpx_oauth.clients.google import GoogleOAuth2

from app.settings.config import settings
from app.settings.bow_config import brand_for_issuer
from app.core.auth import get_jwt_strategy

import logging as _logging
from app.errors.redact import redact_error

_auth_logger = _logging.getLogger(__name__)


async def _audit_auth_event(
    action: str,
    request: Request,
    user_id: Optional[str] = None,
    details: dict | None = None,
) -> None:
    """Fire-and-forget audit log for provider logins (OAuth/OIDC/SAML).

    Routed by app.ee.audit.security_events: once per organization of a known
    account, otherwise without an organization (platform stream only).
    """
    from app.ee.audit.security_events import record_security_event

    d = dict(details or {})
    failed = action.endswith("_failed")
    reason = d.pop("reason", None) if failed else None
    await record_security_event(
        action, request=request, user_id=user_id,
        outcome="failure" if failed else "success",
        failure_reason=(reason or "provider_login_failed") if failed else None,
        method=f"oauth:{d.get('provider')}" if d.get("provider") else "oauth",
        details=d,
    )


def _cookie_secure() -> bool:
    base_url = (settings.bow_config.base_url or "").lower()
    return base_url.startswith("https://")


def _get_scopes(scopes: Optional[list]) -> list:
    return scopes or ["openid", "profile", "email"]


def _get_redirect_uri(provider: str, request: Request, redirect_path: Optional[str] = None) -> str:
    from app.core.base_url import derive_request_base_url
    path = redirect_path or f"/api/auth/{provider}/callback"
    return f"{derive_request_base_url(request)}{path}"


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


def _read_login_hint(request: Request) -> Optional[str]:
    """Read an optional ``login_hint`` off the authorize request.

    An embedded app already knows which user it is opening BOW for, so it can
    name them here and skip the provider's account picker — the difference
    between a silent SSO round trip and a chooser screen for anyone signed in
    to more than one account. Only this one parameter is honoured; the rest of
    the authorize URL is ours to build.
    """
    value = (request.query_params.get("login_hint") or "").strip()
    # Entra echoes the hint into a redirect; a CR/LF would let a caller graft
    # extra header lines onto it, and the length cap keeps a hostile value from
    # blowing past the provider's URL limit.
    if not value or len(value) > 320 or any(c in value for c in "\r\n"):
        return None
    return value


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
    return brand_for_issuer(cfg.issuer) == "microsoft"


def _is_google_provider(provider_name: str) -> bool:
    """Check if a login provider is Google — either the built-in google_oauth
    flow or a generic OIDC provider whose issuer is accounts.google.com."""
    if provider_name == "google":
        return True
    cfg = _get_oidc_config(provider_name)
    if not cfg:
        return False
    return brand_for_issuer(cfg.issuer) == "google"


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
        redirect_uri = _get_redirect_uri(provider, request)
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
    redirect_uri = _get_redirect_uri(provider, request, getattr(cfg, "redirect_path", None))

    login_hint = _read_login_hint(request)

    authorization_url = await client.get_authorization_url(
        redirect_uri=redirect_uri,
        state=state,
        scope=_get_scopes(getattr(cfg, "scopes", None)),
        extras_params={
            **(getattr(cfg, "extra_authorize_params", {}) or {}),
            # A per-request hint names the actual user, so it beats the static
            # config default rather than the other way round.
            **({"login_hint": login_hint} if login_hint else {}),
            **({"code_challenge": code_challenge, "code_challenge_method": "S256"} if getattr(cfg, "pkce", True) else {}),
        },
    )

    response = JSONResponse({"authorization_url": authorization_url})
    _issue_state_cookie(provider, response, state)
    if getattr(cfg, "pkce", True):
        _issue_pkce_cookies(provider, response, code_verifier)
    return response


def _error_code_for(detail: Any) -> str:
    """Map an HTTPException detail to a fixed error code for the sign-in page.

    Only a code from a closed set ever reaches the browser: the page renders its
    own localized message for it. Reflecting provider- or exception-supplied
    text into the URL let anyone put arbitrary content (and, via the old
    ``v-html`` sink, script) on the sign-in page.
    """
    if isinstance(detail, dict) and detail.get("code") in _CALLBACK_ERROR_CODES:
        return detail["code"]
    if isinstance(detail, str) and detail.startswith("Could not determine email"):
        return "oauth_email_missing"
    return "oauth_login_failed"


_CALLBACK_ERROR_CODES = {"invitation_required", "oauth_email_missing", "oauth_login_failed", "account_link_required", "invite_link_required"}


def _error_redirect(code: str) -> RedirectResponse:
    """Redirect back to the sign-in page with a fixed, non-reflective error code."""
    if code not in _CALLBACK_ERROR_CODES:
        code = "oauth_login_failed"
    return RedirectResponse(f"/users/sign-in?error_code={code}", status_code=303)


async def handle_callback(
    provider: str,
    request: Request,
    code: Optional[str],
    state: Optional[str],
    user_manager,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> RedirectResponse:
    """Public entrypoint: convert any callback failure into a user-visible redirect.

    Without this, errors raised during the OAuth/OIDC flow (e.g. a user without an
    invite signing in via EntraID/Okta) would surface as a raw JSON error page
    instead of being shown on the sign-in/sign-up screen.
    """
    # The provider may redirect back with an OAuth error (e.g. Entra AADSTS500011)
    # instead of a code. Log the provider's message for the admin; the browser
    # only gets a fixed code (this branch runs before any state check, so the
    # text is attacker-controllable).
    if error:
        message = error_description or error
        _auth_logger.warning(f"OAuth callback error for provider={provider}: {message}")
        await _audit_auth_event(
            action="auth.login_failed",
            request=request,
            details={"provider": provider, "reason": "provider_error", "error": error},
        )
        return _error_redirect("oauth_login_failed")

    try:
        return await _handle_callback(provider, request, code, state, user_manager)
    except HTTPException as e:
        _auth_logger.warning(f"OAuth callback failed for provider={provider}: {e.detail}")
        return _error_redirect(_error_code_for(e.detail))
    except Exception as e:
        _auth_logger.error(f"Unexpected error in OAuth callback for provider={provider}: {e}", exc_info=True)
        return _error_redirect("oauth_login_failed")


async def _handle_callback(provider: str, request: Request, code: Optional[str], state: Optional[str], user_manager) -> RedirectResponse:
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
        redirect_uri = _get_redirect_uri(provider, request)
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
            raise HTTPException(status_code=400, detail=f"Failed to fetch user info: {redact_error(e)}")

        # Use user manager to link/create. Any failure (e.g. missing invite) is
        # handled by the outer wrapper and surfaced on the sign-in page.
        user = await user_manager.oauth_callback(
            oauth_name=provider,
            access_token=access_token,
            account_id=str(account_id),
            account_email=str(account_email),
            expires_at=expires_at,
            refresh_token=refresh_token,
            request=request,
        )

        await _audit_auth_event(
            action="auth.login",
            request=request,
            user_id=str(user.id),
            details={"provider": provider, "email": str(account_email)},
        )

        # Google profile / job-info sync — fetch the signed-in user's profile
        # (userinfo + People API) and store it on their Membership when the org
        # has opted in. Uses the fresh login token in hand.
        if access_token:
            try:
                await _sync_google_profile_on_login(user=user, access_token=access_token)
            except Exception as e:
                _auth_logger.warning(f"Google profile sync after login failed for user {user.id}: {e}")

        await _record_login(user)

        await _signed_up_through_an_application(request, user)
        return await _login_redirect(user)

    # OIDC providers
    cfg = _get_oidc_config(provider)
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=404, detail="OIDC provider not found")

    issuer = cfg.issuer.rstrip("/")
    openid_cfg_endpoint = issuer if "well-known" in issuer else f"{issuer}/.well-known/openid-configuration"
    client = OpenID(cfg.client_id, cfg.client_secret, openid_configuration_endpoint=openid_cfg_endpoint, name=provider)
    redirect_uri = _get_redirect_uri(provider, request, getattr(cfg, "redirect_path", None))

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
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {redact_error(e)}")

    access_token = token.get("access_token")
    refresh_token = token.get("refresh_token")
    expires_in = token.get("expires_in")
    expires_at = int(time.time()) + int(expires_in) if isinstance(expires_in, int) else None

    # Extract user identity from id_token first (reliable for Entra/OIDC),
    # then fall back to userinfo endpoint.
    import jwt as pyjwt
    account_id = None
    account_email = None
    # What the provider says about the email itself; None when it said nothing.
    identity_claims = None

    id_token_raw = token.get("id_token")
    if id_token_raw:
        id_claims = pyjwt.decode(id_token_raw, options={"verify_signature": False})
        identity_claims = {
            "email_verified": id_claims.get("email_verified"),
            "xms_edov": id_claims.get("xms_edov"),
            "issuer": id_claims.get("iss"),
        }
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
                raise HTTPException(status_code=400, detail=f"Failed to fetch user info: {redact_error(e)}")

    if not account_email:
        await _audit_auth_event(
            action="auth.login_failed",
            request=request,
            details={"provider": provider, "reason": "no_email_in_token"},
        )
        raise HTTPException(status_code=400, detail="Could not determine email from OIDC provider. Ensure the 'email' scope is configured.")

    # Groups the provider asserts for this sign-in, read once for both the
    # organization mapping and group sync. None = could not be determined.
    org_mapping = getattr(cfg, 'org_mapping', None)
    sync_groups = getattr(cfg, 'sync_groups', False)
    login_groups = None
    if org_mapping or sync_groups:
        try:
            login_groups = await _resolve_login_groups(cfg, token, access_token)
        except Exception as e:
            _auth_logger.warning(f"OIDC group lookup failed for provider={provider}: {e}", exc_info=True)

    # A person the operator's mapping places in an organization does not also
    # need an invitation to sign in for the first time.
    org_mapping_admits = False
    if org_mapping and login_groups is not None:
        try:
            from app.services.oidc_org_mapping import mapping_admits
            org_mapping_admits = await mapping_admits(cfg, login_groups[0], login_groups[1])
        except Exception as e:
            _auth_logger.warning(f"OIDC org mapping lookup failed for provider={provider}: {e}", exc_info=True)

    # Any failure here (e.g. missing invite) is handled by the outer wrapper
    # and surfaced on the sign-in page.
    user = await user_manager.oauth_callback(
        oauth_name=provider,
        access_token=access_token,
        account_id=str(account_id),
        account_email=str(account_email),
        expires_at=expires_at,
        refresh_token=refresh_token,
        request=request,
        identity_claims=identity_claims,
        org_mapping_admits=org_mapping_admits,
    )

    await _audit_auth_event(
        action="auth.login",
        request=request,
        user_id=str(user.id),
        details={"provider": provider, "email": str(account_email)},
    )

    # Record immutable identity for optional external-app exchange. Failure does
    # not change ordinary SSO behavior; exchange stays unavailable until linked.
    if id_token_raw and _is_entra_provider(provider):
        try:
            from app.services.entra_token_exchange import link_sso_identity
            from app.dependencies import async_session_maker
            async with async_session_maker() as identity_db:
                await link_sso_identity(identity_db, user.id, cfg, account_id, id_token_raw)
        except Exception as exc:
            _auth_logger.warning("Entra identity binding unavailable (%s)", type(exc).__name__)

    # Operator-owned placement into organizations (bow-config `org_mapping`).
    # Runs before group/profile sync so they see the memberships it settles.
    # Always called: memberships a since-deleted rule produced are reconciled
    # too (one lookup for a provider that never had a mapping). With rules
    # configured it does nothing while the groups are unknown, so an outage
    # never removes anyone.
    try:
        from app.services.oidc_org_mapping import apply_org_mapping
        await apply_org_mapping(cfg, user, login_groups)
    except Exception as e:
        _auth_logger.warning(f"OIDC org mapping failed for user {user.id}: {e}", exc_info=True)

    # OIDC group sync — sync group claims from id_token into BOW Groups
    if sync_groups and login_groups is not None:
        try:
            await _sync_oidc_groups_on_login(
                cfg=cfg,
                token=token,
                access_token=access_token,
                user=user,
                groups=login_groups,
            )
        except Exception as e:
            _auth_logger.warning(f"OIDC group sync failed for user {user.id}: {e}", exc_info=True)

    # Phase 2: Auto-provision OAuth credentials for Entra-based data sources via OBO.
    # Dispatched to the background loop rather than awaited: the work walks every
    # Entra connection in the org, so awaiting it made login latency scale with
    # connection count (~85s, and a browser timeout, at ~100 connections). Nothing
    # in the login response depends on it — the agent pages poll connection status.
    if access_token and _is_entra_provider(provider):
        try:
            from app.services.connection_oauth_service import schedule_auto_provision
            schedule_auto_provision(str(user.id), access_token)
        except Exception as e:
            _auth_logger.warning(f"OBO auto-provision after login failed for user {user.id}: {e}")

    # Entra ID profile / job-info sync — fetch the signed-in user's Graph /me
    # profile and store it on their Membership when the org has opted in. Uses
    # the fresh delegated token in hand (default-granted User.Read).
    if access_token and _is_entra_provider(provider):
        try:
            await _sync_entra_profile_on_login(user=user, access_token=access_token)
        except Exception as e:
            _auth_logger.warning(f"Entra profile sync after login failed for user {user.id}: {e}")

    # Google profile / job-info sync — same gate for OIDC providers whose
    # issuer is accounts.google.com (the built-in google flow returns above).
    if access_token and _is_google_provider(provider):
        try:
            await _sync_google_profile_on_login(user=user, access_token=access_token)
        except Exception as e:
            _auth_logger.warning(f"Google profile sync after login failed for user {user.id}: {e}")

    await _record_login(user)

    await _signed_up_through_an_application(request, user)
    return await _login_redirect(user)


async def _signed_up_through_an_application(request: Request, user) -> None:
    """An account this sign-in created inside a host application's sign-in
    window gets that application's new-user role before its session exists."""
    from app.core.workspace_access import apply_signup_intent_now

    await apply_signup_intent_now(request, user)


async def _login_redirect(user) -> RedirectResponse:
    """Finish an SSO login by redirecting the SPA to a one-time exchange code.

    The session JWT deliberately does not travel in this URL — it would be
    written to browser history, leak via Referer, and land in the access logs of
    every proxy in front of a self-hosted install, where it stays valid for its
    full 7-day lifetime. The code here is single-use and expires in 60s; the SPA
    trades it for the token over POST /api/auth/exchange.
    """
    from app.services.login_exchange_service import issue_login_code
    from app.core.instance_admin import apply_superuser_policy

    # Every SSO sign-in (OAuth, OIDC, SAML) ends here: instance administrators
    # follow BOW_SUPERUSER_EMAILS at each sign-in.
    await apply_superuser_policy(user)

    strategy = get_jwt_strategy()
    jwt_token = await strategy.write_token(user)
    login_code = await issue_login_code(str(user.id), jwt_token)
    return RedirectResponse(f"/users/sign-in?login_code={login_code}", status_code=303)


async def _record_login(user) -> None:
    from datetime import datetime, timezone
    from app.dependencies import async_session_maker
    from app.models.user import User as UserModel
    from sqlalchemy import update
    try:
        # users.last_login is TIMESTAMP WITHOUT TIME ZONE; asyncpg rejects aware values.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with async_session_maker() as db:
            await db.execute(
                update(UserModel).where(UserModel.id == str(user.id)).values(last_login=now)
            )
            await db.commit()
    except Exception as e:
        _auth_logger.warning(f"Failed to record last_login for user {user.id}: {e}")


async def _live_organization_ids(user) -> list:
    """Every organization the user currently belongs to, oldest membership first."""
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.membership import Membership

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Membership.organization_id).where(
                Membership.user_id == str(user.id),
                Membership.deleted_at.is_(None),
            ).order_by(Membership.created_at.asc(), Membership.id.asc())
        )).scalars().all()
    return list(dict.fromkeys(str(r) for r in rows))


async def _resolve_login_groups(cfg, token: dict, access_token: str):
    """Groups the provider asserts for this sign-in: ``(group_ids, names)``.

    ``names`` maps group id → display name (None when names were not resolved).
    Returns None when the groups cannot be determined (no id_token, or an
    overage whose directory lookup failed) — different from an empty list,
    which means the person is in no group.
    """
    import jwt as pyjwt

    id_token_raw = token.get("id_token")
    if not id_token_raw:
        _auth_logger.debug("OIDC group sync: no id_token in token response, skipping")
        return None

    # Decode without signature verification — token was already validated by the provider
    id_claims = pyjwt.decode(id_token_raw, options={"verify_signature": False})
    group_claim = getattr(cfg, 'group_claim', 'groups')
    group_ids = id_claims.get(group_claim, [])
    if isinstance(group_ids, str):
        group_ids = [group_ids]
    group_ids = [str(g) for g in (group_ids or [])]

    # Handle Entra group overage (>200 groups — groups omitted, _claim_names present)
    if not group_ids and "_claim_names" in id_claims:
        _auth_logger.info("OIDC group sync: group overage detected, falling back to Graph API")
        # For overage, we need to get all groups via Graph — use /me/transitiveMemberOf (nested groups included, like the claim) with the delegated token;
        # fall back to client credentials
        try:
            from app.ee.oidc.graph_client import resolve_group_names
            group_names_map = await resolve_group_names(access_token)
        except Exception:
            group_names_map = {}
        if not group_names_map:
            _auth_logger.warning("OIDC group sync: group overage could not be resolved through Graph")
            return None
        return list(group_names_map.keys()), group_names_map

    if not group_ids:
        _auth_logger.debug(f"OIDC group sync: no groups in claim '{group_claim}'")
        return [], None

    # Resolve group display names via Graph API using client credentials
    group_names_map = None
    if getattr(cfg, 'resolve_group_names', False):
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

    return group_ids, group_names_map


async def _sync_oidc_groups_on_login(cfg, token: dict, access_token: str, user, groups=None) -> None:
    """Sync the sign-in's group claims into BOW Groups, in every organization
    the user belongs to. One organization failing does not stop the others."""
    from app.dependencies import async_session_maker
    from app.ee.oidc.group_sync_service import sync_user_oidc_groups

    if groups is None:
        groups = await _resolve_login_groups(cfg, token, access_token)
    if groups is None:
        return
    group_ids, group_names_map = groups
    if not group_ids:
        return

    org_ids = await _live_organization_ids(user)
    if not org_ids:
        _auth_logger.debug(f"OIDC group sync: user {user.id} has no org membership, skipping")
        return

    for org_id in org_ids:
        try:
            async with async_session_maker() as db:
                await sync_user_oidc_groups(
                    db=db,
                    user_id=str(user.id),
                    organization_id=org_id,
                    group_ids=group_ids,
                    group_names=group_names_map,
                )
        except Exception as e:
            _auth_logger.warning(
                f"OIDC group sync failed for user {user.id} in org {org_id}: {e}", exc_info=True
            )


async def _sync_provider_profile_on_login(
    user,
    access_token: str,
    *,
    config_key: str,
    default_fields: list,
    sync_fn,
    label: str,
) -> None:
    """Fetch a provider profile and store it on the user's org Memberships.

    Provider-agnostic on-login gate, evaluated per organization: an
    organization is skipped unless it has enabled the given profile-sync config
    key, and one organization failing does not stop the others. Uses the fresh
    login token so no stored-token refresh is needed.
    """
    from app.dependencies import async_session_maker

    org_ids = await _live_organization_ids(user)
    if not org_ids:
        _auth_logger.debug(f"{label}: user {user.id} has no org membership, skipping")
        return

    for org_id in org_ids:
        try:
            async with async_session_maker() as db:
                from sqlalchemy import select
                from app.models.organization import Organization
                org = (
                    await db.execute(select(Organization).where(Organization.id == org_id))
                ).scalar_one_or_none()
                if not org:
                    continue
                settings_obj = await org.get_settings(db)
                cfg = (settings_obj.config or {}).get(config_key) or {}
                if not cfg.get("enabled"):
                    _auth_logger.debug(f"{label}: org {org_id} has sync disabled, skipping")
                    continue

                fields = list(cfg.get("fields") or default_fields)

                await sync_fn(
                    db=db,
                    user=user,
                    organization_id=org_id,
                    fields=fields,
                    access_token=access_token,
                )
        except Exception as e:
            _auth_logger.warning(f"{label} failed for user {user.id} in org {org_id}: {e}", exc_info=True)


async def _sync_entra_profile_on_login(user, access_token: str) -> None:
    """Fetch the user's Entra profile and store it on their org Membership."""
    from app.ee.oidc.profile_service import sync_profile_on_login
    from app.schemas.organization_settings_schema import ENTRA_PROFILE_SYNC_DEFAULT_FIELDS

    await _sync_provider_profile_on_login(
        user,
        access_token,
        config_key="entra_profile_sync",
        default_fields=ENTRA_PROFILE_SYNC_DEFAULT_FIELDS,
        sync_fn=sync_profile_on_login,
        label="Entra profile sync",
    )


async def _sync_google_profile_on_login(user, access_token: str) -> None:
    """Fetch the user's Google profile and store it on their org Membership."""
    from app.ee.oidc.google_profile_service import sync_profile_on_login
    from app.schemas.organization_settings_schema import GOOGLE_PROFILE_SYNC_DEFAULT_FIELDS

    await _sync_provider_profile_on_login(
        user,
        access_token,
        config_key="google_profile_sync",
        default_fields=GOOGLE_PROFILE_SYNC_DEFAULT_FIELDS,
        sync_fn=sync_profile_on_login,
        label="Google profile sync",
    )


async def _discover_endpoints(openid_cfg_endpoint: str) -> Dict[str, str]:
    # openid_cfg_endpoint may already be the well-known URL
    url = openid_cfg_endpoint if "well-known" in openid_cfg_endpoint else f"{openid_cfg_endpoint}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.json()


