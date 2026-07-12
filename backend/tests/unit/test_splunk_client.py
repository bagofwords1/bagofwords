"""Unit tests for SplunkClient.

Covers:
- URL normalization (bare host / host:port / full URL) to the mgmt base
- auth header selection: Bearer (token) vs HTTP basic (userpass)
- SPL normalization (bare search gets a leading `search`; `|`/`search ` kept)
- get_schemas(): index::sourcetype catalog + top-K field sampling cap
  (sourcetypes beyond the cap stay THIN — the unknown-schema path)
- best-effort enrichment: a field-sample failure degrades to a thin table,
  never fails discovery
- execute_query(): SPL string vs JSON envelope, limit cap, DataFrame shape
- test_connection() success / failure

The search boundary (`_run_search`) is mocked, so these run with no live Splunk.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.data_sources.clients.splunk_client import SplunkClient, MAX_ROWS, CATALOG_SEARCH


# ---------- url + auth ---------- #

def test_url_normalization():
    assert SplunkClient(host="splunk.acme.com").base_url == "https://splunk.acme.com:8089"
    assert SplunkClient(host="splunk.acme.com", port=9089).base_url == "https://splunk.acme.com:9089"
    assert SplunkClient(host="https://sp:8089").base_url == "https://sp:8089"


def test_auth_token_bearer():
    c = SplunkClient(host="h", api_token="tok")
    assert c._headers() == {"Authorization": "Bearer tok"}
    assert c._auth() is None


def test_auth_userpass_basic():
    c = SplunkClient(host="h", username="admin", password="pw")
    assert c._headers() == {}
    assert c._auth() == ("admin", "pw")


# ---------- SPL normalization ---------- #

def test_normalize_spl():
    assert SplunkClient._normalize_spl("index=web") == "search index=web"
    assert SplunkClient._normalize_spl("search index=web") == "search index=web"
    assert SplunkClient._normalize_spl("| tstats count") == "| tstats count"
    with pytest.raises(ValueError):
        SplunkClient._normalize_spl("   ")


# ---------- schema discovery ---------- #

def _catalog_rows():
    # Ranked deliberately out of order to prove get_schemas sorts by volume.
    return [
        {"index": "security", "sourcetype": "auth_audit", "count": "1000"},
        {"index": "web", "sourcetype": "access_combined", "count": "6000"},
        {"index": "app", "sourcetype": "log4j", "count": "3000"},
    ]


def test_top_k_cap_leaves_tail_thin(monkeypatch):
    c = SplunkClient(host="h", api_token="t", max_sampled_sourcetypes=2)
    calls = {"sampled": []}

    def fake_run(spl, earliest=None, latest=None, count=1000):
        if spl == CATALOG_SEARCH:
            return _catalog_rows()
        # a fieldsummary sample
        calls["sampled"].append(spl)
        return [{"field": "status", "count": "500", "numeric_count": "500"},
                {"field": "method", "count": "500", "numeric_count": "0"}]

    monkeypatch.setattr(c, "_run_search", fake_run)
    tables = {t.name: t for t in c.get_schemas()}

    # Ranked by volume: web(6000) and app(3000) sampled; security(1000) thin.
    assert tables["web::access_combined"].columns  # sampled
    assert tables["app::log4j"].columns            # sampled
    assert tables["security::auth_audit"].columns == []  # thin (beyond cap)
    # Only the top-2 sourcetypes triggered a sample search.
    assert len(calls["sampled"]) == 2
    # Thin table's description tells the agent to discover fields first.
    assert "fieldsummary" in tables["security::auth_audit"].description


def test_field_dtype_inference(monkeypatch):
    c = SplunkClient(host="h", api_token="t", max_sampled_sourcetypes=10)

    def fake_run(spl, earliest=None, latest=None, count=1000):
        if spl == CATALOG_SEARCH:
            return [{"index": "web", "sourcetype": "access", "count": "10"}]
        return [{"field": "status", "count": "100", "numeric_count": "100"},
                {"field": "uri", "count": "100", "numeric_count": "0"}]

    monkeypatch.setattr(c, "_run_search", fake_run)
    cols = {col.name: col.dtype for col in c.get_schemas()[0].columns}
    assert cols["status"] == "float"  # numeric
    assert cols["uri"] == "str"       # non-numeric


def test_field_sample_failure_degrades_to_thin(monkeypatch):
    c = SplunkClient(host="h", api_token="t", max_sampled_sourcetypes=10)

    def fake_run(spl, earliest=None, latest=None, count=1000):
        if spl == CATALOG_SEARCH:
            return [{"index": "web", "sourcetype": "access", "count": "10"}]
        raise RuntimeError("search quota exceeded")

    monkeypatch.setattr(c, "_run_search", fake_run)
    tables = c.get_schemas()
    # Discovery still succeeds; the table is just thin.
    assert len(tables) == 1
    assert tables[0].columns == []


def test_get_schema_samples_on_demand(monkeypatch):
    c = SplunkClient(host="h", api_token="t")

    def fake_run(spl, earliest=None, latest=None, count=1000):
        assert 'sourcetype="log4j"' in spl and "fieldsummary" in spl
        return [{"field": "level", "count": "5", "numeric_count": "0"}]

    monkeypatch.setattr(c, "_run_search", fake_run)
    t = c.get_schema("app::log4j")
    assert t.name == "app::log4j"
    assert [col.name for col in t.columns] == ["level"]


# ---------- query execution ---------- #

def test_execute_query_bare_spl(monkeypatch):
    c = SplunkClient(host="h", api_token="t")
    captured = {}

    def fake_run(spl, earliest=None, latest=None, count=1000):
        captured.update(spl=spl, earliest=earliest, count=count)
        return [{"host": "h1", "count": "5"}]

    monkeypatch.setattr(c, "_run_search", fake_run)
    df = c.execute_query("search index=web | stats count by host")
    assert captured["spl"].startswith("search index=web")
    assert list(df.columns) == ["host", "count"]


def test_execute_query_envelope_limit_and_time(monkeypatch):
    c = SplunkClient(host="h", api_token="t")
    captured = {}

    def fake_run(spl, earliest=None, latest=None, count=1000):
        captured.update(spl=spl, earliest=earliest, latest=latest, count=count)
        return []

    monkeypatch.setattr(c, "_run_search", fake_run)
    c.execute_query('{"spl": "search index=web", "earliest": "-1h", "latest": "now", "limit": 250}')
    assert captured["earliest"] == "-1h"
    assert captured["latest"] == "now"
    assert captured["count"] == 250


def test_execute_query_limit_capped(monkeypatch):
    c = SplunkClient(host="h", api_token="t")
    captured = {}
    monkeypatch.setattr(c, "_run_search",
                        lambda spl, earliest=None, latest=None, count=1000: captured.update(count=count) or [])
    c.execute_query({"spl": "search index=web", "limit": 10 ** 9})
    assert captured["count"] == MAX_ROWS


def test_execute_query_missing_spl_raises():
    c = SplunkClient(host="h", api_token="t")
    with pytest.raises(ValueError, match="SPL"):
        c.execute_query('{"earliest": "-1h"}')


def test_execute_query_empty_results_empty_df(monkeypatch):
    c = SplunkClient(host="h", api_token="t")
    monkeypatch.setattr(c, "_run_search", lambda *a, **k: [])
    df = c.execute_query("search index=web")
    assert isinstance(df, pd.DataFrame) and df.empty


# ---------- connection ---------- #

def test_test_connection_success(monkeypatch):
    c = SplunkClient(host="h", api_token="t")
    monkeypatch.setattr(c, "_get",
                        lambda path, params=None: {"entry": [{"content": {"version": "9.3.14"}}]})
    res = c.test_connection()
    assert res["success"] and "9.3.14" in res["message"]


def test_test_connection_failure(monkeypatch):
    c = SplunkClient(host="h", api_token="t")

    def boom(*a, **k):
        raise RuntimeError("401 auth failed")

    monkeypatch.setattr(c, "_get", boom)
    res = c.test_connection()
    assert res["success"] is False and "auth failed" in res["message"]
