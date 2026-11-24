"""One-pass JSON responses for payloads that carry a step's rows.

FastAPI's default path for a route without a response model walks the
returned value with ``jsonable_encoder`` and then writes it with the stdlib
``json`` module; on a dashboard with hundreds of thousands of rows those
Python walks are most of the response time. Here a schema is converted once
with pydantic's JSON mode -- exactly what ``jsonable_encoder`` does for a
model, so the parsed JSON is the same (dates as ISO text, Decimal as text,
NaN/Infinity as null) -- and orjson writes the bytes.
"""
import json
from typing import Any

import orjson
from fastapi.responses import Response
from pydantic import BaseModel


def json_data(value: Any) -> Any:
    """``value`` as plain JSON data: a schema through pydantic's JSON mode
    (its field serializers, such as code visibility, apply now, in the
    caller's context); anything else (plain data, None) as is."""
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def json_response(content: Any) -> Response:
    """``content`` (a schema, or plain data built with ``json_data``) as an
    application/json response."""
    data = json_data(content)
    try:
        body = orjson.dumps(data)
    except orjson.JSONEncodeError:
        # orjson stops at 64-bit integers; a larger one (a 128-bit sum) is
        # still valid JSON, written as Starlette's JSONResponse writes it.
        body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    return Response(body, media_type="application/json")
