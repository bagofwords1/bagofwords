from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi import Depends

from app.services.auth_providers import build_authorize_url, handle_callback
from app.core.auth import get_user_manager

router = APIRouter()


@router.get("/auth/{provider}/authorize")
async def authorize(provider: str, request: Request) -> JSONResponse:
    return await build_authorize_url(provider, request)


@router.get("/auth/{provider}/callback")
async def callback(provider: str, request: Request, code: str | None = None, state: str | None = None, user_manager=Depends(get_user_manager)) -> RedirectResponse:
    return await handle_callback(provider, request, code, state, user_manager)


