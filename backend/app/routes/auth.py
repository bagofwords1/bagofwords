from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi import Depends
from pydantic import BaseModel

from app.services.auth_providers import build_authorize_url, handle_callback
from app.core.auth import get_user_manager

router = APIRouter()


class LoginCodeExchange(BaseModel):
    login_code: str


@router.get("/auth/{provider}/authorize")
async def authorize(provider: str, request: Request) -> JSONResponse:
    return await build_authorize_url(provider, request)


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
