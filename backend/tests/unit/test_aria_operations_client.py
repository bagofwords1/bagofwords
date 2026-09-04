"""Unit tests for AriaOperationsClient.

Covers:
- appliance URL normalization (bare host, trailing /suite-api or /suite-api/api)
- token acquisition against /auth/token/acquire with authSource, the
  `Authorization: OpsToken` header and `Accept: application/json` on every call,
  token caching, and exactly one re-acquire on 401
- pagination over page/pageSize using pageInfo.totalCount
- get_schemas(): the fixed catalog (pks, fks) + discovered wide
  `metrics::<Adapter>/<Kind>` tables only for populated, non-container kinds,
  capped by max_metric_tables; discovery never fails on API errors
- execute_query(): stats flattening (timestamps/data → rows, dt bands),
  resourceId chunking at 1000, relationships BFS edge direction, alerts window
  post-filtering (open alerts kept), wide-table pivot, top-N ranking
- spec validation (bad JSON, unknown table, missing scope / stat_key)
- test_connection() success / auth-failure messages

The `requests` boundary is mocked, so these run without an appliance.
"""
from __future__ import annotations

import json
import time

import pytest

from app.data_sources.clients.aria_operations_client import (
    AriaOperationsClient,
    ID_BATCH,
    METRIC_TABLE_PREFIX,
    _CATALOG,
)


class _FakeResponse:
    def __init__(self, payload, status_code=200, content_type="application/json"):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload
        self.headers = {"Content-Type": content_type}

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _FakeRequests:
    """Routes by (METHOD, path suffix); records every call. A route value may
    be a payload, a _FakeResponse, a callable(params, body) → payload, or a
    list of _FakeResponse consumed in order."""

    class exceptions:  # mirror requests.exceptions used by the client
        class SSLError(Exception):
            pass

        class RequestException(Exception):
            pass

    def __init__(self, routes, token_responses=None):
        self.routes = routes
        self.token_responses = list(token_responses or [])
        self.calls = []   # (method, url, params, headers, body)
        self.token_calls = []

    def Session(self):
        fake = self

        class _S:
            def request(self, method, url, params=None, json=None, headers=None, verify=None, timeout=None):
                return fake.request(method, url, params=params, json=json, headers=headers)

            def post(self, url, json=None, headers=None, verify=None, timeout=None):
                return fake.post(url, json=json, headers=headers)

        return _S()

    def post(self, url, json=None, headers=None):
        # Only the token endpoint uses Session.post directly.
        self.token_calls.append((url, json, headers))
        if self.token_responses:
            resp = self.token_responses.pop(0)
            return resp if isinstance(resp, _FakeResponse) else _FakeResponse(resp)
        return _FakeResponse({"token": f"tok-{len(self.token_calls)}",
                              "validity": int((time.time() + 6 * 3600) * 1000), "roles": ["ReadOnly"]})

    def request(self, method, url, params=None, json=None, headers=None):
        self.calls.append((method, url, dict(params or {}), dict(headers or {}), json))
        path = url.split("?")[0]
        for (m, suffix), val in self.routes.items():
            if m == method and path.endswith(suffix):
                if callable(val):
                    val = val(dict(params or {}), json)
                if isinstance(val, list) and val and all(isinstance(v, _FakeResponse) for v in val):
                    resp = val.pop(0) if len(val) > 1 else val[0]
                else:
                    resp = val
                return resp if isinstance(resp, _FakeResponse) else _FakeResponse(resp)
        return _FakeResponse({"message": "not scripted: " + method + " " + path}, status_code=404)


@pytest.fixture
def patch_requests(monkeypatch):
    def _install(routes, token_responses=None):
        fake = _FakeRequests(routes, token_responses)
        monkeypatch.setattr("app.data_sources.clients.aria_operations_client.requests", fake)
        return fake
    return _install


def _client(**kw):
    defaults = dict(url="https://aria.corp.local", username="svc_bow", password="pw", auth_source="corp.local")
    defaults.update(kw)
    return AriaOperationsClient(**defaults)


def _res(rid, name, kind, ak="VMWARE", health="GREEN"):
    return {"identifier": rid, "resourceKey": {"name": name, "adapterKindKey": ak, "resourceKindKey": kind},
            "resourceHealth": health, "resourceHealthValue": 95.0,
            "resourceStatusStates": [{"resourceStatus": "DATA_RECEIVING", "resourceState": "STARTED"}],
            "badges": [{"type": "HEALTH", "color": health}], "creationTime": 1}


def _page(key, rows, total=None):
    return {key: rows, "pageInfo": {"totalCount": len(rows) if total is None else total, "page": 0, "pageSize": 1000}}


ADAPTERS = {"adapter-kind": [
    {"key": "VMWARE", "name": "vCenter Adapter", "adapterKindType": "GENERAL", "resourceKinds": ["VirtualMachine", "Datastore"]},
    {"key": "HitachiStorage", "name": "Hitachi", "adapterKindType": "GENERAL", "resourceKinds": ["Pool"]},
    {"key": "VMWARE_ARIA_OPERATIONS", "name": "Self", "adapterKindType": "GENERAL", "resourceKinds": ["vC-Ops-Node"]},
]}

VM = _res("vm-1", "prod-db-01", "VirtualMachine")
DS = _res("ds-1", "ds_prod_db_01", "Datastore")
POOL = _res("pool-1", "Pool-07", "Pool", ak="HitachiStorage")


def _dictionary_routes():
    def resourcekinds(params, body):
        return _page("resource-kind", [
            {"key": "VirtualMachine", "name": "Virtual Machine", "resourceKindType": "GENERAL"},
            {"key": "Datastore", "name": "Datastore", "resourceKindType": "GENERAL"},
            {"key": "VMwareAdapter Instance", "name": "Adapter", "resourceKindType": "ADAPTER_INSTANCE"},
        ])

    def hitachi_kinds(params, body):
        return _page("resource-kind", [{"key": "Pool", "name": "Storage Pool", "resourceKindType": "GENERAL"},
                                       {"key": "Port", "name": "Port", "resourceKindType": "GENERAL"}])

    def resources_query(params, body):
        body = body or {}
        kinds = set(body.get("resourceKind") or [])
        rows = [r for r in (VM, DS, POOL) if not kinds or r["resourceKey"]["resourceKindKey"] in kinds]
        ids = set(body.get("resourceId") or [])
        if ids:
            rows = [r for r in rows if r["identifier"] in ids]
        names = set(body.get("name") or [])
        if names:
            rows = [r for r in rows if r["resourceKey"]["name"] in names]
        return _page("resourceList", rows)

    def statkeys(params, body):
        return {"resourceTypeAttributes": [
            {"key": "cpu|usage_average", "name": "CPU|Usage", "unit": "%", "rollupType": "AVG", "dataType2": "FLOAT"},
            {"key": "virtualDisk|totalLatency", "name": "Virtual Disk|Total Latency", "unit": "ms", "rollupType": "AVG", "dataType2": "FLOAT"},
        ]}

    return {
        ("GET", "/adapterkinds"): ADAPTERS,
        ("GET", "/adapterkinds/VMWARE/resourcekinds"): resourcekinds,
        ("GET", "/adapterkinds/HitachiStorage/resourcekinds"): hitachi_kinds,
        ("GET", "/adapterkinds/VMWARE_ARIA_OPERATIONS/resourcekinds"): _page("resource-kind", [{"key": "vC-Ops-Node", "resourceKindType": "GENERAL"}]),
        ("POST", "/resources/query"): resources_query,
        ("GET", "/statkeys"): statkeys,
        ("GET", "/versions/current"): {"releaseName": "8.18.0"},
    }


# ── URL / auth ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["aria.corp.local", "https://aria.corp.local/", "https://aria.corp.local/suite-api",
                                 "https://aria.corp.local/suite-api/api/"])
def test_url_normalization(url):
    assert _client(url=url).base_url == "https://aria.corp.local"


def test_token_acquired_with_auth_source_and_used_as_opstoken(patch_requests):
    fake = patch_requests({("GET", "/adapterkinds"): ADAPTERS})
    c = _client()
    c.execute_query('{"table": "adapter_kinds"}')
    assert len(fake.token_calls) == 1
    url, body, headers = fake.token_calls[0]
    assert url.endswith("/suite-api/api/auth/token/acquire")
    assert body == {"username": "svc_bow", "password": "pw", "authSource": "corp.local"}
    method, call_url, params, call_headers, _ = fake.calls[0]
    assert call_headers["Authorization"] == "OpsToken tok-1"
    assert call_headers["Accept"] == "application/json"
    assert call_url.startswith("https://aria.corp.local/suite-api/api/")


def test_token_cached_across_calls(patch_requests):
    fake = patch_requests({("GET", "/adapterkinds"): ADAPTERS})
    c = _client()
    c.execute_query('{"table": "adapter_kinds"}')
    c._kinds_cache = None
    c.execute_query('{"table": "adapter_kinds"}')
    assert len(fake.token_calls) == 1


def test_reacquires_token_exactly_once_on_401(patch_requests):
    fake = patch_requests({("GET", "/adapterkinds"): [_FakeResponse({"message": "expired"}, status_code=401),
                                                       _FakeResponse(ADAPTERS)]})
    c = _client()
    df = c.execute_query('{"table": "adapter_kinds"}')
    assert len(df) == 3
    assert len(fake.token_calls) == 2
    assert fake.calls[-1][3]["Authorization"] == "OpsToken tok-2"


def test_persistent_401_raises_after_one_retry(patch_requests):
    fake = patch_requests({("GET", "/adapterkinds"): _FakeResponse({"message": "nope"}, status_code=401)})
    with pytest.raises(RuntimeError, match="401"):
        _client().execute_query('{"table": "adapter_kinds"}')
    assert len(fake.token_calls) == 2


def test_bad_credentials_surface_auth_source(patch_requests):
    patch_requests({}, token_responses=[_FakeResponse({"message": "Invalid"}, status_code=401)])
    with pytest.raises(RuntimeError, match="corp.local"):
        _client().execute_query('{"table": "adapter_kinds"}')


def test_xml_answer_is_reported(patch_requests):
    patch_requests({("GET", "/adapterkinds"): _FakeResponse("<ops:error/>", content_type="application/xml")})
    with pytest.raises(RuntimeError, match="XML"):
        _client().execute_query('{"table": "adapter_kinds"}')


def test_ca_bundle_used_for_verification(patch_requests):
    c = _client(ca_bundle="/etc/ssl/corp.pem", verify_ssl=True)
    assert c._verify == "/etc/ssl/corp.pem"
    assert _client(verify_ssl=False)._verify is False


# ── pagination ────────────────────────────────────────────────────────────────

def test_pagination_follows_total_count(patch_requests):
    pages = {0: [VM] * 1000, 1: [DS] * 5}

    def resources_query(params, body):
        rows = pages[int(params["page"])]
        return {"resourceList": rows, "pageInfo": {"totalCount": 1005, "page": params["page"], "pageSize": 1000}}

    fake = patch_requests({("POST", "/resources/query"): resources_query})
    df = _client().execute_query('{"table": "resources", "resource_kind": "VirtualMachine", "limit": 5000}')
    assert len(df) == 1005
    pages_hit = sorted(int(c[2]["page"]) for c in fake.calls if c[1].endswith("/resources/query"))
    assert pages_hit == [0, 1]
    assert all(int(c[2]["pageSize"]) == 1000 for c in fake.calls)


# ── catalog ───────────────────────────────────────────────────────────────────

def test_get_schemas_fixed_catalog_shape(patch_requests):
    patch_requests({("GET", "/adapterkinds"): _FakeResponse({"message": "down"}, status_code=500)})
    tables = _client().get_schemas()
    names = [t.name for t in tables]
    assert names == list(_CATALOG)
    by = {t.name: t for t in tables}
    assert [c.name for c in by["alerts"].pks] == ["alertId"]
    assert {fk.references_name for fk in by["alerts"].fks} == {"resources", "alert_definitions"}
    assert {fk.references_name for fk in by["relationships"].fks} == {"resources"}
    assert "events" not in names  # the public API has no event READ endpoint


def test_get_schemas_discovers_wide_tables_for_populated_kinds_only(patch_requests):
    fake = patch_requests(_dictionary_routes())
    seen = []
    tables = _client().get_schemas(progress_callback=lambda *a: seen.append(a))
    names = [t.name for t in tables]
    wide = [n for n in names if n.startswith(METRIC_TABLE_PREFIX)]
    # VM and Datastore have resources, Pool has one; Port has zero → no table;
    # adapter-instance kinds and the self-monitoring adapter are skipped.
    assert set(wide) == {"metrics::VMWARE/VirtualMachine", "metrics::VMWARE/Datastore", "metrics::HitachiStorage/Pool"}
    vm_table = next(t for t in tables if t.name == "metrics::VMWARE/VirtualMachine")
    cols = [c.name for c in vm_table.columns]
    assert cols[:3] == ["resourceId", "resourceName", "timestamp"]
    assert "virtualDisk|totalLatency" in cols
    assert any("ms" in (c.description or "") for c in vm_table.columns if c.name == "virtualDisk|totalLatency")
    assert seen, "progress callback must be invoked during discovery"
    by = {t.name: t for t in tables}
    assert "HitachiStorage" in by["adapter_kinds"].description
    assert "VMWARE_ARIA_OPERATIONS" not in " ".join(n for n in wide)
    # count queries use a single-row page, never a full listing
    counts = [c for c in fake.calls if c[1].endswith("/resources/query")]
    assert counts and all(int(c[2]["pageSize"]) == 1 for c in counts)


def test_get_schemas_respects_max_metric_tables(patch_requests):
    patch_requests(_dictionary_routes())
    tables = _client(max_metric_tables=1).get_schemas()
    assert sum(t.name.startswith(METRIC_TABLE_PREFIX) for t in tables) == 1
    tables = _client(max_metric_tables=0).get_schemas()
    assert not any(t.name.startswith(METRIC_TABLE_PREFIX) for t in tables)


def test_get_schema_unknown_table():
    with pytest.raises(ValueError, match="Unknown Aria Operations table"):
        _client().get_schema("nope")


# ── stats ─────────────────────────────────────────────────────────────────────

def _stats_payload(params, body):
    return {"values": [
        {"resourceId": rid, "stat-list": {"stat": [
            {"statKey": {"key": k}, "timestamps": [1000, 2000], "data": [1.5, 9.0],
             "minThresholdData": [1.0, 1.0], "maxThresholdData": [3.0, 3.0]}
            for k in body["statKey"]]}}
        for rid in body["resourceId"]]}


def test_metrics_flattening_with_dynamic_thresholds(patch_requests):
    fake = patch_requests({**_dictionary_routes(), ("POST", "/resources/stats/query"): _stats_payload})
    df = _client().execute_query(json.dumps({
        "table": "metrics", "resource_id": ["vm-1"], "stat_key": ["virtualDisk|totalLatency", "cpu|usage_average"],
        "start_time": 1000, "end_time": 5000, "rollup": "MAX", "interval": "MINUTES", "interval_quantifier": 5, "dt": True,
    }))
    assert len(df) == 4
    assert set(df.statKey) == {"virtualDisk|totalLatency", "cpu|usage_average"}
    assert df.resourceName.iloc[0] == "prod-db-01"          # hydrated from resources/query
    assert list(df[df.statKey == "cpu|usage_average"].value) == [1.5, 9.0]
    assert list(df.dt_max.unique()) == [3.0]
    body = next(c[4] for c in fake.calls if c[1].endswith("/resources/stats/query"))
    assert body["begin"] == 1000 and body["end"] == 5000
    assert body["rollUpType"] == "MAX" and body["intervalType"] == "MINUTES" and body["intervalQuantifier"] == 5
    assert body["dt"] is True


def test_metrics_chunks_resource_ids_at_api_limit(patch_requests):
    ids = [f"vm-{i}" for i in range(ID_BATCH + 7)]

    def resources_query(params, body):
        return _page("resourceList", [_res(i, i, "VirtualMachine") for i in body.get("resourceId", [])])

    fake = patch_requests({("POST", "/resources/query"): resources_query,
                           ("POST", "/resources/stats/query"): _stats_payload})
    df = _client().execute_query(json.dumps({"table": "metrics", "resource_id": ids, "stat_key": "cpu|usage_average",
                                             "duration_in_mins": 10, "limit": 50000}))
    stats_calls = [c for c in fake.calls if c[1].endswith("/resources/stats/query")]
    assert [len(c[4]["resourceId"]) for c in stats_calls] == [ID_BATCH, 7]
    assert len(df) == 2 * len(ids)


def test_metrics_requires_scope_and_stat_key(patch_requests):
    patch_requests(_dictionary_routes())
    with pytest.raises(ValueError, match="stat_key"):
        _client().execute_query('{"table": "metrics", "resource_id": ["vm-1"]}')
    with pytest.raises(ValueError, match="scope"):
        _client().execute_query('{"table": "metrics", "stat_key": "cpu|usage_average"}')


def test_metrics_unknown_name_is_a_clear_error(patch_requests):
    patch_requests(_dictionary_routes())
    with pytest.raises(ValueError, match="No Aria Operations resources match"):
        _client().execute_query('{"table": "metrics", "name": "ghost", "resource_kind": "VirtualMachine", "stat_key": "cpu|usage_average"}')


def test_wide_metric_table_pivots_stat_keys_to_columns(patch_requests):
    patch_requests({**_dictionary_routes(), ("POST", "/resources/stats/query"): _stats_payload})
    df = _client().execute_query('{"table": "metrics::VMWARE/VirtualMachine", "duration_in_mins": 60, "limit": 100}')
    assert list(df.columns) == ["resourceId", "resourceName", "timestamp", "cpu|usage_average", "virtualDisk|totalLatency"]
    assert len(df) == 2                      # one VM × two timestamps
    assert list(df["cpu|usage_average"]) == [1.5, 9.0]


def test_topn_ranks_descending(patch_requests):
    def topn(params, body):
        return {"groupBy": "STATKEY", "resourceStatGroups": [{"groupKey": "cpu|usage_average", "resourceStats": [
            {"resourceId": "vm-1", "stat-list": {"stat": [{"statKey": {"key": "cpu|usage_average"}, "timestamps": [9], "data": [40.0]}]}},
            {"resourceId": "ds-1", "stat-list": {"stat": [{"statKey": {"key": "cpu|usage_average"}, "timestamps": [9], "data": [70.0]}]}},
        ]}]}
    fake = patch_requests({**_dictionary_routes(), ("GET", "/resources/stats/topn"): topn})
    df = _client().execute_query('{"table": "metrics_topn", "resource_id": ["vm-1", "ds-1"], "stat_key": "cpu|usage_average", "top_n": 2}')
    assert list(df["rank"]) == [1, 2]
    assert list(df.value) == [70.0, 40.0]
    params = next(c[2] for c in fake.calls if c[1].endswith("/stats/topn"))
    assert params["topN"] == 2 and params["sortOrder"] == "DESCENDING"


# ── topology ──────────────────────────────────────────────────────────────────

def test_relationships_edge_direction_and_depth(patch_requests):
    def rel(params, body):
        return _page("resourceList", [])

    def rel_for(rid, direction):
        table = {("vm-1", "CHILD"): [DS], ("vm-1", "PARENT"): [], ("ds-1", "CHILD"): [POOL], ("ds-1", "PARENT"): [VM],
                 ("pool-1", "CHILD"): [], ("pool-1", "PARENT"): []}
        return _page("resourceList", table.get((rid, direction), []))

    routes = _dictionary_routes()
    for rid in ("vm-1", "ds-1", "pool-1"):
        routes[("GET", f"/resources/{rid}/relationships")] = (lambda r: (lambda p, b: rel_for(r, p["relationshipType"])))(rid)
    patch_requests(routes)
    df = _client().execute_query('{"table": "relationships", "name": "prod-db-01", "resource_kind": "VirtualMachine", "depth": 2}')
    edges = {(r.parentName, r.childName, r.depth) for r in df.itertuples()}
    assert ("prod-db-01", "ds_prod_db_01", 1) in edges
    assert ("ds_prod_db_01", "Pool-07", 2) in edges
    assert all(r.childKind for r in df.itertuples())
    df1 = _client().execute_query('{"table": "relationships", "resource_id": "vm-1", "depth": 1}')
    assert set(df1.depth) == {1}


# ── alerts / symptoms ─────────────────────────────────────────────────────────

def test_alerts_window_keeps_open_and_overlapping_alerts(patch_requests):
    alerts = [
        {"alertId": "a-open", "resourceId": "vm-1", "alertLevel": "CRITICAL", "status": "ACTIVE", "startTimeUTC": 500, "updateTimeUTC": 500, "cancelTimeUTC": 0},
        {"alertId": "a-in", "resourceId": "ds-1", "alertLevel": "WARNING", "status": "CANCELED", "startTimeUTC": 1500, "updateTimeUTC": 1800, "cancelTimeUTC": 1800},
        {"alertId": "a-old", "resourceId": "ds-1", "alertLevel": "WARNING", "status": "CANCELED", "startTimeUTC": 100, "updateTimeUTC": 200, "cancelTimeUTC": 200},
        {"alertId": "a-future", "resourceId": "ds-1", "alertLevel": "WARNING", "status": "ACTIVE", "startTimeUTC": 9000, "updateTimeUTC": 9000, "cancelTimeUTC": 0},
    ]

    def alerts_query(params, body):
        rng = body.get("startTimeRange") or {}
        rows = [a for a in alerts if a["startTimeUTC"] <= rng.get("endTime", 10**18)]
        return _page("alerts", rows)

    fake = patch_requests({**_dictionary_routes(), ("POST", "/alerts/query"): alerts_query})
    df = _client().execute_query('{"table": "alerts", "start_time": 1000, "end_time": 2000}')
    assert list(df.alertId) == ["a-open", "a-in"]          # sorted by start; old + future excluded
    assert df.resourceName.tolist() == ["prod-db-01", "ds_prod_db_01"]
    body = next(c[4] for c in fake.calls if c[1].endswith("/alerts/query"))
    assert body["activeOnly"] is False and body["startTimeRange"]["endTime"] == 2000


def test_alerts_scope_and_level_are_forwarded(patch_requests):
    fake = patch_requests({**_dictionary_routes(), ("POST", "/alerts/query"): _page("alerts", [])})
    _client().execute_query('{"table": "alerts", "active_only": true, "resource_kind": "Datastore", "level": ["critical", "immediate"], "include_children": true}')
    body = next(c[4] for c in fake.calls if c[1].endswith("/alerts/query"))
    assert body["activeOnly"] is True
    assert body["alertCriticality"] == ["CRITICAL", "IMMEDIATE"]
    assert body["resource-query"]["resourceKind"] == ["Datastore"]
    assert body["includeChildrenResources"] is True


def test_contributing_symptoms_flatten(patch_requests):
    payload = {"contributingSymptoms": [{"alertId": "a-1", "contributingSymptoms": {"contributingSymptoms": [
        {"symptomId": "s-1", "symptomDefinitionsIds": ["SD-1"], "alertConditions": [{"severity": "CRITICAL"}]}]}}]}
    patch_requests({("GET", "/alerts/contributingsymptoms"): payload})
    df = _client().execute_query('{"table": "contributing_symptoms", "alert_id": "a-1"}')
    assert df.iloc[0].to_dict() == {"alertId": "a-1", "symptomId": "s-1", "symptomDefinitionId": "SD-1", "severity": "CRITICAL"}


def test_alert_definitions_severity_from_states(patch_requests):
    defs = _page("alertDefinitions", [{"id": "d1", "name": "Pool RT high", "description": "x", "adapterKindKey": "HitachiStorage",
                                       "resourceKindKey": "Pool", "waitCycles": 1, "cancelCycles": 1,
                                       "states": [{"severity": "CRITICAL"}]}])
    fake = patch_requests({("POST", "/alertdefinitions/query"): defs})
    df = _client().execute_query('{"table": "alert_definitions", "adapter_kind": "HitachiStorage"}')
    assert df.iloc[0].severity == "CRITICAL"
    assert next(c[4] for c in fake.calls)["adapterKinds"] == ["HitachiStorage"]


# ── validation / connection ───────────────────────────────────────────────────

def test_spec_validation():
    c = _client()
    with pytest.raises(ValueError, match="JSON object"):
        c.execute_query("SELECT 1")
    with pytest.raises(ValueError, match='"table"'):
        c.execute_query('{"name": "x"}')
    with pytest.raises(ValueError, match="Unknown Aria Operations table"):
        c.execute_query('{"table": "events"}')


def test_connection_success(patch_requests):
    patch_requests({("GET", "/adapterkinds"): ADAPTERS, ("GET", "/versions/current"): {"releaseName": "8.18.0"}})
    out = _client().test_connection()
    assert out["success"] is True
    assert "8.18.0" in out["message"] and "HitachiStorage" in out["message"]


def test_connection_auth_failure(patch_requests):
    patch_requests({}, token_responses=[_FakeResponse({"message": "Invalid"}, status_code=401)])
    out = _client().test_connection()
    assert out["success"] is False
    assert "401" in out["message"]


def test_connection_requires_credentials():
    out = AriaOperationsClient(url="https://aria.corp.local").test_connection()
    assert out["success"] is False
