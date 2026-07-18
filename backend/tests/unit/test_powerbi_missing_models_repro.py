"""Power BI semantic models missing from get_schemas() — reproduction + fix.

Reported symptom: "not all the semantic models are selectable in schema —
some are missing from the fetch". The selectable catalog is whatever
PowerBIClient.get_schemas() emits, and a dataset whose table discovery comes
back empty produced ZERO schema rows AND no signal — the whole semantic model
vanished (docs/feedback-loops/powerbi-missing-semantic-models.md).

Two mechanisms are pinned here, both at the HTTP boundary (the fake below
stands in for api.powerbi.com only — all client logic runs real):

1. Admin-scan shadowing: _batch_admin_scan records an entry for EVERY dataset
   in the scan result, including ones the Scanner API returned no schema for
   (not refreshed since enhanced-metadata scanning was enabled, DirectLake,
   all-hidden models). get_schemas() used to treat "covered by admin scan" as
   final and never try the COLUMNSTATISTICS fallback for those datasets.
   FIX: fall through to COLUMNSTATISTICS on an EMPTY scan result, not just a
   missing one — the model is discovered.

2. Unreadable datasets: without admin rights, discovery rests on
   COLUMNSTATISTICS via executeQueries, which needs Build permission and
   fails for RLS/DirectLake/Viewer-only models. This one legitimately yields
   no columns, so it must NOT become a phantom (column-less, unqueryable)
   table — but it must ALSO not vanish silently. FIX: record it in
   `discovery_diagnostics` / `index_stats()` with a reason, so the indexing
   job can report "found but not readable" instead of dropping it without a
   trace.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from app.data_sources.clients.powerbi_client import PowerBIClient


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


def test_admin_scan_entry_without_schema_must_not_shadow_fallback():
    """Fix 1: a dataset the admin scan covers but returns NO schema for must
    still be discovered via the COLUMNSTATISTICS fallback (which works for it)."""
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
    # The DAX fallback CAN read d2 — the fix must now try it.
    http.route("POST", "/groups/ws1/datasets/d2/executeQueries", _colstats_resp(["Facts"]))

    client = _mk_client(http)
    names = sorted(t.name for t in client.get_schemas())
    print(f"\n[fixed] emitted schema tables: {names}")
    print(f"[fixed] COLUMNSTATISTICS attempted for d2: "
          f"{http.attempted('POST', 'datasets/d2/executeQueries')}")

    assert "ShadowedModel/Facts" in names, (
        "empty admin-scan result must fall through to COLUMNSTATISTICS, not shadow it"
    )
    assert "CoveredModel/Sales" in names  # the non-empty scan result still works
    assert http.attempted("POST", "datasets/d2/executeQueries")


def test_unintrospectable_dataset_reported_not_dropped_and_not_phantom():
    """Fix 2: a dataset whose introspection is forbidden (no Build permission /
    RLS / DirectLake) and that is not a Push dataset must NOT become a
    phantom column-less table, but must ALSO NOT vanish silently — it is
    recorded in discovery_diagnostics / index_stats() with a reason so the
    indexing job can report it."""
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
    diagnostics = client.index_stats().get("unreadable_datasets", [])
    diag_names = sorted(d["datasetName"] for d in diagnostics)
    print(f"\n[fixed] emitted schema tables: {names}")
    print(f"[fixed] unreadable diagnostics: {[(d['datasetName'], d['reason']) for d in diagnostics]}")

    # The readable model is present.
    assert names == ["ReadableModel/Orders"]
    # The unreadable one is NOT a phantom table...
    assert not any(n.startswith("UnreadableModel/") for n in names)
    # ...but IS reported with a reason (not silently dropped).
    assert diag_names == ["UnreadableModel"]
    assert diagnostics[0]["datasetId"] == "d1"
    assert diagnostics[0]["reason"]  # a non-empty human-readable reason
    assert client.index_stats()["unreadable_dataset_count"] == 1


def test_all_readable_leaves_no_diagnostics():
    """The diagnostics channel stays empty when every dataset introspects —
    index_stats() must not fabricate noise on a clean crawl."""
    http = _FakePowerBIHttp()
    _wire_tenant(http, [{"id": "d1", "name": "GoodModel"}])
    http.route("POST", "/admin/workspaces/getInfo", _resp(401, {}))
    http.route("POST", "/groups/ws1/datasets/d1/executeQueries", _colstats_resp(["T"]))

    client = _mk_client(http)
    names = sorted(t.name for t in client.get_schemas())
    assert names == ["GoodModel/T"]
    assert client.index_stats() == {}


def test_builtin_usage_metrics_models_are_skipped():
    """Fix 4: Power BI's built-in usage-metrics system models are not real data
    and must be skipped in discovery (not emitted, not counted as unreadable)."""
    http = _FakePowerBIHttp()
    _wire_tenant(http, [
        {"id": "d1", "name": "Usage Metrics Report"},
        {"id": "d2", "name": "Report Usage Metrics Model"},
        {"id": "d3", "name": "RealModel"},
    ])
    http.route("POST", "/admin/workspaces/getInfo", _resp(401, {}))
    http.route("POST", "/groups/ws1/datasets/d3/executeQueries", _colstats_resp(["Sales"]))

    client = _mk_client(http)
    names = sorted(t.name for t in client.get_schemas())
    assert names == ["RealModel/Sales"]
    # System models are skipped entirely — not surfaced as unreadable either.
    assert client.index_stats() == {}
    assert not http.attempted("POST", "datasets/d1/executeQueries")
