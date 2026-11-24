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
async def authorize(provider: str, request: Request, db: AsyncSession = Depends(get_async_db)):
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
    # Started inside a host application's sign-in window (?embed_host=): the
    # intent rides with THIS request, bound to its browser token, in a cookie
    # that comes back on the cross-site POST as the request's own does.
    from app.core import workspace_access

    host = await workspace_access.host_of_request(request)
    if host is not None:
        response.set_cookie(
            _intent_cookie(provider), workspace_access.issue_signup_intent(host, saml.digest(browser)),
            max_age=300, secure=True, httponly=True, samesite="none", path=f"/api/auth/saml/{provider}",
        )
    else:
        response.delete_cookie(_intent_cookie(provider), path=f"/api/auth/saml/{provider}", secure=True,
                               httponly=True, samesite="none")
    return response


def _intent_cookie(provider: str) -> str:
    return f"{cookie_name(provider)}_embed"


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
        browser = request.cookies.get(cookie_name(provider))
        from app.core import workspace_access

        # The account this assertion creates is stamped as it is written.
        intent = workspace_access.decode_signup_intent(
            request.cookies.get(_intent_cookie(provider)), saml.digest(browser) if browser else None)
        async with workspace_access.signup_intent_scope(intent, request):
            user = await saml.finish_login(cfg, post, browser, db)
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
    response.delete_cookie(
        _intent_cookie(provider), path=f"/api/auth/saml/{provider}", secure=True, httponly=True, samesite="none"
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
