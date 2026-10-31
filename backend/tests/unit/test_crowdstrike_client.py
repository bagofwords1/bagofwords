"""Unit tests for CrowdStrikeClient.

Covers the client's contract with the Falcon API:
- cloud region resolution (explicit region, `auto` via X-Cs-Region, base_url override)
- OAuth2 client-credentials: form fields, member_cid, bearer on every call,
  refresh-on-401, bad credentials vs missing scopes in test_connection
- per-endpoint paging (offset / offset-token / after cursor), page sizes never
  above the endpoint maximum, and the ids → entities hydration batches
- sort syntax normalized to each endpoint's separator
- Spotlight's required filter defaulted; FQL passed through verbatim
- flattened nested fields + `fields` projection
- 429 back-off, legacy hosts fallback on 404, spec validation
- get_schemas() catalog shape and scope annotations

Only the `requests` boundary is faked: a small in-test Falcon server.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pandas as pd
import pytest

from app.data_sources.clients import crowdstrike_client as mod
from app.data_sources.clients.crowdstrike_client import MAX_ROWS, CrowdStrikeClient

# Endpoint maxima from the Falcon API (FalconPy endpoint definitions).
PAGE_MAX = {
    "/devices/combined/devices/v1": 10_000,
    "/devices/queries/devices-scroll/v1": 10_000,
    "/devices/combined/host-groups/v1": 5_000,
    "/alerts/queries/alerts/v2": 10_000,
    "/incidents/queries/incidents/v1": 500,
    "/incidents/combined/crowdscores/v1": 2_500,
    "/spotlight/combined/vulnerabilities/v1": 5_000,
    "/policy/combined/prevention/v1": 5_000,
    "/iocs/combined/indicator/v1": 2_000,
}
ENTITY_MAX = {
    "/alerts/entities/alerts/v2": ("composite_ids", 1_000),
    "/incidents/entities/incidents/GET/v1": ("ids", 500),
    "/devices/entities/devices/v2": ("ids", 5_000),
}


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeFalcon:
    """A scripted Falcon API. `data[path]` is the full record list served by
    that route; the fake applies the endpoint's paging and limit rules and
    validates every request the client makes."""

    def __init__(self, data=None, *, region_header="us-1", deny=(), token_status=201):
        self.data = data or {}
        self.region_header = region_header
        self.deny = set(deny)
        self.token_status = token_status
        self.requests = []           # (method, host, path, params, json/data, headers)
        self.tokens_issued = 0
        self.expire_next = 0         # answer this many API calls with 401
        self.rate_limit_next = 0     # answer this many API calls with 429
        self.missing = set()         # paths that 404
        self._cursors = {}

    # requests.Session surface ------------------------------------------------
    def __call__(self):
        return self

    headers: dict = {}
    verify = True

    def close(self):
        pass

    def post(self, url, data=None, timeout=None, allow_redirects=None):
        parts = urlsplit(url)
        self.requests.append(("POST", parts.netloc, parts.path, None, dict(data or {}), {}))
        assert parts.path == "/oauth2/token", "only the token request may use post()"
        if self.token_status != 201:
            return _Resp(self.token_status, {"errors": [{"code": self.token_status, "message": "Not Authorized"}]})
        self.tokens_issued += 1
        return _Resp(201, {"access_token": f"tok-{self.tokens_issued}", "expires_in": 1799},
                     headers={"X-Cs-Region": self.region_header})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        parts = urlsplit(url)
        path = parts.path
        self.requests.append((method, parts.netloc, path, dict(params or {}), json, dict(headers or {})))
        assert (headers or {}).get("Authorization", "").startswith("Bearer tok-")
        if self.expire_next:
            self.expire_next -= 1
            return _Resp(401, {"errors": [{"code": 401, "message": "access denied"}]})
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return _Resp(429, {"errors": [{"code": 429, "message": "rate limited"}]},
                         headers={"Retry-After": "1"})
        if path in self.deny:
            return _Resp(403, {"errors": [{"code": 403, "message": "access denied, authorization failed"}]})
        if path in self.missing:
            return _Resp(404, {"errors": [{"code": 404, "message": "Not Found"}]})
        if method == "POST":
            key, cap = ENTITY_MAX[path]
            ids = json[key]
            assert len(ids) <= cap, f"{path} got {len(ids)} ids (max {cap})"
            by_id = {r["id"]: r for r in self.data.get(path, [])}
            return _Resp(200, {"meta": {}, "resources": [by_id[i] for i in ids if i in by_id], "errors": []})
        assert method == "GET"
        params = dict(params or {})
        limit = int(params.get("limit", 100))
        assert 1 <= limit <= PAGE_MAX.get(path, 10_000), f"{path} limit {limit} above max"
        rows = self.data.get(path, [])
        if path in ("/spotlight/combined/vulnerabilities/v1",) and not params.get("filter"):
            return _Resp(400, {"errors": [{"code": 400, "message": "filter is required"}]})
        if path in ("/devices/combined/devices/v1", "/devices/queries/devices-scroll/v1"):
            start = self._cursors.get(params.get("offset"), 0)
            page = rows[start:start + limit]
            nxt = f"cur-{start + limit}" if start + limit < len(rows) else ""
            self._cursors[nxt] = start + limit
            return _Resp(200, {"meta": {"pagination": {"offset": nxt, "next": nxt, "total": len(rows)}},
                               "resources": page, "errors": []})
        if path in ("/spotlight/combined/vulnerabilities/v1", "/iocs/combined/indicator/v1"):
            start = self._cursors.get(params.get("after"), 0)
            page = rows[start:start + limit]
            nxt = f"after-{start + limit}" if start + limit < len(rows) else ""
            self._cursors[nxt] = start + limit
            return _Resp(200, {"meta": {"pagination": {"after": nxt, "total": len(rows)}},
                               "resources": page, "errors": []})
        offset = int(params.get("offset", 0))
        return _Resp(200, {"meta": {"pagination": {"offset": offset, "limit": limit, "total": len(rows)}},
                           "resources": rows[offset:offset + limit], "errors": []})

    # helpers ---------------------------------------------------------------
    def gets(self, path):
        return [r for r in self.requests if r[0] == "GET" and r[2] == path]

    def posts(self, path):
        return [r for r in self.requests if r[0] == "POST" and r[2] == path]


@pytest.fixture
def falcon(monkeypatch):
    def _install(**kw):
        fake = FakeFalcon(**kw)
        monkeypatch.setattr(mod.requests, "Session", fake)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        return fake
    return _install


def _client(**kw):
    args = {"client_id": "cid-abc", "client_secret": "s3cret", "region": "us-1"}
    args.update(kw)
    return CrowdStrikeClient(**args)


def _alerts(n):
    ids = [f"cid:ind:{i}" for i in range(n)]
    return ids, [{"id": i, "composite_id": i, "severity": 50 + (k % 50),
                  "device": {"device_id": f"d{k % 7}", "hostname": f"host-{k % 7}"}}
                 for k, i in enumerate(ids)]


# ── region / auth ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("region,host", [
    ("us-1", "api.crowdstrike.com"),
    ("us-2", "api.us-2.crowdstrike.com"),
    ("eu-1", "api.eu-1.crowdstrike.com"),
    ("us-gov-1", "api.laggar.gcw.crowdstrike.com"),
    ("us-gov-2", "api.us-gov-2.crowdstrike.mil"),
])
def test_explicit_region_targets_that_cloud(falcon, region, host):
    fake = falcon(data={"/policy/combined/prevention/v1": [{"id": "p1"}]}, region_header="eu-1")
    _client(region=region).execute_query({"table": "prevention_policies"})
    assert {r[1] for r in fake.requests} == {host}


def test_auto_region_follows_token_region_header(falcon):
    fake = falcon(data={"/policy/combined/prevention/v1": [{"id": "p1"}]}, region_header="us-2")
    _client(region="auto").execute_query({"table": "prevention_policies"})
    assert fake.requests[0][1] == "api.crowdstrike.com"          # token on us-1
    assert {r[1] for r in fake.requests[1:]} == {"api.us-2.crowdstrike.com"}


def test_base_url_override_wins_over_region(falcon):
    fake = falcon(data={"/policy/combined/prevention/v1": []}, region_header="eu-1")
    _client(region="us-2", base_url="falcon-gw.corp.example:8443/").execute_query(
        {"table": "prevention_policies"})
    assert {r[1] for r in fake.requests} == {"falcon-gw.corp.example:8443"}


@pytest.mark.parametrize("member_cid", [None, "child-cid-123"])
def test_token_request_sends_client_credentials(falcon, member_cid):
    fake = falcon(data={"/policy/combined/prevention/v1": []})
    _client(member_cid=member_cid).execute_query({"table": "prevention_policies"})
    form = fake.posts("/oauth2/token")[0][4]
    assert form["client_id"] == "cid-abc" and form["client_secret"] == "s3cret"
    assert form.get("member_cid") == member_cid


def test_expired_token_is_refreshed_once(falcon):
    fake = falcon(data={"/policy/combined/prevention/v1": [{"id": "p1"}]})
    fake.expire_next = 1
    df = _client().execute_query({"table": "prevention_policies"})
    assert len(df) == 1
    assert fake.tokens_issued == 2


def test_persistent_401_surfaces_an_error(falcon):
    fake = falcon(data={"/policy/combined/prevention/v1": []})
    fake.expire_next = 5
    with pytest.raises(RuntimeError):
        _client().execute_query({"table": "prevention_policies"})
    assert fake.tokens_issued == 2  # one refresh, no loop


def test_rate_limit_is_retried(falcon):
    fake = falcon(data={"/policy/combined/prevention/v1": [{"id": "p1"}]})
    fake.rate_limit_next = 2
    assert len(_client().execute_query({"table": "prevention_policies"})) == 1


# ── test_connection ───────────────────────────────────────────────────────────

def test_connection_reports_hosts_and_missing_scopes(falcon):
    hosts = [{"device_id": f"d{i}"} for i in range(42)]
    falcon(data={"/devices/combined/devices/v1": hosts},
           deny={"/spotlight/combined/vulnerabilities/v1", "/iocs/combined/indicator/v1"})
    res = _client().test_connection()
    assert res["success"] is True
    assert "42" in res["message"]
    assert "Vulnerabilities" in res["message"] and "IOC" in res["message"]


def test_connection_bad_credentials_fail_without_probing(falcon):
    fake = falcon(token_status=403)
    res = _client().test_connection()
    assert res["success"] is False
    assert "credential" in res["message"].lower()
    assert not [r for r in fake.requests if r[0] == "GET"]


def test_connection_with_no_readable_scope_fails(falcon):
    falcon(deny=set(PAGE_MAX))
    assert _client().test_connection()["success"] is False


# ── querying ──────────────────────────────────────────────────────────────────

def test_alerts_query_ids_then_hydrate_in_capped_batches(falcon):
    ids, rows = _alerts(2_500)
    fake = falcon(data={"/alerts/queries/alerts/v2": ids, "/alerts/entities/alerts/v2": rows})
    df = _client().execute_query({"table": "alerts", "limit": 5_000})
    assert len(df) == 2_500
    assert set(df["composite_id"]) == set(ids)
    assert len(fake.posts("/alerts/entities/alerts/v2")) >= 3   # cap enforced by the fake


@pytest.mark.parametrize("limit", [1, 499, 501, 1_234])
def test_incidents_paginate_within_endpoint_max(falcon, limit):
    ids = [f"inc:{i}" for i in range(1_300)]
    fake = falcon(data={"/incidents/queries/incidents/v1": ids,
                        "/incidents/entities/incidents/GET/v1": [{"id": i, "incident_id": i} for i in ids]})
    df = _client().execute_query({"table": "incidents", "limit": limit})
    assert len(df) == min(limit, len(ids))
    assert df["incident_id"].is_unique
    # Every id the query endpoint returned was hydrated in a POST of its own batch.
    hydrated = [i for r in fake.posts("/incidents/entities/incidents/GET/v1") for i in r[4]["ids"]]
    assert sorted(hydrated) == sorted(df["incident_id"])


@pytest.mark.parametrize("table,path,n", [
    ("hosts", "/devices/combined/devices/v1", 12_345),
    ("vulnerabilities", "/spotlight/combined/vulnerabilities/v1", 7_001),
    ("iocs", "/iocs/combined/indicator/v1", 2_222),
])
def test_cursor_paging_returns_every_row_up_to_max(falcon, table, path, n):
    falcon(data={path: [{"id": f"r{i}", "device_id": f"r{i}"} for i in range(n)]})
    df = _client().execute_query({"table": table, "limit": 50_000})
    assert len(df) == min(n, MAX_ROWS)
    assert df["id"].is_unique


@pytest.mark.parametrize("table,path,given,expected", [
    ("alerts", "/alerts/queries/alerts/v2", "created_timestamp.desc", "created_timestamp|desc"),
    ("alerts", "/alerts/queries/alerts/v2", "severity|asc", "severity|asc"),
    ("incidents", "/incidents/queries/incidents/v1", "fine_score|desc", "fine_score.desc"),
    ("hosts", "/devices/combined/devices/v1", "last_seen desc", "last_seen.desc"),
    ("vulnerabilities", "/spotlight/combined/vulnerabilities/v1", "updated_timestamp.asc", "updated_timestamp|asc"),
])
def test_sort_is_normalized_to_endpoint_syntax(falcon, table, path, given, expected):
    fake = falcon()
    _client().execute_query({"table": table, "sort": given})
    assert fake.gets(path)[0][3]["sort"] == expected


def test_vulnerabilities_default_filter_and_facets(falcon):
    fake = falcon()
    _client().execute_query({"table": "vulnerabilities"})
    params = fake.gets("/spotlight/combined/vulnerabilities/v1")[0][3]
    assert params["filter"]
    assert {"cve", "host_info"} <= set(params["facet"])


def test_fql_filter_is_passed_through_verbatim(falcon):
    fake = falcon()
    fql = "severity:>=70+device.hostname:*'FIN-*'+status:['new','reopened']"
    _client().execute_query({"table": "alerts", "filter": fql})
    assert fake.gets("/alerts/queries/alerts/v2")[0][3]["filter"] == fql


def test_nested_fields_flatten_and_project(falcon):
    ids, rows = _alerts(10)
    falcon(data={"/alerts/queries/alerts/v2": ids, "/alerts/entities/alerts/v2": rows})
    df = _client().execute_query({"table": "alerts", "fields": ["composite_id", "device.hostname"]})
    assert list(df.columns) == ["composite_id", "device.hostname"]
    assert df["device.hostname"].notna().all()


def test_hosts_fall_back_to_scroll_when_combined_missing(falcon):
    hosts = [{"id": f"d{i}", "device_id": f"d{i}", "hostname": f"h{i}"} for i in range(1_200)]
    fake = falcon(data={"/devices/queries/devices-scroll/v1": [h["device_id"] for h in hosts],
                        "/devices/entities/devices/v2": hosts})
    fake.missing.add("/devices/combined/devices/v1")
    df = _client().execute_query({"table": "hosts", "limit": 2_000})
    assert len(df) == 1_200 and set(df["hostname"]) == {h["hostname"] for h in hosts}


def test_empty_result_is_an_empty_frame(falcon):
    falcon()
    df = _client().execute_query({"table": "iocs"})
    assert isinstance(df, pd.DataFrame) and df.empty


def test_path_escape_hatch_is_read_only_get(falcon):
    fake = falcon(data={"/devices/combined/host-group-members/v1": [{"device_id": "d1"}]})
    df = _client().execute_query({"path": "/devices/combined/host-group-members/v1",
                                  "params": {"id": "g1"}})
    assert len(df) == 1
    assert [r[0] for r in fake.requests if r[2] != "/oauth2/token"] == ["GET"]


@pytest.mark.parametrize("bad", [
    "not json",
    {"limit": 5},
    {"table": "detections"},
    {"path": "https://evil.example/x"},
    {"path": "//evil.example/x"},
    {"path": "devices/v1"},
    {"path": "/devices/../../x"},
])
def test_invalid_specs_are_rejected_before_any_call(falcon, bad):
    fake = falcon()
    with pytest.raises(ValueError):
        _client().execute_query(bad)
    assert fake.requests == []


def test_forbidden_table_raises_readable_scope_error(falcon):
    falcon(deny={"/spotlight/combined/vulnerabilities/v1"})
    with pytest.raises(RuntimeError, match="(?i)scope"):
        _client().execute_query({"table": "vulnerabilities"})


# ── schema ────────────────────────────────────────────────────────────────────

def test_schema_catalog_shape(falcon):
    falcon(deny={"/iocs/combined/indicator/v1"})
    tables = {t.name: t for t in _client().get_schemas()}
    assert set(tables) == {"hosts", "host_groups", "alerts", "incidents", "crowdscores",
                           "vulnerabilities", "prevention_policies", "iocs"}
    for t in tables.values():
        assert t.columns and t.pks, t.name
    fk_targets = {(t.name, fk.references_name) for t in tables.values() for fk in t.fks}
    assert ("alerts", "hosts") in fk_targets and ("vulnerabilities", "hosts") in fk_targets
    assert "IOC Management" in tables["iocs"].description
    assert "lacks" not in tables["hosts"].description


def test_schema_discovery_survives_auth_failure(falcon):
    falcon(token_status=403)
    assert len(_client().get_schemas()) == 8
