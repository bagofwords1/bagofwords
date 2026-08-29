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
- hardened deployments that REJECT wildcard index searches (`index=*`):
  discovery falls back to REST index enumeration + explicit OR-list tstats,
  never emits another wildcard, never stores `*` as an index, resolves legacy
  `*::sourcetype` names, and enriches wildcard-rejection errors with the
  known index list so the agent can self-correct

- dashboards & saved searches: catalog as `dashboard::app/name` /
  `saved_search::app/name` tables (panels = columns carrying the panel SPL),
  Simple XML and Dashboard Studio parsing (incl. base/chain searches),
  system-app + stock-content filtering, envelope routing in execute_query
  (panel inventory / run-a-panel / run-a-saved-search with time overrides),
  get_schema hydration, and best-effort listing that never fails discovery

The search boundary (`_run_search`) is mocked, so these run with no live Splunk.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.data_sources.clients.splunk_client import (
    SplunkClient, MAX_ROWS, CATALOG_SEARCH, _WILDCARD_INDEX,
)


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


# ---------- hardened deployments (wildcard index searches rejected) ---------- #

_REST_INDEXES = {"entry": [{"name": "web"}, {"name": "app"},
                           {"name": "security"}, {"name": "_internal"}]}


def _wildcard_rejecting_run(ran, pairs, allow_metadata=False):
    """A fake `_run_search` mimicking a hardened Splunk: any SPL containing
    `index=*` is rejected (optionally excepting `| metadata`)."""
    def fake_run(spl, earliest=None, latest=None, count=1000):
        ran.append(spl)
        if spl.lstrip("|").strip().lower().startswith("metadata"):
            if allow_metadata:
                return [{"sourcetype": p["sourcetype"], "totalCount": p["count"]}
                        for p in pairs]
            raise RuntimeError("wildcard index searches are not permitted")
        if _WILDCARD_INDEX.search(spl):
            raise RuntimeError("You must specify a specific index; "
                               "wildcard index searches are not permitted")
        if spl.startswith("| tstats") and "sourcetype=" not in spl:
            return pairs  # scoped OR-list catalog
        if "fieldsummary" in spl:
            return [{"field": "status", "count": "10", "numeric_count": "10"}]
        return []
    return fake_run


def test_wildcard_blocked_discovery_falls_back_to_rest_enumeration(monkeypatch):
    """When `index=*` is rejected, discovery enumerates indexes over REST and
    still produces real `index::sourcetype` tables with sampled fields —
    emitting no wildcard search beyond the initial fast-path attempt."""
    c = SplunkClient(host="h", api_token="t", max_sampled_sourcetypes=10)
    ran: list = []
    pairs = [{"index": "web", "sourcetype": "access_combined", "count": "6000"},
             {"index": "app", "sourcetype": "log4j", "count": "3000"}]
    monkeypatch.setattr(c, "_run_search", _wildcard_rejecting_run(ran, pairs))
    monkeypatch.setattr(c, "_get", lambda path, params=None: _REST_INDEXES)

    tables = {t.name: t for t in c.get_schemas()}

    assert set(tables) == {"web::access_combined", "app::log4j"}
    assert tables["web::access_combined"].columns          # sampled, not thin
    assert tables["app::log4j"].metadata_json["index"] == "app"
    # The scoped catalog used an explicit OR list of the REST-enumerated
    # indexes (internal `_internal` excluded).
    scoped = [s for s in ran if s.startswith("| tstats") and "(" in s]
    assert scoped and "(index=web OR index=app OR index=security)" in scoped[0]
    # Only the initial fast-path tstats used a wildcard; nothing after it did.
    wildcards = [s for s in ran if _WILDCARD_INDEX.search(s)]
    assert wildcards == [CATALOG_SEARCH]


def test_metadata_last_resort_never_stores_wildcard_index(monkeypatch):
    """When index enumeration is ALSO denied, the metadata catalog still
    surfaces sourcetypes — with the index recorded as unknown (None), never
    `*`, and no wildcard field sampling attempted."""
    c = SplunkClient(host="h", api_token="t", max_sampled_sourcetypes=10)
    ran: list = []
    pairs = [{"index": None, "sourcetype": "log4j", "count": "3000"}]
    monkeypatch.setattr(c, "_run_search",
                        _wildcard_rejecting_run(ran, pairs, allow_metadata=True))
    monkeypatch.setattr(c, "_get",
                        lambda path, params=None: (_ for _ in ()).throw(RuntimeError("403")))

    tables = c.get_schemas()

    assert [t.name for t in tables] == ["log4j"]           # bare sourcetype, no '*::'
    assert tables[0].metadata_json["index"] is None
    # The description must not INSTRUCT a wildcard search (the old behavior
    # was `run search index=* sourcetype=...`); warning against it is fine.
    assert "search index=*" not in tables[0].description
    assert "do NOT use `index=*`" in tables[0].description
    # No wildcard search after the initial fast-path attempt (sampling was
    # skipped entirely rather than emitted with `index=*`).
    wildcards = [s for s in ran if _WILDCARD_INDEX.search(s)
                 and not s.lstrip("|").strip().lower().startswith("metadata")]
    assert wildcards == [CATALOG_SEARCH]


def test_config_indexes_scope_skips_wildcard_entirely(monkeypatch):
    """An admin-scoped `indexes` config never tries the wildcard fast path."""
    c = SplunkClient(host="h", api_token="t", indexes="web, app", max_sampled_sourcetypes=0)
    ran: list = []
    pairs = [{"index": "web", "sourcetype": "access_combined", "count": "1"}]
    monkeypatch.setattr(c, "_run_search", _wildcard_rejecting_run(ran, pairs))

    tables = c.get_schemas()

    assert [t.name for t in tables] == ["web::access_combined"]
    assert all(not _WILDCARD_INDEX.search(s) for s in ran)
    assert "(index=web OR index=app)" in ran[0]


def test_get_schema_resolves_legacy_wildcard_table_name(monkeypatch):
    """A legacy `*::sourcetype` table (stored by the old fallback) has its
    index resolved through the index catalog instead of emitting `index=*`."""
    c = SplunkClient(host="h", api_token="t")
    ran: list = []

    def fake_run(spl, earliest=None, latest=None, count=1000):
        ran.append(spl)
        assert not _WILDCARD_INDEX.search(spl), f"wildcard emitted: {spl}"
        if spl.startswith("| tstats") and 'sourcetype="log4j"' in spl:
            return [{"index": "app", "count": "3000"}]
        assert "index=app" in spl and "fieldsummary" in spl
        return [{"field": "level", "count": "5", "numeric_count": "0"}]

    monkeypatch.setattr(c, "_run_search", fake_run)
    monkeypatch.setattr(c, "_get", lambda path, params=None: _REST_INDEXES)

    t = c.get_schema("*::log4j")
    assert [col.name for col in t.columns] == ["level"]
    assert t.metadata_json["sourcetype"] == "log4j"


def test_wildcard_rejection_error_includes_known_indexes(monkeypatch):
    """A rejected `index=*` search raises an error enriched with the known
    index list, so the agent can rewrite with an explicit index."""
    c = SplunkClient(host="h", api_token="t")

    class FakeResp:
        status_code = 200
        def json(self):
            return {"messages": [{"type": "ERROR",
                                  "text": "You must specify a specific index."}]}

    monkeypatch.setattr("app.data_sources.clients.splunk_client.requests.post",
                        lambda *a, **k: FakeResp())
    monkeypatch.setattr(c, "_known_indexes", lambda: ["web", "app"])

    with pytest.raises(RuntimeError) as e:
        c._run_search("search index=* | stats count")
    msg = str(e.value)
    assert "specific index" in msg
    assert "Known indexes: web, app" in msg


# ---------- dashboards & saved searches ---------- #

_SIMPLE_XML = """
<form>
  <label>Checkout Health</label>
  <search id="base1"><query>index=web sourcetype=access_combined uri_path="/checkout"</query></search>
  <row>
    <panel>
      <title>Error rate by service</title>
      <chart>
        <search>
          <query>index=web status&gt;=500 | timechart span=5m count by host</query>
          <earliest>-4h</earliest><latest>now</latest>
        </search>
      </chart>
    </panel>
    <panel>
      <title>Checkout failures</title>
      <table>
        <search base="base1"><query>| stats count by status</query></search>
      </table>
      <input type="time"><label>ignored</label></input>
    </panel>
  </row>
</form>
"""

_STUDIO_DEF = {
    "title": "Payments Studio",
    "dataSources": {
        "ds_base": {"type": "ds.search", "name": "base",
                    "options": {"query": "index=app sourcetype=json_app service=billing",
                                "queryParameters": {"earliest": "-24h", "latest": "now"}}},
        "ds_chain": {"type": "ds.chain",
                     "options": {"extend": "ds_base", "query": "| stats avg(latency_ms) by service"}},
    },
    "visualizations": {
        "viz_1": {"title": "Billing latency", "dataSources": {"primary": "ds_chain"}},
    },
    "layout": {},
}
_STUDIO_XML = ("<dashboard version=\"2\"><label>Payments Studio</label>"
               f"<definition><![CDATA[{json.dumps(_STUDIO_DEF)}]]></definition></dashboard>")

_NOT_A_DASHBOARD = "<view template='pages/app.html'><label>Built-in page</label></view>"


def _view_entry(app, name, xml, owner="sre"):
    return {"name": name, "acl": {"app": app, "owner": owner},
            "content": {"eai:data": xml, "isVisible": True}}


_VIEWS = {"entry": [
    _view_entry("payments", "checkout_health", _SIMPLE_XML),
    _view_entry("payments", "payments_studio", _STUDIO_XML),
    _view_entry("search", "builtin_page", _NOT_A_DASHBOARD),
    _view_entry("splunk_monitoring_console", "mc_overview", _SIMPLE_XML),  # system app
]}

_SAVED = {"entry": [
    {"name": "High checkout error rate", "acl": {"app": "payments", "owner": "sre"},
     "content": {"search": 'index=web status>=500 | stats count',
                 "is_scheduled": "1", "cron_schedule": "*/10 * * * *",
                 "alert_type": "number of events",
                 "dispatch.earliest_time": "-10m",
                 "description": "Fires when checkout 5xx spikes."}},
    {"name": "Errors in the last 24 hours", "acl": {"app": "search", "owner": "nobody"},
     "content": {"search": "error"}},                       # stock content — skipped
    {"name": "instrumentation", "acl": {"app": "splunk_instrumentation", "owner": "nobody"},
     "content": {"search": "| rest ..."}},                  # system app — skipped
]}


def _knowledge_get(path, params=None):
    if "/data/ui/views" in path:
        if path.endswith("/data/ui/views"):
            return _VIEWS
        name = path.rsplit("/", 1)[1]
        matches = [e for e in _VIEWS["entry"] if e["name"] == name]
        return {"entry": matches}
    if "/saved/searches" in path:
        if path.endswith("/saved/searches"):
            return _SAVED
        name = path.rsplit("/", 1)[1].replace("%20", " ")
        return {"entry": [e for e in _SAVED["entry"] if e["name"] == name]}
    raise AssertionError(f"unexpected GET {path}")


def _knowledge_client(monkeypatch, **kw):
    c = SplunkClient(host="h", api_token="t", **kw)
    monkeypatch.setattr(c, "_get", _knowledge_get)
    return c


def test_dashboard_catalog_parses_simple_xml_and_studio(monkeypatch):
    c = _knowledge_client(monkeypatch)
    tables = {t.name: t for t in c._knowledge_tables()}

    assert set(tables) == {
        "dashboard::payments/checkout_health",
        "dashboard::payments/payments_studio",
        "saved_search::payments/High checkout error rate",
    }  # built-in page, system app, and stock saved search all filtered out

    dash = tables["dashboard::payments/checkout_health"]
    cols = {col.name: col for col in dash.columns}
    assert set(cols) == {"Error rate by service", "Checkout failures"}
    # The panel's saved SPL is the column description (how it reaches prompts).
    assert "timechart span=5m count by host" in cols["Error rate by service"].description
    # base= search resolved: base SPL + post-process.
    assert cols["Checkout failures"].description.startswith(
        'index=web sourcetype=access_combined uri_path="/checkout" | stats count by status')
    meta = dash.metadata_json["splunk"]
    assert meta["kind"] == "dashboard" and meta["app"] == "payments"
    assert meta["dashboard_type"] == "simple_xml" and meta["panel_count"] == 2
    assert meta["panels"][0]["earliest"] == "-4h"
    assert "Checkout Health" in dash.description and "execute_query" in dash.description

    studio = tables["dashboard::payments/payments_studio"]
    assert studio.metadata_json["splunk"]["dashboard_type"] == "studio"
    (panel,) = studio.metadata_json["splunk"]["panels"]
    # ds.chain resolved onto its base, queryParameters carried through.
    assert panel["spl"] == ("index=app sourcetype=json_app service=billing "
                            "| stats avg(latency_ms) by service")
    assert panel["title"] == "Billing latency" and panel["earliest"] == "-24h"


def test_saved_search_catalog(monkeypatch):
    c = _knowledge_client(monkeypatch)
    (t,) = c._saved_search_tables()
    assert t.name == "saved_search::payments/High checkout error rate"
    meta = t.metadata_json["splunk"]
    assert meta["kind"] == "alert" and meta["alert"] is True
    assert meta["cron"] == "*/10 * * * *"
    assert "index=web status>=500" in t.description       # SPL surfaced
    assert "Fires when checkout 5xx spikes." in t.description


def test_get_schemas_appends_knowledge_tables(monkeypatch):
    c = _knowledge_client(monkeypatch, max_sampled_sourcetypes=0)
    monkeypatch.setattr(c, "_run_search",
                        lambda spl, earliest=None, latest=None, count=1000:
                        _catalog_rows() if spl == CATALOG_SEARCH else [])
    names = [t.name for t in c.get_schemas()]
    assert "web::access_combined" in names
    assert "dashboard::payments/checkout_health" in names
    assert "saved_search::payments/High checkout error rate" in names


def test_knowledge_listing_failure_never_fails_discovery(monkeypatch):
    c = SplunkClient(host="h", api_token="t", max_sampled_sourcetypes=0)
    monkeypatch.setattr(c, "_run_search",
                        lambda spl, earliest=None, latest=None, count=1000:
                        _catalog_rows() if spl == CATALOG_SEARCH else [])
    def denied(path, params=None):
        raise RuntimeError("Splunk access denied (403)")
    monkeypatch.setattr(c, "_get", denied)
    names = [t.name for t in c.get_schemas()]
    assert "web::access_combined" in names                 # events still there
    assert not any(n.startswith("dashboard::") for n in names)


def test_execute_query_dashboard_inventory(monkeypatch):
    c = _knowledge_client(monkeypatch)
    df = c.execute_query({"dashboard": "payments/checkout_health"})
    assert list(df["panel"]) == ["Error rate by service", "Checkout failures"]
    assert df.iloc[0]["spl"].startswith("index=web status>=500")


def test_execute_query_dashboard_panel_with_incident_window(monkeypatch):
    c = _knowledge_client(monkeypatch)
    captured = {}
    monkeypatch.setattr(c, "_run_search",
                        lambda spl, earliest=None, latest=None, count=1000:
                        captured.update(spl=spl, earliest=earliest, latest=latest)
                        or [{"host": "h1", "count": "3"}])
    df = c.execute_query('{"dashboard": "dashboard::payments/checkout_health", '
                         '"panel": "error rate by service", "earliest": "-2h"}')
    assert captured["spl"] == "index=web status>=500 | timechart span=5m count by host"
    assert captured["earliest"] == "-2h"      # incident window overrides -4h
    assert captured["latest"] == "now"        # panel default kept
    assert list(df.columns) == ["host", "count"]


def test_execute_query_dashboard_panel_default_time_from_panel(monkeypatch):
    c = _knowledge_client(monkeypatch)
    captured = {}
    monkeypatch.setattr(c, "_run_search",
                        lambda spl, earliest=None, latest=None, count=1000:
                        captured.update(earliest=earliest) or [])
    c.execute_query({"dashboard": "payments/checkout_health", "panel": 0})
    assert captured["earliest"] == "-4h"      # panel's own earliest


def test_execute_query_dashboard_unknown_panel_lists_titles(monkeypatch):
    c = _knowledge_client(monkeypatch)
    with pytest.raises(ValueError, match="Error rate by service"):
        c.execute_query({"dashboard": "payments/checkout_health", "panel": "nope"})


def test_execute_query_saved_search(monkeypatch):
    c = _knowledge_client(monkeypatch)
    captured = {}
    monkeypatch.setattr(c, "_run_search",
                        lambda spl, earliest=None, latest=None, count=1000:
                        captured.update(spl=spl, earliest=earliest) or [])
    c.execute_query({"saved_search": "payments/High checkout error rate"})
    assert captured["spl"] == "index=web status>=500 | stats count"
    assert captured["earliest"] == "-10m"     # saved dispatch window
    captured.clear()
    c.execute_query({"saved_search": "payments/High checkout error rate",
                     "earliest": "-1h"})
    assert captured["earliest"] == "-1h"      # override wins


def test_get_schema_hydrates_dashboard_and_saved_search(monkeypatch):
    c = _knowledge_client(monkeypatch)
    t = c.get_schema("dashboard::payments/checkout_health")
    assert [col.name for col in t.columns] == ["Error rate by service", "Checkout failures"]
    s = c.get_schema("saved_search::payments/High checkout error rate")
    assert s.metadata_json["splunk"]["spl"] == "index=web status>=500 | stats count"


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
