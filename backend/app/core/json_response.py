"""One-pass JSON responses for payloads that carry a step's rows.

FastAPI's default path for a route without a response model walks the
returned value with ``jsonable_encoder`` and then writes it with the stdlib
``json`` module; on a dashboard with hundreds of thousands of rows those
Python walks are most of the response time. Here a schema is converted once
with pydantic's JSON mode, fields by alias -- what ``jsonable_encoder`` does
for a model, so the parsed JSON is the same (aliases such as a gradient's
``from``/``to``, dates as ISO text, Decimal as text, NaN/Infinity as null) --
and orjson writes the bytes.

Two cases take FastAPI's default path instead, so the output stays the same:

- a body with a key starting with ``_sa`` (``jsonable_encoder`` drops such
  keys at any depth; the check looks for ``"_sa`` in the bytes, so a text
  value starting with it also takes this path, with the same result);
- a payload orjson refuses (an integer past 64 bits, such as a 128-bit sum).
"""
import json
import logging
from typing import Any

import orjson
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def json_data(value: Any) -> Any:
    """``value`` (a pydantic model, or plain JSON data: dict, list, str,
    number, bool, None) as plain JSON data: a model through pydantic's JSON
    mode by alias (its field serializers, such as code visibility, apply now,
    in the caller's context); plain data as is."""
    return value.model_dump(mode="json", by_alias=True) if isinstance(value, BaseModel) else value


def _stdlib_dumps(data: Any) -> bytes:
    """The stdlib writer with Starlette's JSONResponse settings."""
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def json_response(content: Any) -> Response:
    """``content`` (a pydantic model, or plain JSON data built with
    ``json_data``) as an application/json response."""
    data = json_data(content)
    try:
        body = orjson.dumps(data)
    except orjson.JSONEncodeError as exc:
        body = _stdlib_dumps(jsonable_encoder(data))
        logger.info("json_response: orjson refused the payload (%s); stdlib writer used, %d bytes",
                    exc, len(body))
        return Response(body, media_type="application/json")
    if b'"_sa' in body:
        # FastAPI's default path, which drops the keys starting with "_sa".
        body = _stdlib_dumps(jsonable_encoder(data))
        logger.info('json_response: "_sa" in the payload; FastAPI\'s default path used, %d bytes', len(body))
    return Response(body, media_type="application/json")
