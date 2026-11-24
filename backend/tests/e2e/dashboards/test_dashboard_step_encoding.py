"""How the shared dashboard's step routes encode the stored rows.

GET /d/{ref}/queries_with_steps, /d/{ref}/draft/queries_with_steps and the
two per-query step routes hand a dashboard every row it draws. They encode
each step once (pydantic's JSON mode, then orjson) instead of FastAPI's
jsonable_encoder walk plus the stdlib writer; the JSON a page parses must not
change, nor the headers the app adds to every response.
"""
import math

import fastapi.routing
import pytest

from app.routes import dashboard as dashboard_routes
from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

MARKER = "rows-encoding-marker"

# Values a stored step can hold after its JSON column round trip: text
# outside ASCII (Hebrew, an emoji, a line separator), nulls, nested rows, an
# integer past 64 bits (a 128-bit sum), dates and ids already as text.
STORED = {
    "columns": [{"field": "name"}, {"field": "total"}, {"field": "when"}],
    "rows": [
        {"name": MARKER, "total": 2 ** 70, "when": "2024-02-29T03:04:05.123456+02:00",
         "id": "12345678-1234-5678-1234-567812345678", "share": 0.125, "neg": -7, "ok": True},
        {"name": "הכנסות " + chr(0x1F600) + chr(0x2028) + "\"q\"\\", "total": None, "when": "2024-02-29",
         "nested": {"list": [1, [2, {"deep": None}]], "empty": {}}, "share": 1e-7, "ok": False},
    ],
}


def _expected_rows():
    return [dict(r) for r in STORED["rows"]]


def _setup(corpus, corpus_rows, test_client, stored):
    """A published dashboard whose draft step and published copy hold `stored`."""
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    headers = as_(corpus, "owner")
    for base in (f"/api/d/{aid}", f"/api/d/{aid}/draft"):
        for q in test_client.get(f"{base}/queries", headers=headers).json():
            step = test_client.get(f"{base}/queries/{q['id']}/step", headers=headers)
            assert step.status_code == 200, step.text
            corpus_rows.update("steps", step.json()["id"], data=stored)
    return aid


def _routes(test_client, corpus, aid):
    """Every (path, step-or-steps) the four routes serve the owner."""
    headers = as_(corpus, "owner")
    out = []
    for base in (f"/api/d/{aid}", f"/api/d/{aid}/draft"):
        bundle = test_client.get(f"{base}/queries_with_steps", headers=headers)
        out.append((bundle, [q["default_step"] for q in bundle.json()]))
        for q in bundle.json():
            one = test_client.get(f"{base}/queries/{q['id']}/step", headers=headers)
            out.append((one, [one.json()]))
    return out


@pytest.mark.e2e
def test_the_four_step_routes_serve_the_stored_rows_unchanged(corpus, corpus_rows, test_client):
    aid = _setup(corpus, corpus_rows, test_client, STORED)
    served = _routes(test_client, corpus, aid)
    assert len(served) == 4
    for resp, steps in served:
        assert resp.status_code == 200, (resp.request.url, resp.text)
        assert steps and all(s["data"]["rows"] == _expected_rows() for s in steps), resp.request.url
        assert all(s["data"]["columns"] == STORED["columns"] for s in steps)
        # The big integer stays an exact integer, not a float or a string.
        assert all(s["data"]["rows"][0]["total"] == 1180591620717411303424 for s in steps)


@pytest.mark.e2e
def test_a_non_finite_stored_number_is_served_as_null(corpus, corpus_rows, test_client):
    """NaN/Infinity is not JSON; the page has always received null for it
    (pydantic's JSON mode) and still does. Only SQLite's JSON column can
    store one; PostgreSQL refuses it on write."""
    if corpus_rows.engine.dialect.name != "sqlite":
        pytest.skip("PostgreSQL JSON cannot hold NaN")
    stored = {"rows": [{"name": MARKER, "nan": math.nan, "inf": math.inf, "ninf": -math.inf, "n": 1.5}]}
    aid = _setup(corpus, corpus_rows, test_client, stored)
    for resp, steps in _routes(test_client, corpus, aid):
        assert resp.status_code == 200, (resp.request.url, resp.text)
        assert "NaN" not in resp.text and "Infinity" not in resp.text
        assert all(s["data"]["rows"] == [{"name": MARKER, "nan": None, "inf": None, "ninf": None, "n": 1.5}]
                   for s in steps)


@pytest.mark.e2e
def test_step_routes_keep_the_content_type_and_the_app_headers(corpus, corpus_rows, test_client):
    aid = _setup(corpus, corpus_rows, test_client, STORED)
    # GET /d/{ref}/queries still answers through FastAPI's default response.
    reference = test_client.get(f"/api/d/{aid}/queries", headers=as_(corpus, "owner"))
    assert reference.status_code == 200
    varying = {"content-length", "date", "x-request-id", "server-timing"}
    expected = {k: v for k, v in reference.headers.items() if k not in varying}
    assert expected.get("content-type") == "application/json"
    assert "x-content-type-options" in expected
    for resp, _ in _routes(test_client, corpus, aid):
        got = {k: v for k, v in resp.headers.items() if k not in varying}
        assert got == expected, resp.request.url
        assert int(resp.headers["content-length"]) == len(resp.content)


@pytest.mark.e2e
def test_step_rows_are_not_walked_by_jsonable_encoder(corpus, corpus_rows, test_client, monkeypatch):
    """The rows are encoded in one pass: neither the routes nor FastAPI's
    response path hand them to jsonable_encoder (a Python walk of every
    cell, the cost on a large dashboard)."""
    aid = _setup(corpus, corpus_rows, test_client, STORED)
    walked = []

    def spy(real):
        def wrapper(obj, *args, **kwargs):
            if MARKER in repr(obj):
                walked.append(type(obj).__name__)
            return real(obj, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(fastapi.routing, "jsonable_encoder", spy(fastapi.routing.jsonable_encoder))
    if hasattr(dashboard_routes, "jsonable_encoder"):
        monkeypatch.setattr(dashboard_routes, "jsonable_encoder", spy(dashboard_routes.jsonable_encoder))
    served = _routes(test_client, corpus, aid)
    assert all(resp.status_code == 200 for resp, _ in served)
    assert all(s["data"]["rows"][0]["name"] == MARKER for _, steps in served for s in steps)
    assert walked == []
