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

    monkeypatch.setattr("app.data_sources.clients.splunk_client.pinned_request",
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


# ---------- read-only check at the search dispatch point ---------- #
#
# Every SPL search, whatever envelope it came in (bare string, dict, JSON
# string, dashboard panel, saved search) or whichever internal caller ran it,
# goes through `_run_search`. These tests leave `_run_search` and `_get` real
# and fake only the HTTP boundary, `pinned_request`, so the check is exercised
# exactly where a search would leave the process.

_WRITING_PANEL_XML = """
<dashboard>
  <label>Ops</label>
  <row><panel><title>Export</title><table>
    <search><query>index=web | stats count by host | outputlookup hosts.csv</query></search>
  </table></panel>
  <panel><title>Hosts</title><table>
    <search><query>index=web | stats count by host</query></search>
  </table></panel></row>
</dashboard>
"""

_WRITING_SAVED = {"name": "Nightly rollup", "acl": {"app": "ops", "owner": "sre"},
                  "content": {"search": "index=web | stats count | collect index=summary"}}

# Stored searches whose writing command opens a bracketed block or sits behind
# an inline comment, so it never directly follows a `|` in the raw text.
_HIDDEN_PANEL_XML = """
<dashboard>
  <label>Ops</label>
  <row><panel><title>Export</title><table>
    <search><query>index=web | stats count by host | ```c``` outputlookup hosts.csv</query></search>
  </table></panel></row>
</dashboard>
"""

_HIDDEN_SAVED = {"name": "Hidden rollup", "acl": {"app": "ops", "owner": "sre"},
                 "content": {"search": "index=web | stats count | appendpipe [collect index=summary]"}}

# Stored searches whose write shows only in a reading where a ``` inside a
# quoted value shifts which comment runs pair, or that read another stored
# search as a `from` / `union` dataset.
_SHIFTED_PANEL_XML = """
<dashboard>
  <label>Ops</label>
  <row><panel><title>Export</title><table>
    <search><query>index=web x="```" | ```c``` delete y="```"</query></search>
  </table></panel></row>
</dashboard>
"""

_DATASET_SAVED = {"name": "Dataset rollup", "acl": {"app": "ops", "owner": "sre"},
                  "content": {"search": "index=web | union savedsearch:Nightly"}}

# Stored searches whose write shows only if a macro argument holds a quoted
# backtick, or if a command name split across backtick spans rejoins under a
# quote model other than double quotes with escapes.
_MACRO_ARG_PANEL_XML = """
<dashboard>
  <label>Ops</label>
  <row><panel><title>Export</title><table>
    <search><query>index=web | `comment("`")` outputlookup hosts.csv</query></search>
  </table></panel></row>
</dashboard>
"""

_SPLIT_SAVED = {"name": "Split rollup", "acl": {"app": "ops", "owner": "sre"},
                "content": {"search": "index=web x='```' | col```c```lect index=summary y='```'"}}

# Stored searches whose write shows only if Splunk drops quotes inside a
# command name, or an invisible character before it.
_QUOTED_PANEL_XML = """
<dashboard>
  <label>Ops</label>
  <row><panel><title>Export</title><table>
    <search><query>index=web | stats count by host | out""putlookup hosts.csv</query></search>
  </table></panel></row>
</dashboard>
"""

_INVISIBLE_SAVED = {"name": "Invisible rollup", "acl": {"app": "ops", "owner": "sre"},
                    "content": {"search": "index=web | stats count | \u200bcollect index=summary"}}


class _SplunkHttp:
    """Fake `pinned_request`: answers knowledge-object GETs from canned
    definitions and records every search job POST."""

    def __init__(self):
        self.searches: list[str] = []

    def __call__(self, method, url, params=None, data=None, **kwargs):
        from urllib.parse import unquote
        path = unquote(url.split("8089", 1)[1])
        if method == "POST" and path == "/services/search/jobs":
            spl = data["search"]
            self.searches.append(spl)
            if spl == CATALOG_SEARCH:
                return _Resp(_catalog_rows())
            if "fieldsummary" in spl:
                return _Resp([{"field": "status", "count": "5", "numeric_count": "5"}])
            return _Resp([{"host": "h1", "count": "3"}])
        if method == "GET":
            if path == "/servicesNS/-/ops/data/ui/views/ops_board":
                return _Body({"entry": [_view_entry("ops", "ops_board", _WRITING_PANEL_XML)]})
            if path == "/servicesNS/-/ops/data/ui/views/hidden_board":
                return _Body({"entry": [_view_entry("ops", "hidden_board", _HIDDEN_PANEL_XML)]})
            if path == "/servicesNS/-/ops/saved/searches/Nightly rollup":
                return _Body({"entry": [_WRITING_SAVED]})
            if path == "/servicesNS/-/ops/saved/searches/Hidden rollup":
                return _Body({"entry": [_HIDDEN_SAVED]})
            if path == "/servicesNS/-/ops/data/ui/views/shifted_board":
                return _Body({"entry": [_view_entry("ops", "shifted_board", _SHIFTED_PANEL_XML)]})
            if path == "/servicesNS/-/ops/saved/searches/Dataset rollup":
                return _Body({"entry": [_DATASET_SAVED]})
            if path == "/servicesNS/-/ops/data/ui/views/macro_arg_board":
                return _Body({"entry": [_view_entry("ops", "macro_arg_board", _MACRO_ARG_PANEL_XML)]})
            if path == "/servicesNS/-/ops/saved/searches/Split rollup":
                return _Body({"entry": [_SPLIT_SAVED]})
            if path == "/servicesNS/-/ops/data/ui/views/quoted_board":
                return _Body({"entry": [_view_entry("ops", "quoted_board", _QUOTED_PANEL_XML)]})
            if path == "/servicesNS/-/ops/saved/searches/Invisible rollup":
                return _Body({"entry": [_INVISIBLE_SAVED]})
            if path == "/servicesNS/-/-/data/ui/views":
                return _Body({"entry": [_view_entry("payments", "checkout_health", _SIMPLE_XML)]})
            if path in ("/servicesNS/-/-/saved/searches", "/services/data/indexes"):
                return _Body({"entry": []})
        raise AssertionError(f"unexpected {method} {path}")


class _Body:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _Resp(results):
    return _Body({"results": results})


@pytest.fixture
def splunk_http(monkeypatch):
    http = _SplunkHttp()
    monkeypatch.setattr("app.data_sources.clients.splunk_client.pinned_request", http)
    return SplunkClient(host="h", api_token="t", max_sampled_sourcetypes=1), http


@pytest.mark.parametrize("query", [
    {"spl": "search index=web | delete"},
    '{"spl": "search index=web | \\u0064elete"}',
    '{"spl": "search index=web | \\"delete\\""}',
    '{"spl": "search index=web |\\u0000delete"}',
    '{"spl": "search index=web | \\u200bdelete"}',
    {"dashboard": "ops/ops_board", "panel": "Export"},
    {"saved_search": "ops/Nightly rollup"},
])
def test_writing_spl_is_refused_before_any_search_is_sent(splunk_http, query):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation):
        client.execute_query(query)
    assert http.searches == []


@pytest.mark.parametrize("query,expected", [
    ("index=web | stats count by host", "search index=web | stats count by host"),
    ({"spl": "index=web | stats count by host"}, "search index=web | stats count by host"),
    ({"dashboard": "ops/ops_board", "panel": "Hosts"}, "search index=web | stats count by host"),
])
def test_reading_spl_still_runs_once(splunk_http, query, expected):
    client, http = splunk_http
    df = client.execute_query(query)
    assert http.searches == [expected]
    assert list(df.columns) == ["host", "count"]


def test_discovery_searches_still_run(splunk_http):
    client, http = splunk_http
    names = [t.name for t in client.get_schemas()]
    assert "web::access_combined" in names
    assert "dashboard::payments/checkout_health" in names
    assert CATALOG_SEARCH in http.searches
    assert any("fieldsummary" in s for s in http.searches)


# Commands that write (indexes, lookups, files, inputs), alert or send data
# out, run external code, or run other searches (stored or built per row).
_REFUSED_SPL = [
    "index=web | delete",
    "index=web | outputlookup hosts.csv",
    "index=web | outputcsv hosts",
    "index=web | outputtext",
    "index=web | collect index=summary",
    "index=web | summaryindex index=summary",
    "index=web | mcollect index=metrics",
    "index=web | meventcollect index=metrics",
    "index=web | tscollect namespace=ns",
    "index=web | sendemail to=ops@example.com",
    "index=web | sendalert notify",
    "index=web | outputtelemetry input=host",
    "index=web | script myscript",
    "index=web | run myscript",
    "index=web | runshellscript myscript.sh",
    "index=web | dump basefilename=out",
    "| savedsearch Nightly",
    "index=web [| savedsearch Nightly]",
    "| makeresults | map search=\"search index=web\"",
    "| crawl",
    "| input add source=/var/log",
    "index=web | dbxoutput output=x",
    "index=web | dbxquery query=\"SELECT 1\"",
]


# The same commands where Splunk reads them but they do not follow a `|` in
# the raw text: opening a bracketed block (appendpipe subpipeline, subsearch),
# behind a triple-backtick inline comment (Splunk turns it into a space before
# parsing), plus the MLTK commands that save or delete a model.
_REFUSED_SPL += [
    "index=web | stats count | appendpipe [outputlookup hosts.csv]",
    "index=web | stats count | appendpipe [collect index=summary]",
    "index=web | stats count | appendpipe [map search=\"search index=web\"]",
    "index=web [savedsearch Nightly]",
    "index=web | appendpipe[ \n\t outputlookup hosts.csv ]",
    "index=web [\n  savedsearch Nightly ]",
    "index=web |```x```delete",
    "index=web | ```x``` outputlookup hosts.csv",
    "index=web | ```a``` ```b``` delete",
    "index=web | ```multi\nline``` collect index=summary",
    "index=web | appendpipe [```x``` outputlookup hosts.csv]",
    "index=web | stats count | ```x``` deletemodel m",
    "| deletemodel m",
    "index=web | fit LinearRegression y from x into m",
    "index=web | fit LinearRegression y from x\n into m",
    "index=web | appendpipe [fit KMeans x k=3 into m]",
]

# Readings of comments and quotes the check cannot rule out, so any of them
# showing a refused command refuses the search: a ``` inside a quoted value
# shifting which comment runs pair; a comment, or a macro that expands to
# nothing, removed with no space so it rejoins a split command name, or
# standing between the pipe and the command; and `from` / `union` reading a
# stored search as a dataset (its SPL is never in the text checked here).
_REFUSED_SPL += [
    'index=web x="```" | ```c``` delete y="```"',
    'index=web x="```" | appendpipe [```c``` outputlookup hosts.csv] y="```"',
    'index=web x="a\\"```" | ```c``` collect index=summary y="```"',
    "index=web | output```x```lookup hosts.csv",
    "index=web | col```x```lect index=summary",
    "index=web | appendpipe [out```x```putlookup hosts.csv]",
    "index=web | f```x```it LinearRegression y from x into m",
    'index=web | `comment("x")` delete',
    'index=web | appendpipe [`comment("x")` outputlookup hosts.csv]',
    'index=web | out`comment("x")`putlookup hosts.csv',
    'index=web | out`comment("x")`put```y```lookup hosts.csv',
    "| from savedsearch:Nightly",
    'index=web | union savedsearch:"Nightly rollup"',
    "index=web [| from savedsearch:Nightly]",
    "index=web | appendpipe [union savedsearch : Nightly]",
    "| FROM SavedSearch:Nightly",
    "| from saved```x```search:Nightly",
    # A run of more than three backticks pairs more than one way.
    "index=web | ``````` delete",
    "index=web | ````x```` outputlookup hosts.csv",
]

# The verdict must not depend on how backticks and quotes pair. A macro
# argument can hold a quoted or escaped backtick, so a macro need not end at
# the next backtick: a listed name right after any backtick is refused. And a
# name split across backtick spans is refused however the spans pair, under
# single quotes, an escaped quote outside a quoted value, or double quotes
# read without escapes.
_REFUSED_SPL += [
    'index=web | `comment("`")` delete',
    "index=web | `comment('`')` delete",
    'index=web | `comment("\\`")` delete',
    "index=web | `comment(\\`)` outputlookup hosts.csv",
    'index=web | appendpipe [`comment("`")` collect index=summary]',
    'index=web | `comment("``")` savedsearch Nightly',
    "index=web x='```' | out```c```putlookup hosts.csv y='```'",
    "index=web x='```' | appendpipe [out```c```putlookup hosts.csv] y='```'",
    'index=web a=\\" b="```" | out```c```putlookup hosts.csv z="```"',
    'index=web x="a\\" ```c``` y="```" | out```c```putlookup hosts.csv z="```"',
    'index=web x="a\\" | ```" | col```c```lect index=summary y="```"',
    'index=web | out`comment("`")`putlookup hosts.csv',
    "index=web | out`comment('`')`putlookup hosts.csv",
    "index=web | out```x`y`z```putlookup hosts.csv",
    "index=web | o`a`u`b`t```c```putlookup hosts.csv",
    "index=web | OUT```c```PutLookup hosts.csv",
    'index=web | from saved`comment("`")`search:Nightly',
    "index=web | union saved`x`search:Nightly",
    "index=web | from savedsearch`x`:Nightly",
    "index=web | from savedsearch ```x``` :Nightly",
    'index=web | f`comment("`")`it LinearRegression y from x into m',
    "index=web | `m` fit LinearRegression y from x into m",
    # The `into` that saves the model can be split across backticks too.
    "index=web | fit LinearRegression y from x in```c```to m",
    'index=web | fit LinearRegression y from x in`comment("`")`to m',
    "index=web | f```c```it LinearRegression y from x I`m`NTO m",
]

# Splunk may drop quotes and backslashes inside or before a command name, or
# end a name at a letter outside ASCII, so those readings refuse too; and a
# control or zero-width character, which Splunk may drop to join the letters
# around it, is refused anywhere.
_REFUSED_SPL += [
    'index=web | "delete"',
    "index=web | 'outputlookup' x.csv",
    'index=web | out""putlookup x.csv',
    "index=web | out\\putlookup x.csv",
    "index=web | \\delete",
    'index=web | appendpipe ["collect" index=summary]',
    "index=web | `m` 'delete'",
    '| from "savedsearch":Nightly',
    '| from savedsearch":Nightly"',
    "| from savedsearch\\:Nightly",
    '| from saved""search:Nightly',
    "| from saved'`m`'search:Nightly",
    "index=web |\x00delete",
    "index=web | del\x1fete",
    "index=web |\u200bdelete",
    "index=web | out\u200cputlookup x.csv",
    "index=web | \u200dcollect index=summary",
    "index=web | \u2060delete",
    "index=web | \ufeffdelete",
    "index=web | deleteé",
    # A letter outside ASCII that case-folds to an ASCII one (Kelvin sign).
    "index=web | delete\u212a",
    "index=web | fité LinearRegression y from x into m",
    'index=web | fit LinearRegression y from x in""to m',
    # A comment after a pipe can still hide a command, whatever it says.
    "index=web | ```Run hourly``` stats count",
]

# A macro before the first pipe or bracket can expand to text that ends in a
# pipe or a bracket, so a listed name after the backtick that ends it is
# refused there too.
_REFUSED_SPL += [
    "`m` delete",
    "`m` outputlookup x.csv",
    "`m(1)`collect index=summary",
    "index=web `m` delete",
    '`m` from saved""search:Nightly',
    "`m` fit LinearRegression y from x into m",
    "index=web `m` out`n`putlookup x.csv",
    "```note``` `m` delete",
]

# A letter outside ASCII that case-folds to an ASCII one counts as that letter
# when a name is spelled across backticks, as it does in an unsplit name; and
# a name may still end, or a word start, at it.
_REFUSED_SPL += [
    "index=web | outputloo\u212a`m`up x.csv",
    "index=web | \u017fcr`m`ipt x",
    "index=web | scr\u0131`m`pt x",
    "index=web | \u0130np`m`ut x",
    "index=web | del`m`ete\u212a x",
    "index=web | fit LinearRegression y from x \u212ain`m`to m",
    "index=web | \u017fsavedsearch Nightly",
    "index=web [\u212aoutputlookup x.csv",
    "`m`\u0130map search=x",
    "index=web | \u0131run x",
    "index=web | \u212a out`m`putlookup x.csv",
    "index=web | from savedsearch\u212a:Nightly",
    "index=web | \u212afit LinearRegression y from x into m",
]


@pytest.mark.parametrize("spl", [
    "index=web | stats count | ```unterminated",
    "index=web ```a``` | ```b",
])
def test_spl_with_an_unterminated_inline_comment_is_refused(splunk_http, spl):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation):
        client.execute_query(spl)
    with pytest.raises(ReadOnlyViolation):
        client.execute_query({"spl": spl})
    assert http.searches == []


@pytest.mark.parametrize("query", [
    {"dashboard": "ops/hidden_board", "panel": "Export"},
    {"saved_search": "ops/Hidden rollup"},
    {"dashboard": "ops/shifted_board", "panel": "Export"},
    {"saved_search": "ops/Dataset rollup"},
    {"dashboard": "ops/macro_arg_board", "panel": "Export"},
    {"saved_search": "ops/Split rollup"},
    {"dashboard": "ops/quoted_board", "panel": "Export"},
    {"saved_search": "ops/Invisible rollup"},
])
def test_stored_search_hiding_a_write_is_refused_before_it_is_sent(splunk_http, query):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation):
        client.execute_query(query)
    assert http.searches == []


@pytest.mark.parametrize("spl", _REFUSED_SPL)
def test_spl_command_that_writes_or_runs_other_searches_is_refused(splunk_http, spl):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation):
        client.execute_query(spl)
    with pytest.raises(ReadOnlyViolation):
        client.execute_query({"spl": spl})
    assert http.searches == []


@pytest.mark.parametrize("spl,expected", [
    ("index=web | stats count by host", "search index=web | stats count by host"),
    ("index=web | table host count", "search index=web | table host count"),
    ("search index=web status=500", "search index=web status=500"),
    ("index=web runtime>5 | inputlookup hosts.csv", "search index=web runtime>5 | inputlookup hosts.csv"),
    ("index=web ```note``` | stats count", "search index=web ```note``` | stats count"),
    ("index=web | ```note``` stats count", "search index=web | ```note``` stats count"),
    ("index=web | `top_hosts` | stats count", "search index=web | `top_hosts` | stats count"),
    ("index=web | stats count | appendpipe [stats sum(count) as total]",
     "search index=web | stats count | appendpipe [stats sum(count) as total]"),
    ("index=web [search index=hosts | fields host]", "search index=web [search index=hosts | fields host]"),
    ("index=web | fit LinearRegression y from x", "search index=web | fit LinearRegression y from x"),
    ("index=web | apply m", "search index=web | apply m"),
    ('index=web msg="```code```" | stats count', 'search index=web msg="```code```" | stats count'),
    ('index=web | `comment("note")` stats count', 'search index=web | `comment("note")` stats count'),
    ("| from datamodel:Web", "| from datamodel:Web"),
    ("| from inputlookup:hosts.csv", "| from inputlookup:hosts.csv"),
    ("index=web | loadjob savedsearch=\"sre:ops:Nightly\"", "search index=web | loadjob savedsearch=\"sre:ops:Nightly\""),
    ('index=web | `top_hosts("web", 10)` | stats count', 'search index=web | `top_hosts("web", 10)` | stats count'),
    ("index=web | `run_report` | `map_hosts(web)` | stats count",
     "search index=web | `run_report` | `map_hosts(web)` | stats count"),
    ("index=web ```output of the nightly job``` | stats count",
     "search index=web ```output of the nightly job``` | stats count"),
    ("| loadjob savedsearch=\"sre:ops:Nightly\" | `comment(\"x\")` table host",
     "| loadjob savedsearch=\"sre:ops:Nightly\" | `comment(\"x\")` table host"),
    # Quoted values holding listed words, away from where a command starts.
    ('search msg="delete requested" | stats count', 'search msg="delete requested" | stats count'),
    ("index=web user=\"o'brien\" action='run' | stats count",
     "search index=web user=\"o'brien\" action='run' | stats count"),
    ('index=web path="C:\\jobs\\run\\out.log" | stats count',
     'search index=web path="C:\\jobs\\run\\out.log" | stats count'),
    ('index=web | rex field=_raw "id=(?<run_id>\\d+)" | stats count by run_id',
     'search index=web | rex field=_raw "id=(?<run_id>\\d+)" | stats count by run_id'),
    ('index=web city="Zürich" | where status="deleted" | stats count',
     'search index=web city="Zürich" | where status="deleted" | stats count'),
    ("index=web\n| stats count\tby host", "search index=web\n| stats count\tby host"),
    # A comment or macro before the first pipe cannot start a command, so its
    # first word may be a listed name.
    ("```Run hourly``` | stats count", "search ```Run hourly``` | stats count"),
    ("```delete old rows first``` index=web | stats count",
     "search ```delete old rows first``` index=web | stats count"),
    ("`collect(web)` | stats count", "search `collect(web)` | stats count"),
    ("```out```put```lookup``` index=web | stats count",
     "search ```out```put```lookup``` index=web | stats count"),
    ("index=web ```Run hourly``` | stats count", "search index=web ```Run hourly``` | stats count"),
    # A quote or backslash joins only the letters right next to it, and the
    # dataset word counts only before a colon.
    ('index=web | where info="saved" | search action="search" | stats count',
     'search index=web | where info="saved" | search action="search" | stats count'),
    ('index=web | search path="C:\\logs\\saved\\search.log" | stats count',
     'search index=web | search path="C:\\logs\\saved\\search.log" | stats count'),
    ('index=web | search path="C:\\saved\\jobs\\search.log" | stats count',
     'search index=web | search path="C:\\saved\\jobs\\search.log" | stats count'),
    ('index=web | fit LinearRegression y from x | where dir="in" OR dir="to"',
     'search index=web | fit LinearRegression y from x | where dir="in" OR dir="to"'),
    # Ordinary reads after a pipe: apostrophes, letters outside ASCII, rex,
    # and listed words as field values.
    ("index=web | search msg=\"don't run it\" user=\"o'brien\" | stats count",
     "search index=web | search msg=\"don't run it\" user=\"o'brien\" | stats count"),
    ('index=web | search city="Zürich" note="délété" | stats count',
     'search index=web | search city="Zürich" note="délété" | stats count'),
    ('index=web | rex field=_raw "user=(?<user>\\w+) action=(?<action>\\w+)" | stats count by user',
     'search index=web | rex field=_raw "user=(?<user>\\w+) action=(?<action>\\w+)" | stats count by user'),
    ('| tstats count where index=web sourcetype="collect" by host',
     '| tstats count where index=web sourcetype="collect" by host'),
    ('index=web | search name="\u0130stanbul" | stats count',
     'search index=web | search name="\u0130stanbul" | stats count'),
])
def test_read_spl_still_posts_exactly_one_search(splunk_http, spl, expected):
    client, http = splunk_http
    client.execute_query(spl)
    assert http.searches == [expected]
    client.execute_query({"spl": spl})
    assert http.searches == [expected, expected]


def test_refused_word_inside_a_quoted_value_refusal_says_how_to_match_it(splunk_http):
    # A listed word after a `[` is refused even inside a quoted value (the
    # check does not trust quotes); the message says how to search for it.
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation) as refused:
        client.execute_query('index=web msg="[run] started" | stats count')
    message = str(refused.value)
    assert "'run'" in message
    assert "quoted value" in message
    assert '"*run"' in message and '"[run"' in message
    assert http.searches == []


@pytest.mark.parametrize("spl,shown", [
    pytest.param('index=web msg="```" | stats count', "```", id="open-comment-mark"),
    pytest.param('index=web msg="savedsearch:Nightly" | stats count', "savedsearch:", id="dataset-word"),
    pytest.param('index=web | where msg="`delete`" | stats count', "`delete", id="name-after-backtick"),
])
def test_refused_text_inside_a_quoted_value_refusal_says_how_to_match_it(splunk_http, spl, shown):
    # The check does not trust quotes, so these refuse even when they sit
    # inside a quoted value; the message names the text and the wildcard way
    # to search for it.
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation) as refused:
        client.execute_query(spl)
    message = str(refused.value)
    assert shown in message
    assert "quoted value" in message and "wildcard" in message
    assert http.searches == []


@pytest.mark.parametrize("spl,shown", [
    pytest.param('index=web | eval x=`m` "delete me" | stats count', '` "delete', id="quote-between"),
    pytest.param("index=web | \\collect index=summary", "| \\collect", id="backslash-between"),
])
def test_refusal_names_the_text_that_was_matched(splunk_http, spl, shown):
    # The message quotes the text as written, with whatever stood between the
    # mark and the name, and says how to write a value that starts with it.
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation) as refused:
        client.execute_query(spl)
    message = str(refused.value)
    assert shown in message
    assert "two quoted strings" in message
    assert http.searches == []


# Every code point Splunk might drop and so join the letters around it:
# Unicode control characters (Cc) other than tab, newline and carriage return,
# format characters (Cf), and the Default_Ignorable_Code_Point ranges of
# DerivedCoreProperties.txt.
_DEFAULT_IGNORABLE = [
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
]


def _invisible_groups():
    import unicodedata

    groups = {"Cc": [], "Cf": []}
    for code in range(0x110000):
        category = unicodedata.category(chr(code))
        if category in groups and chr(code) not in "\t\n\r":
            groups[category].append(code)
    yield pytest.param(groups["Cc"], id="Cc")
    yield pytest.param(groups["Cf"], id="Cf")
    for low, high in _DEFAULT_IGNORABLE:
        yield pytest.param(list(range(low, high + 1)), id=f"DI-{low:04X}")


@pytest.mark.parametrize("codes", list(_invisible_groups()))
def test_invisible_character_inside_a_command_name_is_refused(splunk_http, codes):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    passed = []
    for code in codes:
        try:
            client.execute_query(f"index=web | del{chr(code)}ete")
            passed.append(f"U+{code:04X}")
        except ReadOnlyViolation:
            pass
    assert passed == []
    assert http.searches == []


# ---------- the check runs in linear time ---------- #
#
# Python's `re` holds the GIL while it matches, so a check that grows
# quadratically with the search would stall every request on the worker.
# About 1 MB of each shape below must be checked in well under a second, by
# everything the query wrapper runs on it. The default head has a pipe, so the
# rules that start at the first pipe or bracket scan the whole shape.

_MB = 1 << 20


def _about_1mb(piece: str, head: str = "index=web | ", tail: str = "") -> str:
    return head + piece * (_MB // len(piece)) + tail


@pytest.mark.parametrize("spl", [
    pytest.param(_about_1mb("| fit a "), id="fit-after-pipes"),
    pytest.param(_about_1mb(" ```c``` ", head="index=web |"), id="comments-after-one-pipe"),
    pytest.param(_about_1mb("| ```c``` ```d``` "), id="pipe-two-comments"),
    pytest.param(_about_1mb("| fit a ```c``` "), id="fit-and-comment"),
    pytest.param(_about_1mb("\\\"", head="index=web x=\""), id="escaped-quotes"),
    pytest.param(_about_1mb("[ "), id="open-brackets"),
    pytest.param(_about_1mb("`a"), id="backtick-letter"),
    pytest.param(_about_1mb("```| ", head="index=web "), id="comment-mark-pipe"),
    pytest.param(_about_1mb("savedsearch ", head="index=web "), id="savedsearch-words"),
    pytest.param(_about_1mb('"```" | '), id="quoted-comment-mark"),
    pytest.param(_about_1mb("| ` "), id="pipe-lone-backtick"),
    pytest.param(_about_1mb("|```|``` ", tail="`"), id="pipes-in-comments"),
    pytest.param(_about_1mb("`a`", head="index=web | "), id="macro-run"),
    # Markers each followed by a run of four backticks (the comment marks all
    # close, so the whole text is scanned), and long runs of one marker
    # between backtick runs.
    pytest.param(_about_1mb("|````", tail="```"), id="pipe-4-backticks"),
    pytest.param(_about_1mb("[````", tail="```"), id="bracket-4-backticks"),
    pytest.param(_about_1mb("savedsearch````", head="index=web ", tail="```"), id="savedsearch-4-backticks"),
    pytest.param(_about_1mb("| ````x"), id="pipe-4-backticks-letter"),
    pytest.param(_about_1mb("|", head="index=web ````", tail="````"), id="pipes-between-4-backtick-runs"),
    pytest.param(_about_1mb("[", head="index=web ````", tail="````"), id="brackets-between-4-backtick-runs"),
    pytest.param(_about_1mb("savedsearch", head="index=web ````", tail="````"),
                 id="savedsearch-between-4-backtick-runs"),
    # Pieces of command names glued to backticks.
    pytest.param(_about_1mb("out`x`", head="index=web | "), id="name-piece-macros"),
    pytest.param(_about_1mb("| out`"), id="name-prefix-per-pipe"),
    pytest.param(_about_1mb("| o`u`"), id="short-name-pieces"),
    pytest.param(_about_1mb("saved`"), id="dataset-prefix-pieces"),
    pytest.param(_about_1mb("`` ", head="index=web | out"), id="prefix-then-backtick-pairs"),
    pytest.param(_about_1mb('| out`comment("`")`'), id="macro-argument-backticks"),
    pytest.param(_about_1mb("a", head="index=web | o`"), id="long-word-after-prefix"),
    # One letter that is a piece of many checked words, glued to backticks.
    pytest.param(_about_1mb("`t"), id="backtick-t"),
    pytest.param(_about_1mb("t`"), id="t-backtick"),
    pytest.param(_about_1mb("`e"), id="backtick-e"),
    # The same shapes glued to quotes and backslashes, and those characters
    # between a mark or the dataset word and what follows.
    pytest.param(_about_1mb('"t'), id="quote-t"),
    pytest.param(_about_1mb("t'"), id="t-single-quote"),
    pytest.param(_about_1mb("\\e"), id="backslash-e"),
    pytest.param(_about_1mb('| "'), id="pipe-quote"),
    pytest.param(_about_1mb("| \\"), id="pipe-backslash"),
    pytest.param(_about_1mb('| out"'), id="name-prefix-quote-per-pipe"),
    pytest.param(_about_1mb('| o"u"'), id="short-name-pieces-quoted"),
    pytest.param(_about_1mb('saved"'), id="dataset-prefix-quoted"),
    pytest.param(_about_1mb('savedsearch" ', head="index=web | x "), id="dataset-word-quote"),
    pytest.param(_about_1mb('" '), id="quote-runs"),
    # Backticks before the first pipe, where the command rules also look.
    pytest.param(_about_1mb("`m` ", head="index=web "), id="macros-before-any-pipe"),
    pytest.param(_about_1mb("`t", head="index=web "), id="backtick-t-before-any-pipe"),
    pytest.param(_about_1mb("```c``` ", head="index=web "), id="comments-before-any-pipe"),
    pytest.param(_about_1mb("`m` fit a ", head="index=web "), id="fit-after-macros-before-any-pipe"),
    # Quote runs between pieces, and folded letters in pieces.
    pytest.param(_about_1mb('"t"'), id="quoted-t-runs"),
    pytest.param(_about_1mb('| o"u" '), id="pipe-quoted-pieces"),
    pytest.param(_about_1mb('saved" "'), id="dataset-prefix-quote-runs"),
    pytest.param(_about_1mb("\u212a`"), id="kelvin-backtick"),
    pytest.param(_about_1mb("| o\u212a`"), id="pipe-kelvin-pieces"),
    pytest.param(_about_1mb("in`\u212a"), id="into-prefix-kelvin"),
    # The same shapes inside a JSON-string envelope, escaped as JSON.
    pytest.param('{"spl": "index=web | ' + '| o\\"u\\" ' * (_MB // 10) + '"}', id="json-quoted-pieces"),
    pytest.param('{"spl": "index=web ' + "\\u0060m\\u0060 " * (_MB // 14) + '"}', id="json-escaped-macros"),
    pytest.param('{"spl": "index=web | ' + "\\\\e" * (_MB // 4) + '"}', id="json-backslash-e"),
    # Many name prefixes started first, then pieces that can continue them.
    pytest.param(_about_1mb("`t `e `l `o `u `a `s ", head="index=web " + "".join(
        f"| {name[:i]}` " for name in ("outputlookup", "collect", "sendemail", "deletemodel", "tscollect")
        for i in range(1, len(name)))), id="many-live-prefixes"),
])
def test_check_spl_is_linear_on_large_input(spl):
    import time

    from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only

    client = SplunkClient(host="h", api_token="t")
    start = time.perf_counter()
    try:
        ensure_read_only(client, spl)
    except ReadOnlyViolation:
        pass
    assert time.perf_counter() - start < 1.0


# ---------- envelopes the wrapper cannot read are refusals ---------- #
#
# Through the query wrapper the sandbox uses: an envelope that cannot be
# decoded or captured is refused like any other refused query (UnsafeSQLError),
# the attempted text is captured, and nothing reaches Splunk.

def _wrapped(client):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    captured: list = []
    return QueryCapturingClientWrapper(client, captured, [], client_key="splunk:1"), captured


def test_too_deeply_nested_json_envelope_is_a_refusal_with_capture(splunk_http):
    from app.ai.code_execution.code_execution import UnsafeSQLError

    client, http = splunk_http
    envelope = '{"spl": ' + "[" * 200_000 + "]" * 200_000 + "}"
    wrapper, captured = _wrapped(client)
    with pytest.raises(UnsafeSQLError) as refused:
        wrapper.execute_query(envelope)
    assert '{"spl":' in str(refused.value)
    assert [str(q) for q in captured] == [envelope]
    assert http.searches == []


def test_too_deeply_nested_dict_envelope_is_a_refusal_with_capture(splunk_http):
    # Too deep for json.dumps or repr: the capture is a fixed description,
    # and the guard refuses the envelope (its SPL is not text).
    from app.ai.code_execution.code_execution import UnsafeSQLError

    client, http = splunk_http
    deep: dict = {}
    node = deep
    for _ in range(100_000):
        node["a"] = {}
        node = node["a"]
    wrapper, captured = _wrapped(client)
    with pytest.raises(UnsafeSQLError) as refused:
        wrapper.execute_query({"spl": deep})
    assert '{"spl":' in str(refused.value)
    assert [str(q) for q in captured] == ["<dict envelope that cannot be shown as text>"]
    assert http.searches == []


def test_envelope_with_non_text_keys_is_a_refusal_with_capture(splunk_http):
    from app.ai.code_execution.code_execution import UnsafeSQLError

    client, http = splunk_http
    wrapper, captured = _wrapped(client)
    with pytest.raises(UnsafeSQLError):
        wrapper.execute_query({"spl": "index=web | stats count", 1: "x"})
    assert len(captured) == 1 and "index=web | stats count" in str(captured[0])
    assert http.searches == []


@pytest.mark.parametrize("query", [
    {"spl": ["search index=web"]},
    {"spl": 42},
    '{"spl": {"search": "index=web"}}',
])
def test_non_text_spl_in_an_envelope_is_refused_with_envelope_guidance(splunk_http, query):
    from app.ai.code_execution.sql_guard import ReadOnlyViolation

    client, http = splunk_http
    with pytest.raises(ReadOnlyViolation) as refused:
        client.execute_query(query)
    assert '{"spl":' in str(refused.value)
    assert http.searches == []


# ---------- refused searches are not metered ---------- #
#
# The query wrapper runs the rate limit and the data-query quota before it
# sends. A Splunk envelope the guard can read (dict or JSON string) is checked
# before that, so its refusal costs nothing. A dashboard panel or saved search
# is only known after the client fetches it; a refusal there gives back the
# data-query usage event (the fetch did reach Splunk, so the rate-limit request
# stays counted).

async def _metered_splunk(http_client):
    import uuid

    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.organization import Organization
    from app.models.user import User

    async with async_session_maker() as db:
        org = Organization(name=f"Org-{uuid.uuid4().hex[:8]}")
        db.add(org)
        await db.flush()
        user = User(name="U", email=f"spl-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x",
                    is_active=True, is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        conn = Connection(organization_id=org.id, name="Splunk", type="splunk", config={},
                          credentials=None, rate_limit_enabled=True, rate_limit_per_minute=100)
        db.add(conn)
        await db.commit()
        org_id, user_id, conn_id = str(org.id), str(user.id), str(conn.id)
    http_client._bow_connection_id = conn_id
    return org_id, user_id, conn_id


async def _usage_and_limiter(org_id, conn_id):
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.connection_rate_limit_counter import ConnectionRateLimitCounter
    from app.models.usage_policy import UsageEvent
    from app.services.usage_policy_service import METRIC_DATA_QUERIES

    async with async_session_maker() as db:
        events = (await db.execute(select(UsageEvent).where(
            UsageEvent.organization_id == org_id, UsageEvent.metric == METRIC_DATA_QUERIES,
        ))).scalars().all()
        counters = (await db.execute(select(ConnectionRateLimitCounter).where(
            ConnectionRateLimitCounter.connection_id == conn_id,
        ))).scalars().all()
    return len(events), sum(c.count for c in counters)


@pytest.fixture
def metering_on(monkeypatch):
    import app.services.usage_policy_service as ups
    from app.ee import license as ee_license

    monkeypatch.setattr(ups, "has_feature", lambda name: True)
    monkeypatch.setattr(ee_license, "has_feature", lambda name: True)


async def _run_metered(client, query):
    import asyncio

    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper
    from app.dependencies import async_session_maker
    from app.services.usage_policy_service import UsageLimitContext

    org_id, user_id, conn_id = await _metered_splunk(client)
    ctx = UsageLimitContext(organization_id=org_id, user_id=user_id, source="agent",
                            source_ref_id="run-1", session_maker=async_session_maker)
    ctx.loop = asyncio.get_running_loop()
    wrapper = QueryCapturingClientWrapper(client, [], [], usage_context=ctx, client_key="splunk:1")
    outcome = None
    try:
        await asyncio.to_thread(wrapper.execute_query, query)
    except Exception as e:
        outcome = e
    await ctx.flush()
    return outcome, await _usage_and_limiter(org_id, conn_id)


@pytest.mark.db
@pytest.mark.asyncio
async def test_read_search_is_metered_once(splunk_http, metering_on):
    client, http = splunk_http
    outcome, (events, requests) = await _run_metered(client, {"spl": "index=web | stats count by host"})
    assert outcome is None
    assert http.searches == ["search index=web | stats count by host"]
    assert (events, requests) == (1, 1)


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    {"spl": "search index=web | delete"},
    '{"spl": "search index=web | \\u0064elete"}',
    {"spl": ["search index=web"]},
])
async def test_refused_envelope_costs_no_usage_and_no_rate_limit(splunk_http, metering_on, query):
    from app.ai.code_execution.code_execution import UnsafeSQLError

    client, http = splunk_http
    outcome, (events, requests) = await _run_metered(client, query)
    assert isinstance(outcome, UnsafeSQLError), outcome
    assert http.searches == []
    assert (events, requests) == (0, 0)


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    {"saved_search": "ops/Nightly rollup"},
    {"dashboard": "ops/ops_board", "panel": "Export"},
])
async def test_refused_stored_search_leaves_no_usage_event(splunk_http, metering_on, query):
    from app.ai.code_execution.code_execution import UnsafeSQLError

    client, http = splunk_http
    outcome, (events, requests) = await _run_metered(client, query)
    assert isinstance(outcome, UnsafeSQLError), outcome
    assert http.searches == []
    assert (events, requests) == (0, 1)
