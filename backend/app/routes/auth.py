from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi import Depends
from pydantic import BaseModel

from app.services.auth_providers import build_authorize_url, handle_callback
from app.core.auth import get_user_manager

from app.routes.saml_auth import router as saml_router

router = APIRouter()
router.include_router(saml_router)


class LoginCodeExchange(BaseModel):
    login_code: str


class LoginCodeHandoff(BaseModel):
    login_code: str
    code_challenge: str


@router.get("/auth/{provider}/authorize")
async def authorize(provider: str, request: Request) -> JSONResponse:
    response = await build_authorize_url(provider, request)
    # Started inside a host application's sign-in window: bind that to this
    # authorization, so an account the callback creates gets the host's role.
    from app.core.workspace_access import bind_oidc_authorization
    await bind_oidc_authorization(provider, request, response)
    return response


@router.get("/auth/{provider}/callback")
async def callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    user_manager=Depends(get_user_manager),
) -> RedirectResponse:
    return await handle_callback(
        provider, request, code, state, user_manager,
        error=error, error_description=error_description,
    )




@router.post("/auth/exchange")
async def exchange_login_code(payload: LoginCodeExchange) -> JSONResponse:
    """Trade a single-use SSO login code for the session token.

    The second half of keeping the JWT out of the redirect URL: the callback
    redirects with a short-lived code and the SPA POSTs it here, so the token
    itself only ever travels in a request/response body.
    """
    from app.services.login_exchange_service import redeem_login_code

    access_token = await redeem_login_code(payload.login_code)
    if not access_token:
        raise HTTPException(status_code=400, detail="Invalid or expired login code")

    return JSONResponse({"access_token": access_token, "token_type": "bearer"})


@router.post("/auth/exchange/handoff", status_code=204)
async def hand_off_login_code(payload: LoginCodeHandoff) -> Response:
    """Pass a login code from the sign-in popup to the framed page that opened it.

    The popup spends its code against the frame's challenge; the frame then
    redeems its own verifier at ``/auth/exchange``. See
    ``login_exchange_service.hand_off_login_code``.
    """
    from app.services.login_exchange_service import hand_off_login_code as hand_off

    if not await hand_off(payload.login_code, payload.code_challenge):
        raise HTTPException(status_code=400, detail="Invalid or expired login code")
    return Response(status_code=204)


@router.get("/auth/exchange/handoff/{code_challenge}")
async def login_code_handoff_status(code_challenge: str) -> JSONResponse:
    """Whether a handoff is waiting (``ready``) or was taken (``consumed``).

    The frame polls it to learn the popup is done; the popup polls it to learn
    the frame took the session, and only then closes.
    """
    from app.services.login_exchange_service import handoff_status

    return JSONResponse(
        {"status": await handoff_status(code_challenge)},
        headers={"Cache-Control": "no-store"},
    )
