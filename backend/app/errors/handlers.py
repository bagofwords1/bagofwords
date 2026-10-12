"""Global exception handlers that convert typed errors into JSON responses."""
from __future__ import annotations

import logging
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, DataError

from .app_error import AppError
from .codes import ErrorCode

logger = logging.getLogger("app.errors")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
        headers=exc.headers,
    )


def _request_id(request: Request) -> str:
    incoming = request.headers.get("x-request-id") or ""
    return incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex


def _is_data_error(exc: DBAPIError) -> bool:
    """Driver rejected a *value* (SQLSTATE class 22: too long, NUL byte,
    out of range, bad encoding): a client input problem, not a server bug."""
    if isinstance(exc, DataError):
        return True
    orig = getattr(exc, "orig", None)
    code = str(getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None) or "")
    return code.startswith("22")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: never send exception text or a traceback to the
    client. The full traceback is logged server-side under ``request_id`` so
    an operator can correlate a user report with the log line."""
    request_id = _request_id(request)
    if isinstance(exc, DBAPIError) and _is_data_error(exc):
        logger.warning(
            "Rejected invalid input [request_id=%s] %s %s: %s",
            request_id, request.method, request.url.path, type(exc).__name__,
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid input",
                "error_code": ErrorCode.VALIDATION.value,
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )
    logger.error(
        "Unhandled exception [request_id=%s] %s %s",
        request_id, request.method, request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "error_code": ErrorCode.INTERNAL.value,
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire AppError handling into the FastAPI app.

    Kept as a function so tests and alternative app factories can share the
    same registration without duplicating decorator wiring in main.py.

    ``DBAPIError`` is handled in the normal exception middleware (so input
    errors become a 422); a bare ``Exception`` handler is installed on
    Starlette's ServerErrorMiddleware, which only consults it when the app
    is *not* in debug mode, so main.py keeps ``debug`` off unless
    ``DEBUG_TRACEBACKS`` is explicitly enabled.
    """
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(DBAPIError, unhandled_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
