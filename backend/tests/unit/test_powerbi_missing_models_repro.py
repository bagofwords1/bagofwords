"""Reproduction: Power BI semantic models silently missing from get_schemas().

Reported symptom: "not all the semantic models are selectable in schema —
some are missing from the fetch". The selectable catalog is whatever
PowerBIClient.get_schemas() emits, and a dataset whose table discovery comes
back empty produces ZERO schema rows — the whole semantic model vanishes with
no error surfaced (docs/feedback-loops/powerbi-missing-semantic-models.md).

Two drop mechanisms are pinned here, both at the HTTP boundary (the fake
below stands in for api.powerbi.com only — all client logic runs real):

1. Admin-scan shadowing: _batch_admin_scan records an entry for EVERY dataset
   in the scan result, including ones the Scanner API returned no schema for
   (not refreshed since enhanced-metadata scanning was enabled, DirectLake,
   all-hidden models). get_schemas() then treats "covered by admin scan" as
   final and never tries the COLUMNSTATISTICS fallback for those datasets
   (powerbi_client.py — `if ds_id in admin_scan_results`), even when the
   fallback would have worked.

2. Fallback failure is silent: without admin rights, discovery rests on
   COLUMNSTATISTICS via executeQueries, which needs Build permission and
   fails for RLS/DirectLake/Viewer-only models. The failure is swallowed
   (`([], [])`) and the REST /tables fallback only answers for Push datasets,
   so the dataset ends up with no rows — invisible in the schema, no signal.

The invariant asserted (and currently violated, hence strict xfail): every
dataset the identity can LIST must be represented in the emitted schema —
that is what makes a semantic model selectable at all.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from app.data_sources.clients.powerbi_client import PowerBIClient

XFAIL_REASON = (
    "known bug: datasets with no discoverable tables are silently dropped from "
    "get_schemas(); see docs/feedback-loops/powerbi-missing-semantic-models.md"
)


def _resp(status: int, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.headers = {}
    r.text = json.dumps(payload) if payload else ""
    return r


class _FakePowerBIHttp:
    """Stands in for api.powerbi.com. Routes by (method, URL substring) and
    records every call so tests can observe which endpoints were attempted."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._routes: list[tuple[str, str, object]] = []

    def route(self, method: str, url_substr: str, response) -> None:
        self._routes.append((method.upper(), url_substr, response))

    def _dispatch(self, method: str, url: str):
        self.calls.append((method.upper(), url))
        # Most specific (longest) matching substring wins, so e.g. a route for
        # "/groups/ws1/datasets/d1/tables" beats the "/groups/ws1/datasets"
        # dataset-listing route.
        matches = [
            (len(sub), response)
            for m, sub, response in self._routes
            if m == method.upper() and sub in url
        ]
        if matches:
            return max(matches, key=lambda x: x[0])[1]
        return _resp(404, {"error": {"code": "ItemNotFound"}})

    # requests.Session surface used by the client
    def request(self, method, url, json=None, headers=None, timeout=None):
        return self._dispatch(method, url)

    def post(self, url, json=None, headers=None, timeout=None):
        return self._dispatch("POST", url)

    def get(self, url, headers=None, timeout=None):
        return self._dispatch("GET", url)

    def attempted(self, method: str, url_substr: str) -> bool:
        return any(m == method.upper() and url_substr in u for m, u in self.calls)


def _mk_client(http: _FakePowerBIHttp) -> PowerBIClient:
    c = PowerBIClient(tenant_id="t", client_id="c", client_secret="s", access_token="tok")
    c._http = http
    return c


def _colstats_resp(table_names):
    """executeQueries response for EVALUATE COLUMNSTATISTICS()."""
    rows = [
        {"[Table Name]": t, "[Column Name]": "id", "[Min]": None,
         "[Max]": None, "[Cardinality]": 1, "[Max Length]": None}
        for t in table_names
    ]
    return _resp(200, {"results": [{"tables": [{"rows": rows}]}]})


def _wire_tenant(http: _FakePowerBIHttp, datasets):
    """One workspace with the given datasets; no reports."""
    http.route("GET", "/groups/ws1/datasets", _resp(200, {"value": datasets}))
    http.route("GET", "/groups/ws1/reports", _resp(200, {"value": []}))
    http.route("GET", "/groups", _resp(200, {"value": [{"id": "ws1", "name": "Sales"}]}))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Admin-scan polling sleeps 2s per iteration; irrelevant to the logic."""
    monkeypatch.setattr(time, "sleep", lambda *_: None)


@pytest.mark.xfail(strict=True, reason=XFAIL_REASON)
def test_admin_scan_entry_without_schema_must_not_shadow_fallback():
    """A dataset the admin scan covers but returns NO schema for must still be
    discovered via the COLUMNSTATISTICS fallback (which works for it)."""
    http = _FakePowerBIHttp()
    _wire_tenant(http, [
        {"id": "d1", "name": "CoveredModel"},
        {"id": "d2", "name": "ShadowedModel"},
    ])
    # Admin scan succeeds; d1 comes back with schema, d2 with none (e.g. not
    # refreshed since enhanced-metadata scanning was enabled).
    http.route("POST", "/admin/workspaces/getInfo", _resp(200, {"id": "scan-1"}))
    http.route("GET", "/admin/workspaces/scanStatus/scan-1", _resp(200, {"status": "Succeeded"}))
    http.route("GET", "/admin/workspaces/scanResult/scan-1", _resp(200, {
        "workspaces": [{
            "id": "ws1",
            "datasets": [
                {"id": "d1", "tables": [
                    {"name": "Sales", "columns": [{"name": "id", "dataType": "Int64"}]},
                ]},
                {"id": "d2"},  # covered by the scan, but no schema returned
            ],
        }]
    }))
    # The DAX fallback CAN read d2 — but current code never tries it.
    http.route("POST", "/groups/ws1/datasets/d2/executeQueries", _colstats_resp(["Facts"]))

    client = _mk_client(http)
    names = sorted(t.name for t in client.get_schemas())
    print(f"\n[repro] emitted schema tables: {names}")
    print(f"[repro] COLUMNSTATISTICS attempted for d2: "
          f"{http.attempted('POST', 'datasets/d2/executeQueries')}")

    assert "ShadowedModel/Facts" in names, (
        "dataset covered by the admin scan without schema was dropped instead "
        "of falling back to COLUMNSTATISTICS"
    )


@pytest.mark.xfail(strict=True, reason=XFAIL_REASON)
def test_unintrospectable_dataset_is_still_represented_in_schema():
    """Without admin-scan rights, a dataset whose COLUMNSTATISTICS probe is
    forbidden (no Build permission / RLS / DirectLake) and that is not a Push
    dataset must not silently vanish from the schema — it is a semantic model
    the identity can list, and it should remain visible/selectable (or at
    minimum the failure surfaced), not disappear without a trace."""
    http = _FakePowerBIHttp()
    _wire_tenant(http, [
        {"id": "d1", "name": "UnreadableModel"},
        {"id": "d2", "name": "ReadableModel"},
    ])
    # No admin rights → batch scan rejected → everything uses the fallback.
    http.route("POST", "/admin/workspaces/getInfo", _resp(401, {"error": {"code": "Unauthorized"}}))
    # d1: executeQueries forbidden (no Build permission), and the REST /tables
    # fallback 404s (only Push datasets answer it).
    http.route("POST", "/groups/ws1/datasets/d1/executeQueries",
               _resp(403, {"error": {"code": "PowerBINotAuthorizedException"}}))
    http.route("GET", "/groups/ws1/datasets/d1/tables",
               _resp(404, {"error": {"code": "ItemNotFound"}}))
    # d2: introspection works fine.
    http.route("POST", "/groups/ws1/datasets/d2/executeQueries", _colstats_resp(["Orders"]))

    client = _mk_client(http)
    tables = client.get_schemas()
    names = sorted(t.name for t in tables)
    print(f"\n[repro] emitted schema tables: {names}")

    # The readable model is fine — proves discovery itself ran.
    assert any(n.startswith("ReadableModel/") for n in names)
    # The invariant (currently violated): the listed-but-unintrospectable
    # semantic model must still be represented in the emitted schema.
    assert any(n.startswith("UnreadableModel/") for n in names), (
        "dataset with failed table introspection was silently dropped from the schema"
    )


def test_current_behavior_drops_are_silent_no_exception():
    """Pins the 'silent' part of the bug (passes today): both drop mechanisms
    complete without raising — the caller gets a smaller schema and no signal.
    If a fix makes get_schemas raise or annotate instead, revisit alongside
    the xfail tests above."""
    http = _FakePowerBIHttp()
    _wire_tenant(http, [{"id": "d1", "name": "UnreadableModel"}])
    http.route("POST", "/admin/workspaces/getInfo", _resp(401, {}))
    http.route("POST", "/groups/ws1/datasets/d1/executeQueries", _resp(403, {}))
    http.route("GET", "/groups/ws1/datasets/d1/tables", _resp(404, {}))

    client = _mk_client(http)
    tables = client.get_schemas()  # must not raise
    assert tables == []  # the only dataset in the tenant is simply gone
