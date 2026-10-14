"""Global exception handlers that convert typed errors into JSON responses."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .app_error import AppError


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
        headers=exc.headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire AppError handling into the FastAPI app.

    Kept as a function so tests and alternative app factories can share the
    same registration without duplicating decorator wiring in main.py.
    """
    app.add_exception_handler(AppError, app_error_handler)
