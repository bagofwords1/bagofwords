"""SAML endpoints: never expose protocol errors or assertions to clients/logs."""

import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.errors import AppError, ErrorCode
from app.services import saml_service as saml
from app.services.auth_providers import _login_redirect, _audit_auth_event

router = APIRouter(prefix="/auth/saml", tags=["auth"])
logger = logging.getLogger(__name__)


def cookie_name(provider):
    return f"bow_saml_{provider}"


@router.get("/{provider}/metadata")
async def metadata(provider: str):
    cfg = saml.provider_config(provider)
    try:
        return Response(
            await saml.metadata(cfg), media_type="application/samlmetadata+xml", headers={"Cache-Control": "no-store"}
        )
    except Exception:
        raise AppError.bad_request(ErrorCode.SAML_UNAVAILABLE, "SSO configuration could not be loaded")


@router.get("/{provider}/authorize")
async def authorize(provider: str, db: AsyncSession = Depends(get_async_db)):
    cfg = saml.provider_config(provider)
    try:
        url, browser = await saml.start_login(cfg, db)
    except Exception as exc:
        logger.warning(
            "SAML request rejected: %s", str(exc) if isinstance(exc, saml.SAMLFailure) else type(exc).__name__
        )
        await db.rollback()
        raise AppError.bad_request(ErrorCode.SAML_UNAVAILABLE, "SSO configuration could not be loaded")
    response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
    # SAML POST is cross-site, so Lax cookies (used by OIDC GET callbacks) would
    # be absent. Secure + None is deliberate; browser correlation is mandatory.
    response.set_cookie(
        cookie_name(provider),
        browser,
        max_age=300,
        secure=True,
        httponly=True,
        samesite="none",
        path=f"/api/auth/saml/{provider}",
    )
    return response


@router.post("/{provider}/acs")
async def acs(provider: str, request: Request, db: AsyncSession = Depends(get_async_db)):
    cfg = saml.provider_config(provider)
    try:
        # Bound even chunked request bodies before parsing form/base64/XML.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > saml.MAX_XML_BYTES:
                raise ValueError("Response too large")
        from urllib.parse import parse_qs

        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise ValueError("Unsupported binding")
        fields = parse_qs(body.decode(), max_num_fields=10)
        if any(len(v) != 1 for v in fields.values()):
            raise ValueError("Duplicate fields")
        post = {k: v[0] for k, v in fields.items()}
        user = await saml.finish_login(cfg, post, request.cookies.get(cookie_name(provider)), db)
        response = await _login_redirect(user)
        await _audit_auth_event("auth.login", request, str(user.id), {"provider": provider, "protocol": "saml"})
    except Exception as exc:
        logger.warning(
            "SAML request rejected: %s", str(exc) if isinstance(exc, saml.SAMLFailure) else type(exc).__name__
        )
        await db.rollback()
        await _audit_auth_event("auth.login_failed", request, details={"provider": provider, "protocol": "saml"})
        response = RedirectResponse("/users/sign-in?error_code=saml_login_failed", status_code=303)
    response.delete_cookie(
        cookie_name(provider), path=f"/api/auth/saml/{provider}", secure=True, httponly=True, samesite="none"
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
