"""Admin-set query guardrails on the Elasticsearch connector.

Exercised through the client's public `execute_query` with the HTTP boundary
(`_request`) faked, so every assertion is about what would — or would not —
reach the cluster:
  - all-index targets (`*`, `_all`, wildcard-only) are refused for DSL, SQL
    and ES|QL unless the admin turns the block off; named patterns pass,
  - the optional allowlist confines query targets,
  - the document cap bounds DSL size+from, the SQL fetch size and ES|QL rows,
  - aggregation limits (count, depth, buckets) reject oversized trees, can run
    log-only, and FAIL OPEN on any tree the analyzer cannot read,
  - the resolved query timeout reaches the engine and the HTTP read, an
    agent-chosen timeout can only tighten it, and partial (timed-out) results
    are refused.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.data_sources.clients.elasticsearch_client import ElasticsearchClient
from app.data_sources.clients.elasticsearch_guardrails import QueryGuardrailError


@pytest.fixture(autouse=True)
def _guardrail_loggers_enabled(monkeypatch):
    # Migration-backed tests run alembic's fileConfig(), which disables every
    # logger that already exists; re-enable the ones these tests assert on.
    for name in ("app.data_sources.clients.elasticsearch_client",
                 "app.data_sources.clients.elasticsearch_guardrails"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


class FakeCluster:
    """Records every request; answers like a healthy cluster."""

    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def __call__(self, method, path, json_body=None, params=None, timeout=None):
        self.calls.append({"method": method, "path": path, "body": json_body,
                           "params": params, "timeout": timeout})
        if self.response is not None:
            return self.response
        if path == "/_sql":
            return {"columns": [{"name": "n"}], "rows": [[1]]}
        if path == "/_query":
            return {"columns": [{"name": "n"}], "values": [[1]]}
        return {"hits": {"hits": []}, "aggregations": {}}


def make_client(monkeypatch, response=None, **config):
    client = ElasticsearchClient(host="h", **config)
    cluster = FakeCluster(response)
    monkeypatch.setattr(client, "_request", cluster)
    return client, cluster


def dsl(index, **body):
    return json.dumps({"index": index, **body})


# ---------------------------------------------------------- all-index block --- #

ALL_INDEX_TARGETS = ["*", "_all", "**", "*,-logs-old", "remote:*", ".*", "*,*", "logs-*,*"]


@pytest.mark.parametrize("target", ALL_INDEX_TARGETS)
def test_all_index_dsl_search_is_refused_before_reaching_the_cluster(monkeypatch, target):
    client, cluster = make_client(monkeypatch)
    with pytest.raises(QueryGuardrailError, match="all indices"):
        client.execute_query(dsl(target, query={"match_all": {}}))
    assert cluster.calls == []


@pytest.mark.parametrize("sql", [
    'SELECT COUNT(*) FROM "*"',
    "SELECT level FROM logs-app-* UNION SELECT level FROM _all",
])
def test_all_index_sql_is_refused(monkeypatch, sql):
    client, cluster = make_client(monkeypatch)
    with pytest.raises(QueryGuardrailError, match="all indices"):
        client.execute_query(json.dumps({"sql": sql}))
    assert cluster.calls == []


@pytest.mark.parametrize("esql", ["FROM * | STATS n = COUNT(*)", "from _all", "FROM logs-*, * | LIMIT 5"])
def test_all_index_esql_is_refused(monkeypatch, esql):
    client, cluster = make_client(monkeypatch)
    with pytest.raises(QueryGuardrailError, match="all indices"):
        client.execute_query(json.dumps({"esql": esql}))
    assert cluster.calls == []


@pytest.mark.parametrize("target", ["logs-app-*", "logs-*,metrics-*", "orders", "remote:logs-*", "logs-*,-logs-old"])
def test_named_targets_are_searched(monkeypatch, target):
    client, cluster = make_client(monkeypatch)
    client.execute_query(dsl(target))
    assert cluster.calls[0]["path"] == f"/{target}/_search"


def test_named_sql_and_esql_targets_run(monkeypatch):
    client, cluster = make_client(monkeypatch)
    client.execute_query(json.dumps({"sql": 'SELECT level FROM "logs-app-*" GROUP BY level'}))
    client.execute_query(json.dumps({"esql": "FROM logs-app-* | STATS n = COUNT(*) BY level"}))
    client.execute_query(json.dumps({"esql": "ROW a = 1"}))  # no source at all
    assert [c["path"] for c in cluster.calls] == ["/_sql", "/_query", "/_query"]


def test_block_can_be_turned_off_by_the_admin(monkeypatch):
    client, cluster = make_client(monkeypatch, block_all_indices_search=False)
    client.execute_query(dsl("*"))
    client.execute_query(json.dumps({"esql": "FROM * | LIMIT 1"}))
    assert len(cluster.calls) == 2


def test_block_defaults_on_when_form_sends_blank(monkeypatch):
    client, cluster = make_client(monkeypatch, block_all_indices_search="")
    with pytest.raises(QueryGuardrailError):
        client.execute_query(dsl("_all"))


@pytest.mark.parametrize("target", [
    "%2A",                              # decoded to `*` by the server
    "%5Fall",
    "applogs-x/../finance-secret",      # path traversal past the glob
    "applogs-*/_search?q=*",
    "logs app",
    "logs-*#frag",
])
def test_targets_that_are_not_index_names_are_refused(monkeypatch, target):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="applogs-*")
    with pytest.raises(QueryGuardrailError, match="Invalid index target"):
        client.execute_query(dsl(target))
    assert cluster.calls == []


@pytest.mark.parametrize("esql", [
    "// just counting\nFROM * | STATS n = COUNT(*)",
    "/* scan */ FROM _all | LIMIT 1",
    "  \n// a\n// b\n  from * ",
    "TS * | STATS max(cpu)",
])
def test_comments_cannot_hide_an_all_index_esql_source(monkeypatch, esql):
    client, cluster = make_client(monkeypatch)
    with pytest.raises(QueryGuardrailError, match="all indices"):
        client.execute_query(json.dumps({"esql": esql}))
    assert cluster.calls == []


@pytest.mark.parametrize("sql", [
    'SELECT EXTRACT(YEAR FROM "@timestamp") AS y, COUNT(*) FROM "logs-app" GROUP BY y',
    "SELECT * FROM \"logs-app\" WHERE message = 'retry from upstream finance'",
    'SELECT level FROM "logs-app" -- not from finance',
    "SELECT TRIM(LEADING 'x' FROM service) FROM logs-app",
])
def test_sql_from_inside_functions_literals_and_comments_is_not_a_target(monkeypatch, sql):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="logs-*")
    client.execute_query(json.dumps({"sql": sql}))
    assert len(cluster.calls) == 1


def test_sql_subquery_source_is_still_checked(monkeypatch):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="logs-*")
    with pytest.raises(QueryGuardrailError, match="finance"):
        client.execute_query(json.dumps({"sql": 'SELECT * FROM (SELECT * FROM "finance") t'}))
    assert cluster.calls == []


# Comment markers inside literals are data, not comments: lexing them as
# comments swallowed the FROM after them and the query ran unchecked.
@pytest.mark.parametrize("sql", [
    """SELECT '--' AS marker, secret FROM "finance-secret" """,
    """SELECT '/*' AS a, secret FROM "finance-secret" WHERE b = '*/'""",
    """SELECT 'it''s -- fine' AS a, secret FROM "finance-secret" """,
    """SELECT "--weird col" FROM "finance-secret" """,
    """SELECT x FROM "applogs-1" UNION SELECT y FROM (SELECT '--' FROM "finance-secret") t""",
])
def test_sql_literal_comment_markers_cannot_hide_a_target(monkeypatch, sql):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="applogs-*")
    with pytest.raises(QueryGuardrailError, match="finance-secret"):
        client.execute_query(json.dumps({"sql": sql}))
    assert cluster.calls == []


def test_sql_literal_comment_marker_cannot_hide_an_all_index_target(monkeypatch):
    client, cluster = make_client(monkeypatch)
    with pytest.raises(QueryGuardrailError, match="all indices"):
        client.execute_query(json.dumps({"sql": """SELECT '--' AS m, x FROM "*" """}))
    assert cluster.calls == []


@pytest.mark.parametrize("query", [
    {"sql": "SELECT 'unterminated FROM \"finance-secret\""},
    {"sql": 'SELECT x FROM "applogs-1" /* never closed'},
    {"esql": 'FROM applogs-1 | WHERE m == "unterminated'},
])
def test_queries_that_cannot_be_lexed_are_refused(monkeypatch, query):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="applogs-*")
    with pytest.raises(QueryGuardrailError, match="Unterminated"):
        client.execute_query(json.dumps(query))
    assert cluster.calls == []


@pytest.mark.parametrize("esql", [
    "FROM applogs-1 | LOOKUP JOIN finance-secret ON employee_id",
    "FROM applogs-1\n| WHERE level == \"error\"\n| lookup join finance-secret on employee_id",
    'FROM applogs-1 | EVAL x = "| LOOKUP JOIN ok-index" | LOOKUP JOIN finance-secret ON id',
    "FROM applogs-1, (FROM finance-secret | KEEP secret)",
    "FROM applogs-1 | FORK (WHERE a > 1) (WHERE b > 1 | LOOKUP JOIN finance-secret ON id)",
])
def test_every_esql_index_bearing_command_is_checked(monkeypatch, esql):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="applogs-*")
    with pytest.raises(QueryGuardrailError, match="finance-secret"):
        client.execute_query(json.dumps({"esql": esql}))
    assert cluster.calls == []


def test_lookup_join_on_an_allowed_index_runs(monkeypatch):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="applogs-*,lookup-*")
    client.execute_query(json.dumps({"esql": "FROM applogs-1 | LOOKUP JOIN lookup-hosts ON host | KEEP host, team"}))
    assert len(cluster.calls) == 1


def test_lookup_join_cannot_reach_all_indices(monkeypatch):
    client, cluster = make_client(monkeypatch)
    with pytest.raises(QueryGuardrailError, match="all indices"):
        client.execute_query(json.dumps({"esql": "FROM applogs-1 | LOOKUP JOIN * ON id"}))
    assert cluster.calls == []


def test_enrich_is_refused_under_an_allowlist_but_not_otherwise(monkeypatch):
    query = json.dumps({"esql": "FROM applogs-1 | ENRICH finance_policy ON account"})
    client, cluster = make_client(monkeypatch, allowed_index_patterns="applogs-*")
    with pytest.raises(QueryGuardrailError, match="ENRICH"):
        client.execute_query(query)
    assert cluster.calls == []
    client, cluster = make_client(monkeypatch)
    client.execute_query(query)
    assert len(cluster.calls) == 1


def test_unrecognised_esql_command_is_refused(monkeypatch):
    client, cluster = make_client(monkeypatch)
    with pytest.raises(QueryGuardrailError, match="not recognised"):
        client.execute_query(json.dumps({"esql": "FROM applogs-1 | SNEAKY_READ finance-secret"}))
    assert cluster.calls == []


@pytest.mark.parametrize("esql", [
    'FROM applogs-1 | WHERE message == "FROM * | LOOKUP JOIN finance-secret" | STATS n = COUNT(*)',
    'FROM applogs-1 | EVAL ts = MAX(ts) | STATS n = COUNT(from) BY ts',
    'FROM "applogs-1" METADATA _id, _index | KEEP _id',
    'FROM applogs-1 | WHERE url LIKE "http://x" // trailing comment\n| LIMIT 5',
    'ROW a = 1 | EVAL b = a + 1',
])
def test_esql_strings_comments_and_field_names_are_not_targets(monkeypatch, esql):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="applogs-*")
    client.execute_query(json.dumps({"esql": esql}))
    assert len(cluster.calls) == 1


# ---------------------------------------------------------------- allowlist --- #

@pytest.mark.parametrize("target", ["logs-app-*", "logs-2026.10", "metrics-cpu", "logs-*", "remote:logs-x"])
def test_allowlist_admits_targets_under_its_patterns(monkeypatch, target):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="logs-*, metrics-*")
    client.execute_query(dsl(target))
    assert len(cluster.calls) == 1


@pytest.mark.parametrize("target", ["finance", "logs-*,finance", "l*", "metrics"])
def test_allowlist_refuses_targets_outside_it(monkeypatch, target):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="logs-*,metrics-*")
    with pytest.raises(QueryGuardrailError, match="outside"):
        client.execute_query(dsl(target))
    assert cluster.calls == []


def test_allowlist_applies_to_sql_and_esql(monkeypatch):
    client, cluster = make_client(monkeypatch, allowed_index_patterns="logs-*")
    with pytest.raises(QueryGuardrailError):
        client.execute_query(json.dumps({"sql": 'SELECT * FROM "finance"'}))
    with pytest.raises(QueryGuardrailError):
        client.execute_query(json.dumps({"esql": "FROM finance | LIMIT 1"}))
    assert cluster.calls == []


# ------------------------------------------------------------ document cap --- #

def test_document_cap_refuses_larger_windows(monkeypatch):
    client, cluster = make_client(monkeypatch, max_result_size=500)
    with pytest.raises(QueryGuardrailError, match="500"):
        client.execute_query(dsl("logs-*", size=400, **{"from": 200}))
    client.execute_query(dsl("logs-*", size=500))
    assert len(cluster.calls) == 1


def test_document_cap_lowers_the_default_page(monkeypatch):
    client, cluster = make_client(monkeypatch, max_result_size=25)
    client.execute_query(dsl("logs-*"))
    assert cluster.calls[0]["body"]["size"] == 25


def test_document_cap_never_exceeds_the_engine_window(monkeypatch):
    client, _ = make_client(monkeypatch, max_result_size=50_000)
    with pytest.raises(QueryGuardrailError):
        client.execute_query(dsl("logs-*", size=10_001))


def test_uncapped_connection_keeps_engine_row_defaults(monkeypatch):
    client, cluster = make_client(monkeypatch)
    client.execute_query(json.dumps({"sql": 'SELECT * FROM "logs-*"'}))
    client.execute_query(json.dumps({"esql": "FROM logs-* | LIMIT 5000"}))
    assert "fetch_size" not in cluster.calls[0]["body"]
    assert cluster.calls[1]["body"]["query"] == "FROM logs-* | LIMIT 5000"


def test_document_cap_bounds_sql_and_esql_rows(monkeypatch):
    client, cluster = make_client(monkeypatch, max_result_size=200)
    client.execute_query(json.dumps({"sql": 'SELECT * FROM "logs-*"'}))
    client.execute_query(json.dumps({"esql": "FROM logs-* | LIMIT 5000"}))
    assert cluster.calls[0]["body"]["fetch_size"] == 200
    assert cluster.calls[1]["body"]["query"].rstrip().endswith("| LIMIT 200")


# ----------------------------------------------------------- aggregations --- #

def terms(field, size=None, **children):
    spec = {"field": field}
    if size is not None:
        spec["size"] = size
    body = {"terms": spec}
    if children:
        body["aggs"] = children
    return body


def test_aggregation_count_limit(monkeypatch):
    client, cluster = make_client(monkeypatch, max_aggregations=3)
    aggs = {"a": terms("x", 5, b=terms("y", 5)), "c": {"avg": {"field": "ms"}},
            "d": {"max": {"field": "ms"}}}
    with pytest.raises(QueryGuardrailError, match="4 aggregations"):
        client.execute_query(dsl("logs-*", aggs=aggs))
    aggs.pop("d")
    client.execute_query(dsl("logs-*", aggs=aggs))
    assert len(cluster.calls) == 1


def test_aggregation_depth_limit(monkeypatch):
    client, cluster = make_client(monkeypatch, max_aggregation_depth=2)
    deep = {"a": terms("x", 2, b=terms("y", 2, c={"avg": {"field": "ms"}}))}
    with pytest.raises(QueryGuardrailError, match="3 nested"):
        client.execute_query(dsl("logs-*", aggregations=deep))
    client.execute_query(dsl("logs-*", aggs={"a": terms("x", 2, b={"avg": {"field": "ms"}})}))
    assert len(cluster.calls) == 1


@pytest.mark.parametrize("aggs, allowed", [
    ({"a": terms("x", 5000)}, False),                       # one huge terms
    ({"a": terms("x", 100, b=terms("y", 100))}, False),     # 100 x 100 multiplies
    ({"a": terms("x", 30, b=terms("y", 30))}, True),        # 30 + 30*30 = 930
    ({"a": terms("x")}, True),                              # default size 10
    ({"h": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"},
            "aggs": {"t": terms("x", 2000)}}}, False),      # size under a histogram
    ({"c": {"composite": {"size": 900, "sources": []}}}, True),
])
def test_aggregation_bucket_limit(monkeypatch, aggs, allowed):
    client, cluster = make_client(monkeypatch, max_aggregation_buckets=1000)
    if allowed:
        client.execute_query(dsl("logs-*", aggs=aggs))
        assert len(cluster.calls) == 1
    else:
        with pytest.raises(QueryGuardrailError, match="buckets"):
            client.execute_query(dsl("logs-*", aggs=aggs))
        assert cluster.calls == []


def test_both_aggregation_keys_are_checked(monkeypatch):
    client, cluster = make_client(monkeypatch, max_aggregations=1)
    with pytest.raises(QueryGuardrailError):
        client.execute_query(dsl("logs-*", aggs={"a": terms("x")},
                                 aggregations={"b": terms("y"), "c": terms("z")}))
    assert cluster.calls == []


def test_log_only_mode_runs_the_query_and_warns(monkeypatch, caplog):
    client, cluster = make_client(monkeypatch, max_aggregations=1,
                                  aggregation_limits_observe_only=True)
    with caplog.at_level(logging.WARNING):
        client.execute_query(dsl("logs-*", aggs={"a": terms("x"), "b": terms("y")}))
    assert len(cluster.calls) == 1
    assert "would block" in caplog.text


@pytest.mark.parametrize("aggs", [
    {"a": {"terms": {"field": "x", "size": "{{params.n}}"}}},   # templated size
    {"a": "not-an-object"},
    {"a": {"terms": {"field": "x"}, "aggs": ["unexpected", "list"]}},
    ["a", "list"],
])
def test_unreadable_aggregation_tree_fails_open(monkeypatch, caplog, aggs):
    """A checker error must never block the query — it runs unchecked."""
    client, cluster = make_client(monkeypatch, max_aggregations=1, max_aggregation_depth=1,
                                  max_aggregation_buckets=1)
    with caplog.at_level(logging.WARNING):
        client.execute_query(dsl("logs-*", aggs=aggs))
    assert len(cluster.calls) == 1
    assert "runs unchecked" in caplog.text


def test_no_aggregation_limits_by_default(monkeypatch):
    client, cluster = make_client(monkeypatch)
    big = {f"a{i}": terms("x", 10_000, b=terms("y", 10_000)) for i in range(30)}
    client.execute_query(dsl("logs-*", aggs=big))
    assert len(cluster.calls) == 1


def test_limits_accept_form_string_values(monkeypatch):
    client, cluster = make_client(monkeypatch, max_aggregations="2", max_aggregation_depth="",
                                  max_result_size="", max_aggregation_buckets=None)
    with pytest.raises(QueryGuardrailError):
        client.execute_query(dsl("logs-*", aggs={"a": terms("x"), "b": terms("y"), "c": terms("z")}))
    client.execute_query(dsl("logs-*", size=10_000))
    assert len(cluster.calls) == 1


# ---------------------------------------------------------------- timeout --- #

def test_default_timeout_reaches_engine_and_socket(monkeypatch):
    client, cluster = make_client(monkeypatch)
    client.execute_query(dsl("logs-*"))
    call = cluster.calls[0]
    assert call["body"]["timeout"] == "60s"
    assert call["timeout"][1] > 60


def test_resolved_timeout_replaces_the_default(monkeypatch):
    client, cluster = make_client(monkeypatch)
    client.apply_query_timeout(240)
    client.execute_query(dsl("logs-*"))
    client.execute_query(json.dumps({"esql": "FROM logs-* | LIMIT 1"}))
    client.execute_query(json.dumps({"sql": 'SELECT 1 FROM "logs-*"'}))
    assert cluster.calls[0]["body"]["timeout"] == "240s"
    assert cluster.calls[2]["body"]["request_timeout"] == "240s"
    assert all(c["timeout"][1] > 240 for c in cluster.calls)


@pytest.mark.parametrize("asked, sent", [
    ("10s", "10s"),        # tighter: kept
    ("500ms", "500ms"),    # tighter: kept
    ("10m", "60s"),        # looser: clamped to the budget
    ("forever", "60s"),    # unreadable: the budget
    (0, "60s"),
])
def test_agent_timeout_can_only_tighten(monkeypatch, asked, sent):
    client, cluster = make_client(monkeypatch)
    client.execute_query(dsl("logs-*", timeout=asked))
    assert cluster.calls[0]["body"]["timeout"] == sent


def test_partial_results_after_engine_timeout_are_refused(monkeypatch):
    client, _ = make_client(monkeypatch, response={
        "timed_out": True, "hits": {"hits": [{"_id": "1", "_source": {}}]}})
    with pytest.raises(QueryGuardrailError, match="partial"):
        client.execute_query(dsl("logs-*"))


# ----------------------------------------------------------------- prompt --- #

def test_agent_prompt_names_the_active_limits():
    client = ElasticsearchClient(host="h", allowed_index_patterns="logs-*", max_result_size=300,
                                 max_aggregations=5, max_aggregation_buckets=2000)
    text = client.description
    assert "ALL indices" in text
    assert "logs-*" in text
    assert "300" in text
    assert "5 aggregations" in text
    assert "2,000 buckets" in text


def test_agent_prompt_omits_log_only_aggregation_limits():
    client = ElasticsearchClient(host="h", max_aggregations=5, aggregation_limits_observe_only=True)
    assert "5 aggregations" not in client.description
