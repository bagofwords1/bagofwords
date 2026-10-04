# SCIM 2.0 Error Responses
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# RFC 7644 §3.12: errors on the SCIM surface are JSON bodies carrying the
# Error schema, `status` as a string and an optional `scimType`. Entra and Okta
# read `scimType` (uniqueness, invalidFilter, ...) to decide whether to retry,
# match, or surface the failure, so FastAPI's `{"detail": ...}` default is not
# enough.

from typing import Any, Optional

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_MEDIA_TYPE = "application/scim+json"


class ScimError(HTTPException):
    """HTTPException that also carries an RFC 7644 `scimType`."""

    def __init__(self, status_code: int, detail: str, scim_type: Optional[str] = None):
        super().__init__(status_code=status_code, detail=detail)
        self.scim_type = scim_type


def scim_error_response(
    status_code: int,
    detail: Any,
    scim_type: Optional[str] = None,
    headers: Optional[dict] = None,
) -> JSONResponse:
    body = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "status": str(status_code),
        "detail": detail if isinstance(detail, str) else str(detail),
    }
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=headers,
        media_type=SCIM_MEDIA_TYPE,
    )


class ScimRoute(APIRoute):
    """Route class that renders every failure on the SCIM surface as a SCIM Error.

    Covers errors raised by dependencies (bearer auth, license) as well as the
    handler, and turns request-body validation failures into 400 invalidValue
    rather than FastAPI's 422, which SCIM clients do not understand.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except HTTPException as exc:
                return scim_error_response(
                    exc.status_code,
                    exc.detail,
                    scim_type=getattr(exc, "scim_type", None),
                    headers=getattr(exc, "headers", None),
                )
            except RequestValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in err.get('loc', ()) if p != 'body')}: {err.get('msg')}"
                    for err in exc.errors()
                )
                return scim_error_response(400, problems or "Invalid request", scim_type="invalidValue")

        return handler
