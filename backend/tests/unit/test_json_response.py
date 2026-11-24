"""app.core.json_response: one-pass JSON for the dashboard step routes.

A step's rows reach the routes as stored JSON, but a step schema may hold
any value pydantic can serialize; the body must parse to what FastAPI's
default path (jsonable_encoder, then Starlette's JSONResponse) produced.
"""
import datetime as dt
import decimal
import json
import math
import uuid

import pytest
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

from app.core.code_visibility import code_visibility
from app.core.json_response import json_data, json_response
from app.schemas.step_schema import PublicStepSchema

ROW = {
    "day": dt.date(2024, 2, 29),
    "naive": dt.datetime(2024, 1, 2, 3, 4, 5, 123456),
    "utc": dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
    "plus2": dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=2))),
    "clock": dt.time(1, 2, 3),
    "price": decimal.Decimal("1.50"),
    "count": decimal.Decimal("10"),
    "nan": math.nan,
    "inf": math.inf,
    "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "text": "הכנסות " + chr(0x1F600),
    "none": None,
    "big": 2 ** 70,
    3: "int key",
}

# What the page has always parsed for ROW (pydantic's JSON mode).
EXPECTED_ROW = {
    "day": "2024-02-29",
    "naive": "2024-01-02T03:04:05.123456",
    "utc": "2024-01-02T03:04:05Z",
    "plus2": "2024-01-02T03:04:05+02:00",
    "clock": "01:02:03",
    "price": "1.50",
    "count": "10",
    "nan": None,
    "inf": None,
    "id": "12345678-1234-5678-1234-567812345678",
    "text": "הכנסות " + chr(0x1F600),
    "none": None,
    "big": 1180591620717411303424,
    "3": "int key",
}


def _step(**data):
    return PublicStepSchema(id="s1", title="t", type="table", code="select 1", data=data)


def _old_body(content):
    """FastAPI's default path for a route without a response model."""
    return json.loads(JSONResponse(jsonable_encoder(content)).body)


@pytest.mark.parametrize("big", [False, True])
def test_a_step_parses_to_what_fastapis_default_path_gave(big):
    row = dict(ROW) if big else {k: v for k, v in ROW.items() if k != "big"}
    step = _step(rows=[row], nested={"inner": [row, [None, {"x": row}]]})
    with code_visibility(True):
        new = json.loads(json_response(step).body)
        old = _old_body(step)
    assert new == old
    expected = EXPECTED_ROW if big else {k: v for k, v in EXPECTED_ROW.items() if k != "big"}
    assert new["data"]["rows"] == [expected]
    assert new["data"]["nested"]["inner"][1][1]["x"] == expected


def test_a_bundle_of_dumped_steps_parses_like_the_old_bundle():
    step = _step(rows=[ROW])
    with code_visibility(True):
        bundle = [{"id": "q1", "default_step": json_data(step)}, {"id": "q2", "default_step": json_data(None)}]
        old = _old_body([{"id": "q1", "default_step": jsonable_encoder(step)}, {"id": "q2", "default_step": None}])
    assert json.loads(json_response(bundle).body) == old
    assert old[0]["default_step"]["data"]["rows"] == [EXPECTED_ROW]


def test_code_visibility_still_applies_when_the_step_is_dumped():
    with code_visibility(False):
        assert json.loads(json_response(_step(rows=[])).body)["code"] is None
    with code_visibility(True):
        assert json.loads(json_response(_step(rows=[])).body)["code"] == "select 1"


def test_plain_data_passes_through_and_the_body_is_utf8_json():
    pending = {"id": "p", "title": "ד", "pending": True, "data": {}}
    resp = json_response(pending)
    assert resp.media_type == "application/json"
    assert resp.headers["content-type"] == "application/json"
    assert json.loads(resp.body) == pending
    assert "ד".encode() in resp.body
