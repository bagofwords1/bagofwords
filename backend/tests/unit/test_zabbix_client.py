"""Unit tests for ZabbixClient.

Covers:
- URL normalization to the /api_jsonrpc.php endpoint
- auth header handling: Bearer only on authed calls (token mode);
  `auth` field + user.login round-trip (userpass mode)
- get_schemas() virtual-table catalog shape (columns, pks, fks)
- execute_query() method dispatch, output/limit defaults, DataFrame shape
- spec validation (bad JSON, unknown table, missing table/method)
- test_connection() success / failure and JSON-RPC error surfacing

The `requests` boundary is mocked, so these run without a live Zabbix server.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.data_sources.clients.zabbix_client import ZabbixClient, _CATALOG


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeSession:
    """Records every POST and replays scripted results keyed by method name."""

    def __init__(self, results):
        # results: method -> value (the JSON-RPC `result`) OR an Exception/dict error
        self._results = results
        self.headers = {}
        self.verify = True
        self.calls = []  # list of (method, params, headers, auth_field)

    def post(self, url, data=None, headers=None, timeout=None):
        payload = json.loads(data)
        method = payload["method"]
        self.calls.append((method, payload.get("params"), headers, payload.get("auth")))
        if method not in self._results:
            return _FakeResponse({"error": {"code": -32601, "message": "no result scripted"}})
        val = self._results[method]
        if isinstance(val, dict) and "error" in val:
            return _FakeResponse(val)
        return _FakeResponse({"jsonrpc": "2.0", "result": val, "id": payload["id"]})

    def close(self):
        pass


@pytest.fixture
def patch_session(monkeypatch):
    """Return a helper that installs a _FakeSession for a given results map."""
    holder = {}

    def _install(results):
        session = _FakeSession(results)
        holder["session"] = session
        monkeypatch.setattr("app.data_sources.clients.zabbix_client.requests.Session",
                            lambda: session)
        return session

    return _install


# ---------- URL normalization ---------- #

class TestUrlNormalization:
    def test_bare_host_gets_endpoint(self):
        c = ZabbixClient(url="https://zbx.example.com", api_token="t")
        assert c.endpoint == "https://zbx.example.com/api_jsonrpc.php"

    def test_trailing_slash(self):
        c = ZabbixClient(url="https://zbx.example.com/", api_token="t")
        assert c.endpoint == "https://zbx.example.com/api_jsonrpc.php"

    def test_full_endpoint_kept(self):
        c = ZabbixClient(url="https://zbx.example.com/api_jsonrpc.php", api_token="t")
        assert c.endpoint == "https://zbx.example.com/api_jsonrpc.php"


# ---------- auth handling ---------- #

class TestAuth:
    def test_token_bearer_only_on_authed_calls(self, patch_session):
        session = patch_session({"apiinfo.version": "7.0.0", "host.get": 1})
        c = ZabbixClient(url="http://z", api_token="tok123")
        assert c.test_connection()["success"] is True
        by_method = {m: (hdr, auth) for m, _p, hdr, auth in session.calls}
        # apiinfo.version must NOT carry the Authorization header
        assert by_method["apiinfo.version"][0] in (None, {})
        # host.get (authed) carries the Bearer header, no auth field
        assert by_method["host.get"][0]["Authorization"] == "Bearer tok123"
        assert by_method["host.get"][1] is None

    def test_userpass_login_puts_token_in_auth_field(self, patch_session):
        session = patch_session({
            "apiinfo.version": "7.0.0",
            "user.login": "sess-token-abc",
            "host.get": 1,
        })
        c = ZabbixClient(url="http://z", username="u", password="p")
        assert c.test_connection()["success"] is True
        calls = {m: auth for m, _p, _h, auth in session.calls}
        # user.login itself carries no auth; host.get carries the session token
        assert calls["user.login"] is None
        assert calls["host.get"] == "sess-token-abc"

    def test_missing_credentials_raises_on_authed_call(self, patch_session):
        patch_session({"apiinfo.version": "7.0.0"})
        c = ZabbixClient(url="http://z")  # no token, no userpass
        res = c.test_connection()
        assert res["success"] is False
        assert "token" in res["message"].lower() or "password" in res["message"].lower()


# ---------- schema catalog ---------- #

class TestSchemas:
    def test_catalog_tables_present(self, patch_session):
        patch_session({"item.get": []})
        c = ZabbixClient(url="http://z", api_token="t")
        names = {t.name for t in c.get_schemas()}
        assert names == set(_CATALOG)

    def test_items_table_has_hostid_fk(self, patch_session):
        patch_session({"item.get": []})
        c = ZabbixClient(url="http://z", api_token="t")
        items = c.get_schema("items")
        assert items.pks[0].name == "itemid"
        fk_targets = {(f.column.name, f.references_name) for f in items.fks}
        assert ("hostid", "hosts") in fk_targets

    def test_history_fk_to_items(self, patch_session):
        patch_session({})
        c = ZabbixClient(url="http://z", api_token="t")
        hist = c.get_schema("history")
        assert ("itemid", "items") in {(f.column.name, f.references_name) for f in hist.fks}

    def test_unknown_table_raises(self):
        c = ZabbixClient(url="http://z", api_token="t")
        with pytest.raises(ValueError):
            c.get_schema("does_not_exist")

    def test_enrichment_never_breaks_discovery(self, patch_session):
        # item.get errors → discovery still returns the full catalog
        patch_session({"item.get": {"error": {"code": -1, "message": "boom"}}})
        c = ZabbixClient(url="http://z", api_token="t")
        assert len(c.get_schemas()) == len(_CATALOG)


# ---------- query dispatch ---------- #

class TestExecuteQuery:
    def test_table_maps_to_get_method(self, patch_session):
        session = patch_session({"problem.get": [{"eventid": "1", "severity": "5"}]})
        c = ZabbixClient(url="http://z", api_token="t")
        df = c.execute_query('{"table": "problems", "limit": 10}')
        assert session.calls[0][0] == "problem.get"
        assert list(df.columns) == ["eventid", "severity"]
        assert len(df) == 1

    def test_output_and_limit_defaults(self, patch_session):
        session = patch_session({"host.get": []})
        c = ZabbixClient(url="http://z", api_token="t")
        c.execute_query('{"table": "hosts"}')
        params = session.calls[0][1]
        assert params["output"] == "extend"
        assert params["limit"] == 500  # DEFAULT_LIMIT

    def test_params_passed_through(self, patch_session):
        session = patch_session({"item.get": []})
        c = ZabbixClient(url="http://z", api_token="t")
        c.execute_query(json.dumps({"table": "items", "params": {"hostids": ["10"]}}))
        assert session.calls[0][1]["hostids"] == ["10"]

    def test_method_escape_hatch(self, patch_session):
        session = patch_session({"trigger.get": []})
        c = ZabbixClient(url="http://z", api_token="t")
        c.execute_query('{"method": "trigger.get", "params": {}}')
        assert session.calls[0][0] == "trigger.get"

    def test_count_output_omits_limit(self, patch_session):
        session = patch_session({"host.get": 42})
        c = ZabbixClient(url="http://z", api_token="t")
        df = c.execute_query('{"table": "hosts", "params": {"countOutput": true}}')
        assert "limit" not in session.calls[0][1]
        assert df.iloc[0]["result"] == 42

    def test_dict_query_accepted(self, patch_session):
        session = patch_session({"host.get": []})
        c = ZabbixClient(url="http://z", api_token="t")
        c.execute_query({"table": "hosts"})
        assert session.calls[0][0] == "host.get"

    def test_bad_json_raises(self):
        c = ZabbixClient(url="http://z", api_token="t")
        with pytest.raises(ValueError):
            c.execute_query("not json")

    def test_missing_table_and_method_raises(self):
        c = ZabbixClient(url="http://z", api_token="t")
        with pytest.raises(ValueError):
            c.execute_query('{"limit": 5}')

    def test_unknown_table_raises(self):
        c = ZabbixClient(url="http://z", api_token="t")
        with pytest.raises(ValueError):
            c.execute_query('{"table": "nope"}')


# ---------- connection test ---------- #

class TestConnection:
    def test_success_reports_version(self, patch_session):
        patch_session({"apiinfo.version": "7.0.28", "host.get": 1})
        c = ZabbixClient(url="http://z", api_token="t")
        res = c.test_connection()
        assert res["success"] is True
        assert "7.0.28" in res["message"]

    def test_api_error_surfaced(self, patch_session):
        patch_session({
            "apiinfo.version": "7.0.0",
            "host.get": {"error": {"code": -32602, "message": "Invalid params.",
                                   "data": "bad session"}},
        })
        c = ZabbixClient(url="http://z", api_token="t")
        res = c.test_connection()
        assert res["success"] is False
        assert "Invalid params" in res["message"]
