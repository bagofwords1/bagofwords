import base64
import hashlib
import os
import time
import uuid

from typing import Optional, Tuple, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from httpx_oauth.clients.openid import OpenID
import httpx

from app.settings.config import settings
from app.core.auth import get_user_manager, get_jwt_strategy
from app.models.user import User


router = APIRouter()


def _get_provider_config(name: str):
    providers = getattr(settings.bow_config, "oidc_providers", []) or []
    for p in providers:
        if p.name == name:
            return p
    return None


def _build_openid_client(provider) -> OpenID:
    issuer = provider.issuer.rstrip("/")
    openid_cfg_endpoint = (
        issuer if "well-known" in issuer else issuer + "/.well-known/openid-configuration"
    )
    return OpenID(
        provider.client_id,
        provider.client_secret,
        openid_configuration_endpoint=openid_cfg_endpoint,
        name=provider.name,
    )


async def _get_oidc_endpoints(provider) -> Dict[str, str]:
    issuer = provider.issuer.rstrip("/")
    if getattr(provider, "discovery", True):
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                config_url = (
                    issuer if "well-known" in issuer else f"{issuer}/.well-known/openid-configuration"
                )
                resp = await http.get(config_url)
                resp.raise_for_status()
                return resp.json()
        except Exception:
            # Fallback below
            pass
    # Fallback to conventional Okta endpoints
    return {
        "token_endpoint": f"{issuer}/v1/token",
        "userinfo_endpoint": f"{issuer}/v1/userinfo",
        "authorization_endpoint": f"{issuer}/v1/authorize",
    }


def _generate_pkce_pair() -> Tuple[str, str]:
    # Create a high-entropy code_verifier (43-128 chars, base64url)
    verifier_bytes = os.urandom(64)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).decode().rstrip("=")
    # Create S256 code_challenge
    challenge = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).decode().rstrip("=")
    return code_verifier, code_challenge


def _cookie_secure() -> bool:
    base_url = (settings.bow_config.base_url or "").lower()
    return base_url.startswith("https://")


def _get_scopes(cfg) -> list[str]:
    scopes = getattr(cfg, "scopes", ["openid", "profile", "email"]) or ["openid", "profile", "email"]
    return scopes


def _get_redirect_uri(provider: str, cfg) -> str:
    redirect_path = getattr(cfg, "redirect_path", None) or f"/api/auth/{provider}/callback"
    return f"{settings.bow_config.base_url}{redirect_path}"


@router.get("/{provider}/authorize")
async def oidc_authorize(provider: str, request: Request):
    cfg = _get_provider_config(provider)
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=404, detail="OIDC provider not found")
    if not (cfg.client_id and cfg.client_secret and cfg.issuer):
        raise HTTPException(status_code=400, detail="OIDC provider is misconfigured")

    client = _build_openid_client(cfg)

    code_verifier, code_challenge = _generate_pkce_pair()
    state = uuid.uuid4().hex
    redirect_uri = _get_redirect_uri(provider, cfg)

    authorization_url = await client.get_authorization_url(
        redirect_uri=redirect_uri,
        state=state,
        scope=_get_scopes(cfg),
        extras_params={
            **(getattr(cfg, "extra_authorize_params", {}) or {}),
            **({"code_challenge": code_challenge, "code_challenge_method": "S256"} if getattr(cfg, "pkce", True) else {}),
        },
    )

    response = JSONResponse({"authorization_url": authorization_url})
    # short-lived (5 min) cookies to carry state+verifier; HttpOnly to keep out of JS
    response.set_cookie(
        key=f"oidc_{provider}_state",
        value=state,
        max_age=300,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path=f"/api/auth/{provider}",
    )
    response.set_cookie(
        key=f"oidc_{provider}_verifier",
        value=code_verifier,
        max_age=300,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path=f"/api/auth/{provider}",
    )
    return response


@router.get("/{provider}/callback")
async def oidc_callback(
    provider: str,
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    user_manager=Depends(get_user_manager),
):
    cfg = _get_provider_config(provider)
    if not cfg or not cfg.enabled:
        raise HTTPException(status_code=404, detail="OIDC provider not found")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")
    cookie_state = request.cookies.get(f"oidc_{provider}_state")
    code_verifier = request.cookies.get(f"oidc_{provider}_verifier")
    if not cookie_state or not code_verifier or cookie_state != state:
        raise HTTPException(status_code=400, detail="Invalid state or verifier")

    client = _build_openid_client(cfg)
    redirect_uri = _get_redirect_uri(provider, cfg)

    # Exchange code for tokens with PKCE
    try:
        # Manual token exchange for both auth methods so we can surface error bodies
        token_endpoint = (await _get_oidc_endpoints(cfg))["token_endpoint"]
        async with httpx.AsyncClient(timeout=10) as http:
            data: Dict[str, Any] = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }
            # Some providers expect scope to be echoed on token request
            scopes = _get_scopes(cfg)
            if scopes:
                data["scope"] = " ".join(scopes)
            if getattr(cfg, "pkce", True):
                data["code_verifier"] = code_verifier
            data.update(getattr(cfg, "extra_token_params", {}) or {})

            auth = None
            if getattr(cfg, "client_auth_method", "basic") == "basic":
                auth = httpx.BasicAuth(cfg.client_id, cfg.client_secret)
            else:
                # client_secret_post: credentials in body
                data["client_id"] = cfg.client_id
                data["client_secret"] = cfg.client_secret

            resp = await http.post(
                token_endpoint,
                data=data,
                auth=auth,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            # If error, include JSON body for debugging
            if resp.status_code >= 400:
                detail = None
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise HTTPException(status_code=400, detail=f"Token exchange failed: {detail}")
            token = resp.json()
    except httpx.HTTPStatusError as e:
        # Surface provider error body for easier debugging (invalid_grant, redirect mismatch, PKCE, etc.)
        body = None
        try:
            body = e.response.json()
        except Exception:
            try:
                body = e.response.text
            except Exception:
                body = str(e)
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {body}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    access_token = token.get("access_token")
    refresh_token = token.get("refresh_token")
    expires_in = token.get("expires_in")
    expires_at: Optional[int] = None
    if isinstance(expires_in, int):
        expires_at = int(time.time()) + int(expires_in)

    try:
        account_id, account_email = await client.get_id_email(access_token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch user info: {e}")

    # Optional override for uid claim
    if getattr(cfg, "uid_claim", None) and cfg.uid_claim != "sub":
        try:
            userinfo_endpoint = (await _get_oidc_endpoints(cfg))["userinfo_endpoint"]
            async with httpx.AsyncClient(timeout=10) as http:
                ui = await http.get(userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"})
                ui.raise_for_status()
                data = ui.json()
                account_id = str(data.get(cfg.uid_claim) or account_id)
                account_email = str(data.get("email") or account_email)
        except Exception:
            pass

    # Reuse your existing user creation/linking logic
    try:
        user: User = await user_manager.oauth_callback(
            oauth_name=provider,
            access_token=access_token,
            account_id=str(account_id),
            account_email=str(account_email),
            expires_at=expires_at,
            refresh_token=refresh_token,
            request=request,
        )
    except HTTPException as e:
        # If invitation is required, redirect with a friendly message
        if isinstance(e.detail, dict) and e.detail.get("code") == "invitation_required":
            error_msg = "You must be invited to create an account."
            redirect_url = f"{settings.bow_config.base_url}/users/sign-in?error={httpx.QueryParams({'error': error_msg}).get('error')}"
            return RedirectResponse(url=redirect_url, status_code=303)
        raise

    # Create our app JWT and redirect to frontend
    strategy = get_jwt_strategy()
    jwt_token = await strategy.write_token(user)

    redirect_url = f"{settings.bow_config.base_url}/users/sign-in?access_token={jwt_token}&email={user.email}"
    response = RedirectResponse(url=redirect_url, status_code=303)
    # Clear temp cookies
    response.delete_cookie(key=f"oidc_{provider}_state", path=f"/api/auth/{provider}")
    response.delete_cookie(key=f"oidc_{provider}_verifier", path=f"/api/auth/{provider}")
    return response


