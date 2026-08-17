"""Unit tests for AppDynamicsClient.

Covers:
- controller URL normalization (bare host, host:port, trailing /controller)
- the @-append rule: bare username → user@account Basic login; a username
  already containing "@" passes through unchanged (no double-append)
- `output=JSON` present on every request
- OAuth api_client mode: token fetch, cache, refresh-on-401 exactly once;
  Basic mode NEVER retries a 401 (controller lockout safety)
- get_schemas() virtual-table catalog shape (10 tables, pks, fks) and
  best-effort estate/service-map enrichment
- execute_query() dispatch per table, all-application spanning, service-flow
  edge parsing from External Calls metric names, metric_data flattening
- spec validation (bad JSON, unknown table, missing metric_path)
- test_connection() success / empty-world / auth-failure messages

The `requests` boundary is mocked, so these run without a live controller.
"""
from __future__ import annotations

import json

import pytest

from app.data_sources.clients.appdynamics_client import AppDynamicsClient, _CATALOG


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeRequests:
    """Replays scripted results keyed by URL path suffix; records every call."""

    class exceptions:  # mirror requests.exceptions used by the client
        class SSLError(Exception):
            pass

        class RequestException(Exception):
            pass

    def __init__(self, routes, token_responses=None):
        # routes: path-suffix -> payload | _FakeResponse | list of them (consumed in order)
        self.routes = routes
        self.token_responses = list(token_responses or [])
        self.calls = []   # (method, url, params, auth, headers)
        self.sessions_created = 0

    def Session(self):
        # The client keeps ONE requests.Session for HTTP keep-alive; route its
        # get/post back through this fake and count instantiations.
        self.sessions_created += 1
        fake = self

        class _S:
            def get(self, *a, **kw):
                return fake.get(*a, **kw)

            def post(self, *a, **kw):
                return fake.post(*a, **kw)

            def close(self):
                pass

        return _S()

    def _route(self, url):
        for suffix, val in self.routes.items():
            if url.split("?")[0].endswith(suffix):
                # A list of _FakeResponse acts as a queue (consumed in order);
                # any other list is a plain JSON payload.
                if isinstance(val, list) and val and all(isinstance(v, _FakeResponse) for v in val):
                    resp = val.pop(0) if len(val) > 1 else val[0]
                else:
                    resp = val
                return resp if isinstance(resp, _FakeResponse) else _FakeResponse(resp)
        return _FakeResponse({"error": "not scripted: " + url}, status_code=404)

    def get(self, url, params=None, verify=None, timeout=None, auth=None, headers=None):
        self.calls.append(("GET", url, dict(params or {}), auth, headers))
        return self._route(url)

    def post(self, url, data=None, headers=None, verify=None, timeout=None):
        self.calls.append(("POST", url, dict(data or {}), None, headers))
        if self.token_responses:
            resp = self.token_responses.pop(0)
            return resp if isinstance(resp, _FakeResponse) else _FakeResponse(resp)
        return _FakeResponse({"access_token": "tok-1", "expires_in": 300})


@pytest.fixture
def patch_requests(monkeypatch):
    def _install(routes, token_responses=None):
        fake = _FakeRequests(routes, token_responses)
        monkeypatch.setattr(
            "app.data_sources.clients.appdynamics_client.requests", fake
        )
        return fake
    return _install


APPS = [{"id": 100, "name": "retail-banking", "description": ""},
        {"id": 200, "name": "mobile-banking", "description": ""}]
TIERS_100 = [{"id": 10, "name": "web-portal", "numberOfNodes": 2},
             {"id": 11, "name": "api-services", "numberOfNodes": 3}]
TIERS_200 = [{"id": 20, "name": "mobile-gateway", "numberOfNodes": 1}]


def _client(**kw):
    defaults = dict(controller_url="https://appd.bank.local:8090",
                    account_name="customer1", username="svc_bow", password="pw")
    defaults.update(kw)
    return AppDynamicsClient(**defaults)


# ── URL + login normalization ────────────────────────────────────────────────

def test_url_normalization():
    assert _client().base_url == "https://appd.bank.local:8090"
    assert _client(controller_url="appd.bank.local:8090").base_url == "https://appd.bank.local:8090"
    assert _client(controller_url="https://appd.bank.local/controller/").base_url == "https://appd.bank.local"


def test_basic_login_appends_account():
    assert _client()._basic_login == "svc_bow@customer1"


def test_basic_login_no_double_append():
    c = _client(username="svc_bow@customer1")
    assert c._basic_login == "svc_bow@customer1"
    c2 = _client(username="svc_bow@someotheraccount")
    assert c2._basic_login == "svc_bow@someotheraccount"


def test_basic_auth_tuple_sent(patch_requests):
    fake = patch_requests({"/rest/applications": APPS})
    _client().test_connection()
    method, url, params, auth, headers = fake.calls[0]
    assert auth == ("svc_bow@customer1", "pw")
    assert headers is None


def test_single_session_reused_across_calls(patch_requests):
    fake = patch_requests({
        "/rest/applications": APPS,
        "/tiers": TIERS_100,
        "/metrics": [],
    })
    c = _client()
    c.test_connection()
    c.execute_query('{"table": "service_flows", "application": "retail-banking"}')
    assert len(fake.calls) >= 3
    assert fake.sessions_created == 1, "keep-alive requires one shared Session"


def test_applications_list_cached_across_queries(patch_requests, monkeypatch):
    fake = patch_requests({
        "/rest/applications": APPS,
        "/100/tiers": TIERS_100,
        "/200/tiers": TIERS_200,
        "/100/nodes": [], "/200/nodes": [],
    })
    c = _client()
    c.execute_query('{"table": "tiers"}')
    c.execute_query('{"table": "nodes"}')
    app_calls = [u for _, u, _, _, _ in fake.calls if u.endswith("/rest/applications")]
    assert len(app_calls) == 1, "applications list must be cached within the TTL"

    # After the TTL expires the list is refetched. (Capture the real clock
    # first — appdynamics_client.time IS the stdlib module, so patching its
    # `time` attribute would otherwise make the lambda call itself.)
    import time as _time
    real_time = _time.time
    monkeypatch.setattr(
        "app.data_sources.clients.appdynamics_client.time.time",
        lambda: real_time() + 3600,
    )
    c.execute_query('{"table": "tiers"}')
    app_calls = [u for _, u, _, _, _ in fake.calls if u.endswith("/rest/applications")]
    assert len(app_calls) == 2


def test_output_json_on_every_call(patch_requests):
    fake = patch_requests({
        "/rest/applications": APPS,
        "/tiers": TIERS_100,
        "/metrics": [],
    })
    c = _client()
    c.execute_query('{"table": "service_flows", "application": "retail-banking"}')
    assert fake.calls, "no calls made"
    for _, _, params, _, _ in fake.calls:
        assert params.get("output") == "JSON"


# ── OAuth api_client mode ────────────────────────────────────────────────────

def test_oauth_token_fetch_and_cache(patch_requests):
    fake = patch_requests({"/rest/applications": APPS})
    c = _client(username=None, password=None,
                client_name="bow_api", client_secret="s3cret")
    c.test_connection()
    c.test_connection()
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1, "token must be cached across requests"
    assert posts[0][2]["client_id"] == "bow_api@customer1"
    gets = [c for c in fake.calls if c[0] == "GET"]
    assert all(c[4] == {"Authorization": "Bearer tok-1"} for c in gets)


def test_oauth_refreshes_once_on_401(patch_requests):
    fake = patch_requests(
        {"/rest/applications": [
            _FakeResponse({"error": "expired"}, status_code=401),
            _FakeResponse(APPS),
        ]},
        token_responses=[{"access_token": "tok-1", "expires_in": 300},
                         {"access_token": "tok-2", "expires_in": 300}],
    )
    c = _client(username=None, password=None,
                client_name="bow_api", client_secret="s3cret")
    result = c.test_connection()
    assert result["success"] is True
    assert len([c for c in fake.calls if c[0] == "POST"]) == 2


def test_basic_auth_never_retries_401(patch_requests):
    fake = patch_requests({
        "/rest/applications": _FakeResponse({"error": "bad"}, status_code=401)
    })
    result = _client().test_connection()
    assert result["success"] is False
    assert "svc_bow@customer1" in result["message"]
    assert len(fake.calls) == 1, "a wrong password must not be retried (lockout)"


# ── schema discovery ─────────────────────────────────────────────────────────

def test_get_schemas_catalog_shape(patch_requests):
    patch_requests({"/rest/applications": [], })
    tables = _client().get_schemas()
    assert [t.name for t in tables] == list(_CATALOG)
    by_name = {t.name: t for t in tables}
    tiers = by_name["tiers"]
    assert [pk.name for pk in tiers.pks] == ["id"]
    assert [(fk.column.name, fk.references_name) for fk in tiers.fks] == [("application_id", "applications")]
    flows = by_name["service_flows"]
    assert {c.name for c in flows.columns} >= {"from_tier", "to_target", "to_type", "exit_type"}


def test_get_schemas_enrichment(patch_requests):
    patch_requests({
        "/rest/applications": [APPS[0]],
        "/tiers": TIERS_100,
        "/metrics": [{"name": 'Call-HTTP to api-services', "type": "folder"}],
    })
    tables = {t.name: t for t in _client().get_schemas()}
    assert "retail-banking" in tables["applications"].description
    assert "retail-banking/web-portal" in tables["tiers"].description
    assert "web-portal -[HTTP]-> api-services" in tables["service_flows"].description


def test_get_schemas_survives_api_failure(patch_requests):
    patch_requests({"/rest/applications": _FakeResponse({"e": 1}, status_code=500)})
    tables = _client().get_schemas()  # enrichment must never break discovery
    assert len(tables) == len(_CATALOG)


def test_get_schema_unknown_table():
    with pytest.raises(ValueError, match="Unknown AppDynamics table"):
        _client().get_schema("nope")


# ── querying ─────────────────────────────────────────────────────────────────

def test_execute_query_applications(patch_requests):
    patch_requests({"/rest/applications": APPS})
    df = _client().execute_query('{"table": "applications"}')
    assert list(df["name"]) == ["retail-banking", "mobile-banking"]


def test_execute_query_tiers_spans_all_apps(patch_requests):
    fake = patch_requests({
        "/rest/applications": APPS,
        "/100/tiers": TIERS_100,
        "/200/tiers": TIERS_200,
    })
    df = _client().execute_query('{"table": "tiers"}')
    assert len(df) == 3
    assert set(df["application_name"]) == {"retail-banking", "mobile-banking"}
    tier_urls = [u for _, u, _, _, _ in fake.calls if u.endswith("/tiers")]
    assert len(tier_urls) == 2


def test_service_flows_edge_parsing(patch_requests):
    patch_requests({
        "/rest/applications": [APPS[0]],
        "/tiers": TIERS_100,
        "/metrics": [
            {"name": 'Call-JDBC to Discovered backend call "oracle-db"', "type": "folder"},
            {"name": 'Call-HTTP to api-services', "type": "folder"},
            {"name": 'not an edge', "type": "leaf"},
        ],
    })
    df = _client().execute_query('{"table": "service_flows", "application": "retail-banking"}')
    edges = {(r.from_tier, r.to_target, r.to_type, r.exit_type) for r in df.itertuples()}
    assert ("web-portal", "oracle-db", "backend", "JDBC") in edges
    assert ("web-portal", "api-services", "tier", "HTTP") in edges
    assert not any(t == "not an edge" for _, t, _, _ in edges)


def test_metric_data_flattening_and_time_params(patch_requests):
    series = [{
        "metricId": 1, "metricName": "Average Response Time (ms)",
        "metricPath": "Business Transaction Performance|Business Transactions|t|/x|Average Response Time (ms)",
        "frequency": "ONE_MIN",
        "metricValues": [
            {"startTimeInMillis": 1, "value": 10.0, "min": 5, "max": 20, "sum": 10, "count": 1},
            {"startTimeInMillis": 2, "value": 30.0, "min": 25, "max": 40, "sum": 30, "count": 1},
        ],
    }]
    fake = patch_requests({
        "/rest/applications": [APPS[0]],
        "/metric-data": series,
    })
    df = _client().execute_query(json.dumps({
        "table": "metric_data", "application": "retail-banking",
        "metric_path": "Business Transaction Performance|Business Transactions|*|*|Average Response Time (ms)",
        "duration_in_mins": 30,
    }))
    assert list(df["value"]) == [10.0, 30.0]
    assert set(df["application_name"]) == {"retail-banking"}
    md_call = [c for c in fake.calls if c[1].endswith("/metric-data")][0]
    assert md_call[2]["time-range-type"] == "BEFORE_NOW"
    assert md_call[2]["duration-in-mins"] == "30"
    assert md_call[2]["rollup"] == "false"


def test_metric_data_requires_path(patch_requests):
    patch_requests({"/rest/applications": [APPS[0]]})
    with pytest.raises(ValueError, match="metric_path"):
        _client().execute_query('{"table": "metric_data", "application": "retail-banking"}')


def test_events_default_filters(patch_requests):
    fake = patch_requests({"/rest/applications": [APPS[0]], "/events": []})
    _client().execute_query('{"table": "events", "application": "retail-banking"}')
    ev_call = [c for c in fake.calls if c[1].endswith("/events")][0]
    assert "APPLICATION_ERROR" in ev_call[2]["event-types"]
    assert ev_call[2]["severities"] == "INFO,WARN,ERROR"


def test_explicit_time_range(patch_requests):
    fake = patch_requests({"/rest/applications": [APPS[0]],
                           "/problems/healthrule-violations": []})
    _client().execute_query(json.dumps({
        "table": "health_rule_violations", "application": "retail-banking",
        "start_time": 1000, "end_time": 2000,
    }))
    call = [c for c in fake.calls if "healthrule" in c[1]][0]
    assert call[2]["time-range-type"] == "BETWEEN_TIMES"
    assert call[2]["start-time"] == "1000" and call[2]["end-time"] == "2000"


def test_unknown_application(patch_requests):
    patch_requests({"/rest/applications": APPS})
    with pytest.raises(ValueError, match="not found"):
        _client().execute_query('{"table": "tiers", "application": "nope"}')


def test_spec_validation():
    c = _client()
    with pytest.raises(ValueError, match="JSON object"):
        c.execute_query("SELECT * FROM apps")
    with pytest.raises(ValueError, match='"table"'):
        c.execute_query('{"application": "x"}')
    with pytest.raises(ValueError, match="Unknown AppDynamics table"):
        c.execute_query('{"table": "wat"}')


# ── test_connection ──────────────────────────────────────────────────────────

def test_connection_success(patch_requests):
    patch_requests({"/rest/applications": APPS})
    result = _client().test_connection()
    assert result["success"] is True
    assert "2 applications visible" in result["message"]


def test_connection_empty_world(patch_requests):
    patch_requests({"/rest/applications": []})
    result = _client().test_connection()
    assert result["success"] is True
    assert "0 applications" in result["message"]


def test_connection_no_credentials():
    c = AppDynamicsClient(controller_url="https://x")
    result = c.test_connection()
    assert result["success"] is False
    assert "username/password" in result["message"]
