"""AppError: a domain exception that carries a stable machine-readable code.

The frontend resolves `error_code` against its own i18n catalog, so the
English `message` we ship on the wire is only a fallback for logs and
non-localized clients (curl, CI, scripts). Callers pass `params` when the
localized string has interpolations (e.g. `{name}`).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from .codes import ErrorCode


class AppError(Exception):
    """Raised by services/routes to signal a typed failure.

    The global exception handler converts this into a JSON response with the
    shape `{detail, error_code, params, status_code}`. `detail` is retained
    for backwards compatibility with older frontend code that reads
    `error.data.detail` directly.
    """

    def __init__(
        self,
        error_code: Union[ErrorCode, str],
        message: Optional[str] = None,
        *,
        status_code: int = 400,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.error_code = (
            error_code.value if isinstance(error_code, ErrorCode) else str(error_code)
        )
        self.message = message or self.error_code
        self.status_code = status_code
        self.params = params or {}
        self.headers = headers
        super().__init__(self.message)

    # Convenience constructors for common cases keep call sites short.
    @classmethod
    def not_found(
        cls,
        error_code: Union[ErrorCode, str],
        message: Optional[str] = None,
        **params: Any,
    ) -> "AppError":
        return cls(error_code, message, status_code=404, params=params or None)

    @classmethod
    def forbidden(
        cls,
        error_code: Union[ErrorCode, str] = ErrorCode.ACCESS_DENIED,
        message: Optional[str] = None,
        **params: Any,
    ) -> "AppError":
        return cls(error_code, message, status_code=403, params=params or None)

    @classmethod
    def unauthorized(
        cls,
        error_code: Union[ErrorCode, str] = ErrorCode.UNAUTHORIZED,
        message: Optional[str] = None,
        **params: Any,
    ) -> "AppError":
        return cls(error_code, message, status_code=401, params=params or None)

    @classmethod
    def bad_request(
        cls,
        error_code: Union[ErrorCode, str],
        message: Optional[str] = None,
        **params: Any,
    ) -> "AppError":
        return cls(error_code, message, status_code=400, params=params or None)

    @classmethod
    def conflict(
        cls,
        error_code: Union[ErrorCode, str] = ErrorCode.RESOURCE_CONFLICT,
        message: Optional[str] = None,
        **params: Any,
    ) -> "AppError":
        return cls(error_code, message, status_code=409, params=params or None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detail": self.message,
            "error_code": self.error_code,
            "params": self.params,
            "status_code": self.status_code,
        }
