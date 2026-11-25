"""Column usage extraction from executed queries.

Contract: given a query and the agent's catalog, report exactly the catalog
columns the query references, per catalog table, from every clause; never
report columns that are not in the catalog, and never treat ``SELECT *`` as
using every column.
"""
import json

import pytest

from app.utils.column_extraction import (
    CatalogTable,
    extract_columns,
    extract_dsl_columns,
    extract_sql_columns,
    map_dsl_field,
)


def _catalog(prefix: str = "public"):
    return [
        CatalogTable("orders", f"{prefix}.orders", ["order_id", "account_id", "amount", "status", "ordered_at", "region_code"]),
        CatalogTable("accounts", f"{prefix}.accounts", ["account_id", "plan_tier", "region_code", "created_at"]),
        CatalogTable("events", f"{prefix}.events", ["event_id", "account_id", "event_kind", "event_time", "value_num"]),
    ]


def _cols(result):
    return {k: sorted(v) for k, v in result.items()}


SQL_CASES = [
    pytest.param(
        "SELECT a.plan_tier, SUM(o.amount) FROM orders o JOIN accounts a ON o.account_id = a.account_id "
        "WHERE o.status = 'paid' GROUP BY a.plan_tier HAVING SUM(o.amount) > 10 ORDER BY 2",
        {"orders": ["account_id", "amount", "status"], "accounts": ["account_id", "plan_tier"]},
        id="aliases-join-filter-group-having",
    ),
    pytest.param(
        # Unprefixed names resolve through the schema: plan_tier only exists on accounts.
        "SELECT plan_tier, COUNT(*) FROM orders JOIN accounts USING (account_id) WHERE amount > 5 GROUP BY plan_tier",
        {"orders": ["account_id", "amount"], "accounts": ["account_id", "plan_tier"]},
        id="unprefixed-and-using",
    ),
    pytest.param(
        "WITH recent AS (SELECT account_id, amount FROM orders WHERE ordered_at > '2026-01-01') "
        "SELECT r.account_id, SUM(r.amount) AS total FROM recent r GROUP BY r.account_id ORDER BY total DESC",
        {"orders": ["account_id", "amount", "ordered_at"]},
        id="cte-columns-attributed-to-base-table",
    ),
    pytest.param(
        "SELECT t.account_id FROM (SELECT account_id, SUM(amount) AS amt FROM orders GROUP BY account_id) t WHERE t.amt > 3",
        {"orders": ["account_id", "amount"]},
        id="derived-table",
    ),
    pytest.param(
        "SELECT region_code FROM accounts a WHERE EXISTS "
        "(SELECT 1 FROM events e WHERE e.account_id = a.account_id AND e.event_kind = 'click')",
        {"accounts": ["account_id", "region_code"], "events": ["account_id", "event_kind"]},
        id="correlated-subquery",
    ),
    pytest.param(
        "SELECT SUM(value_num) OVER (PARTITION BY event_kind ORDER BY event_time) FROM events",
        {"events": ["event_kind", "event_time", "value_num"]},
        id="window-clause",
    ),
    pytest.param("SELECT * FROM orders", {}, id="select-star-uses-no-column"),
    pytest.param(
        # region_code exists on both joined tables: ambiguous, so not attributed.
        "SELECT region_code, o.amount FROM orders o JOIN accounts a ON o.account_id = a.account_id",
        {"orders": ["account_id", "amount"], "accounts": ["account_id"]},
        id="ambiguous-unprefixed-skipped",
    ),
    pytest.param(
        "SELECT o.amount AS revenue, o.not_a_column FROM orders o ORDER BY revenue",
        {"orders": ["amount"]},
        id="aliases-and-unknown-columns-ignored",
    ),
]


@pytest.mark.parametrize("sql,expected", SQL_CASES)
@pytest.mark.parametrize("dialect", ["postgres", "tsql", "snowflake", None])
def test_sql_extracts_every_referenced_column(sql, expected, dialect):
    assert _cols(extract_sql_columns(sql, dialect, _catalog())) == expected


@pytest.mark.parametrize("sql", [
    "SELECT TOP 5 o.[amount], o.[status] FROM [public].[orders] AS o WHERE o.[region_code] = 'north' ORDER BY o.[amount] DESC",
    "SELECT TOP (5) amount, status FROM dbo.orders WITH (NOLOCK) WHERE region_code = N'north' ORDER BY amount DESC",
])
def test_sql_server_brackets_top_and_hints(sql):
    cat = [CatalogTable("orders", "dbo.orders", ["order_id", "amount", "status", "region_code"])]
    if "[public]" in sql:
        cat = [CatalogTable("orders", "public.orders", ["order_id", "amount", "status", "region_code"])]
    assert _cols(extract_sql_columns(sql, "tsql", cat)) == {"orders": ["amount", "region_code", "status"]}


def test_sql_matches_identifiers_case_insensitively_and_returns_catalog_spelling():
    cat = [CatalogTable("m", "dbo.Metrics", ["Row_Id", "HostName", "CpuPct", "EventTime"])]
    got = extract_sql_columns("SELECT hostname, AVG(cpupct) FROM DBO.METRICS WHERE eventtime > '2026-01-01' GROUP BY hostname", "tsql", cat)
    assert _cols(got) == {"m": ["CpuPct", "EventTime", "HostName"]}


def test_sql_qualified_names_resolve_by_trailing_parts():
    cat = _catalog("analytics")
    for sql in ("SELECT amount FROM orders", "SELECT amount FROM analytics.orders", "SELECT amount FROM wh.analytics.orders"):
        assert _cols(extract_sql_columns(sql, "postgres", cat)) == {"orders": ["amount"]}, sql


def test_sql_tables_outside_the_catalog_contribute_nothing():
    got = extract_sql_columns("SELECT x.amount, y.secret FROM orders x JOIN other_db.hidden y ON x.order_id = y.id", "postgres", _catalog())
    assert _cols(got) == {"orders": ["amount", "order_id"]}


@pytest.mark.parametrize("bad", ["", "SELEC amount FRM", "not sql at all ((("])
def test_sql_unparseable_input_yields_nothing(bad):
    assert extract_sql_columns(bad, "postgres", _catalog()) == {}


# ------------------------------------------------------------- Elastic DSL
def _es_catalog():
    return [
        CatalogTable("wide", "svc-events-wide", [
            "@timestamp", "event.outcome", "event.duration_ms", "message", "message.keyword",
            "items", "items[].sku", "items[].qty", "host.os.name", "svc.request.tenant_tier",
            "svc.request.tenant_tier.keyword", "user.id",
        ]),
        CatalogTable("logs", "obs-logs-*", ["@timestamp", "log.level", "trace.id"]),
    ]


def test_dsl_walks_query_aggs_sort_source_fields_and_scripts():
    env = {
        "index": "svc-events-wide",
        "query": {"bool": {
            "filter": [{"range": {"@timestamp": {"gte": "now-7d"}}}, {"term": {"event.outcome": "failure"}}],
            "must": [{"nested": {"path": "items", "query": {"term": {"items.sku": "s-1"}}}}],
            "must_not": [{"exists": {"field": "user.id"}}],
        }},
        "aggs": {"tiers": {"terms": {"field": "svc.request.tenant_tier.keyword"},
                           "aggs": {"p95": {"percentiles": {"field": "event.duration_ms"}}}}},
        "sort": [{"@timestamp": "desc"}, "_score"],
        "_source": ["message"],
        "fields": [{"field": "host.os.name"}],
        "runtime_mappings": {"qty_x2": {"type": "long", "script": {"source": "emit(doc['items.qty'].value * 2)"}}},
    }
    got = _cols(extract_dsl_columns(env, _es_catalog()))
    assert got == {"wide": sorted([
        "@timestamp", "event.outcome", "items", "items[].sku", "user.id", "svc.request.tenant_tier",
        "event.duration_ms", "message", "host.os.name", "items[].qty",
    ])}


def test_dsl_keyword_multi_field_maps_back_to_its_field():
    t = _es_catalog()[0]
    assert map_dsl_field(t, "message.keyword") == "message"
    assert map_dsl_field(t, "svc.request.tenant_tier.keyword") == "svc.request.tenant_tier"
    assert map_dsl_field(t, "items.sku") == "items[].sku"
    assert map_dsl_field(t, "not.a.field") is None


def test_dsl_index_patterns_and_concrete_indices_resolve_to_catalog_tables():
    cat = _es_catalog()
    for index in ("obs-logs-*", "obs-logs-2026.11.20", "obs-*"):
        got = extract_dsl_columns({"index": index, "query": {"match": {"log.level": "error"}}}, cat)
        assert _cols(got) == {"logs": ["log.level"]}, index


def test_dsl_runtime_fields_and_wildcards_are_not_columns():
    env = {"index": "svc-events-wide", "_source": ["svc.*"], "runtime_mappings": {"event.outcome": {"type": "keyword"}},
           "query": {"query_string": {"query": "x", "fields": ["host.*"]}}}
    assert extract_dsl_columns(env, _es_catalog()) == {}


def test_dsl_elasticsearch_sql_envelope_parses_as_sql():
    env = {"sql": 'SELECT "log.level", COUNT(*) FROM "obs-logs-*" WHERE "trace.id" IS NOT NULL GROUP BY 1'}
    assert _cols(extract_dsl_columns(env, _es_catalog())) == {"logs": ["log.level", "trace.id"]}


def test_dispatch_routes_json_to_dsl_and_text_to_sql():
    es = json.dumps({"index": "obs-logs-*", "query": {"term": {"trace.id": "t"}}})
    assert _cols(extract_columns(es, None, _es_catalog())) == {"logs": ["trace.id"]}
    assert _cols(extract_columns("SELECT amount FROM orders", "postgres", _catalog())) == {"orders": ["amount"]}
