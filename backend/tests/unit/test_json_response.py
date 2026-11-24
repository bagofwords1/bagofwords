"""app.core.json_response: one-pass JSON for the dashboard step routes.

A step's rows reach the routes as stored JSON, but a step schema may hold
any value pydantic can serialize; the body must parse to what FastAPI's
default path (jsonable_encoder, then Starlette's JSONResponse) produced.
"""
import datetime as dt
import decimal
import json
import logging
import math
import uuid

import pytest
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

from app.core.code_visibility import code_visibility
from app.core.json_response import json_data, json_response
from app.schemas.query_schema import PublicQuerySchema
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


VIEW = {
    "version": "v2",
    "view": {
        "type": "bar_chart", "x": "month", "y": "total",
        "seriesStyles": [{"key": "total", "gradient": {"from": "#93c5fd", "to": "#1d4ed8", "opacity": [1, 0.2]}}],
    },
}


def _query(**extra):
    return PublicQuerySchema.model_validate({
        "id": "q1", "title": "Sales", "default_step_id": "s1",
        "visualizations": [{"id": "v1", "title": "By month", "view": VIEW}],
        "parameters": [{"name": "region", "type": "string"}],
        **extra,
    })


# Every schema the step routes send: the listed query (nested view with a
# gradient, whose fields have aliases), a step (rows, view, viewer result),
# a missing step (None) and the bundle built from them.
def _every_payload():
    step = _step(rows=[{k: v for k, v in ROW.items() if k != "big"}], columns=[{"field": "day"}])
    step.view = {"type": "table", "nested": {"_sales": 1}}
    step.viewer_result = {"rows": [{"_sales": 2, "n": 1}]}
    bundle = [
        {**json_data(_query()), "default_step": json_data(step)},
        {**json_data(_query(id="q2")), "default_step": json_data(None)},
    ]
    return {"query": _query(), "step": step, "none": None, "bundle": bundle}


@pytest.mark.parametrize("name", ["query", "step", "none", "bundle"])
def test_every_schema_the_step_routes_send_parses_like_fastapis_default_path(name):
    with code_visibility(True):
        payload = _every_payload()[name]
        old = _old_body(payload)
        assert json.loads(json_response(payload).body) == old
        # json_data is jsonable_encoder's model step: fields by alias.
        assert _old_body(json_data(payload)) == old


def test_a_gradient_keeps_its_from_and_to_names():
    """The page reads seriesStyles[].gradient.from/.to (the aliases)."""
    for body in (json_response(_query()).body, json_response([json_data(_query())]).body):
        parsed = json.loads(body)
        query = parsed[0] if isinstance(parsed, list) else parsed
        gradient = query["visualizations"][0]["view"]["view"]["seriesStyles"][0]["gradient"]
        assert gradient == {"from": "#93c5fd", "to": "#1d4ed8", "opacity": [1, 0.2]}


def test_keys_starting_with_sa_are_dropped_as_the_default_path_drops_them():
    """jsonable_encoder (sqlalchemy_safe) drops any key starting with "_sa",
    at any depth; the step routes keep that output, so a "_sales" column is
    not served (the same as before the one-pass encoding)."""
    step = _step(rows=[{"_sales": 5, "region": "north", "sub": {"_sa_x": 1, "y": 2}}])
    with code_visibility(True):
        got = json.loads(json_response(step).body)
        assert got == _old_body(step)
    assert got["data"]["rows"] == [{"region": "north", "sub": {"y": 2}}]
    bundle = [{"id": "q", "default_step": {"data": {"rows": [{"_sales": 5, "n": 1}]}}}]
    assert json.loads(json_response(bundle).body) == [{"id": "q", "default_step": {"data": {"rows": [{"n": 1}]}}}]


def test_a_value_starting_with_sa_is_still_served():
    """Only keys are dropped; a text value that starts with "_sa" stays."""
    payload = {"rows": [{"name": "_sample"}], "note": '"_sa'}
    assert json.loads(json_response(payload).body) == payload == _old_body(payload)


@pytest.fixture
def module_logs(caplog):
    """The module logger's records. alembic's fileConfig (run by migration-
    backed tests earlier in the worker) disables loggers that exist then."""
    logger = logging.getLogger("app.core.json_response")
    disabled = logger.disabled
    logger.disabled = False
    with caplog.at_level(logging.INFO, logger="app.core.json_response"):
        yield caplog
    logger.disabled = disabled


def test_an_integer_past_64_bits_falls_back_with_a_log_line_and_a_correct_body(module_logs):
    payload = {"rows": [{"total": 2 ** 70, "name": "north"}]}
    body = json_response(payload).body
    assert json.loads(body) == payload == _old_body(payload)
    records = [r for r in module_logs.records if r.name == "app.core.json_response"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert str(len(body)) in message
    assert "north" not in message and "1180591620717411303424" not in message


def test_the_fallback_log_line_carries_orjsons_reason(module_logs):
    """orjson's error is always a TypeError; its message says why."""
    json_response({"rows": [{"total": 2 ** 70, "name": "north"}]})
    records = [r for r in module_logs.records if r.name == "app.core.json_response"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "64-bit" in message
    assert "north" not in message and "1180591620717411303424" not in message


def test_a_payload_with_an_sa_key_logs_one_line_with_the_size(module_logs):
    """The "_sa" path walks the payload again; ops see why the response is slow."""
    body = json_response({"rows": [{"_sales": 5, "region": "north"}]}).body
    records = [r for r in module_logs.records if r.name == "app.core.json_response"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert str(len(body)) in message
    assert "north" not in message and "5" not in message.replace(str(len(body)), "")


def test_an_integer_past_64_bits_next_to_an_sa_key_parses_like_the_default_path():
    payload = {"rows": [{"total": 2 ** 70, "_sales": 3}]}
    assert json.loads(json_response(payload).body) == _old_body(payload) == {"rows": [{"total": 2 ** 70}]}


def test_the_fast_path_logs_nothing(module_logs):
    json_response({"rows": [{"total": 2 ** 40, "name": "north"}]})
    assert [r for r in module_logs.records if r.name == "app.core.json_response"] == []


def test_plain_data_passes_through_and_the_body_is_utf8_json():
    pending = {"id": "p", "title": "ד", "pending": True, "data": {}}
    resp = json_response(pending)
    assert resp.media_type == "application/json"
    assert resp.headers["content-type"] == "application/json"
    assert json.loads(resp.body) == pending
    assert "ד".encode() in resp.body
