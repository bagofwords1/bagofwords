"""Parameterized queries — core contract tests.

Covers the safety-critical pieces: safe SQL rendering (injection, types,
dialects, IN expansion), value resolution (identity locking, unknown
rejection, defaults, optional='All'), fingerprint stability, the AST guards,
and declaration/code consistency.
"""

import asyncio
import pytest
from datetime import date, datetime

from app.schemas.param_schema import ParamSpec, parse_param_specs
from app.ai.code_execution.query_params import (
    ParamError,
    calendar_date_bounds,
    check_declarations_vs_code,
    check_params_not_formatted,
    code_accepts_params,
    extract_code_param_names,
    params_fingerprint,
    render_sql_with_params,
    resolve_param_values,
    verify_identity_binds_in_queries,
)


class FakeIdentity:
    user_id = "u-1"
    email = "sarah@example.com"
    profile_attributes = {"department": "Sales"}
    group_names = {"emea", "managers"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render_string_quoted_and_escaped():
    out = render_sql_with_params("SELECT * FROM t WHERE a = :v", {"v": "x' OR '1'='1"})
    assert out == "SELECT * FROM t WHERE a = 'x'' OR ''1''=''1'"


def test_render_backslash_dialect():
    out = render_sql_with_params(
        "SELECT * FROM t WHERE a = :v", {"v": "a\\'b"}, client_type_name="MysqlClient"
    )
    assert out == "SELECT * FROM t WHERE a = 'a\\\\''b'"


def test_render_mssql_non_ascii_gets_nvarchar_prefix():
    # SQL_Latin1_General_* databases turn a plain 'מטה' into '???' at parse
    # time; only N'…' survives. Reproduces the "Run with a value returns 0
    # rows" report on an MSSQL source.
    out = render_sql_with_params(
        "WHERE cat = :c", {"c": "מטה"}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat = N'מטה'"
    out = render_sql_with_params(
        "WHERE cat = :c", {"c": "מטה"}, client_type_name="MsFabricClient"
    )
    assert out == "WHERE cat = N'מטה'"


def test_render_mssql_ascii_stays_plain():
    out = render_sql_with_params(
        "WHERE cat = :c AND n = :n", {"c": "Admin", "n": 3}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat = 'Admin' AND n = 3"


def test_render_mssql_list_prefixes_each_non_ascii_item():
    out = render_sql_with_params(
        "WHERE cat IN :c", {"c": ["מטה", "Admin", "יצרן"]}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat IN (N'מטה', 'Admin', N'יצרן')"


def test_render_mssql_non_ascii_still_escapes_quotes():
    out = render_sql_with_params(
        "WHERE cat = :c", {"c": "מט'ה"}, client_type_name="MSSQLClient"
    )
    assert out == "WHERE cat = N'מט''ה'"


def test_render_non_tsql_clients_never_prefix():
    for name in ("SqliteClient", "PostgresqlClient", "MysqlClient", ""):
        out = render_sql_with_params("WHERE cat = :c", {"c": "מטה"}, client_type_name=name)
        assert out == "WHERE cat = 'מטה'", name


def test_render_number_and_null_and_bool():
    out = render_sql_with_params(
        "WHERE n = :n AND m = :m AND b = :b", {"n": 5, "m": None, "b": True}
    )
    assert out == "WHERE n = 5 AND m = NULL AND b = TRUE"


def test_render_list_in_expansion():
    out = render_sql_with_params("WHERE g IN :g", {"g": ["a", "b"]})
    assert out == "WHERE g IN ('a', 'b')"


def test_render_empty_list_never_matches():
    out = render_sql_with_params("WHERE g IN :g", {"g": []})
    assert out == "WHERE g IN (NULL)"


def test_render_missing_value_raises():
    with pytest.raises(ParamError):
        render_sql_with_params("WHERE a = :a AND b = :b", {"a": 1})


def test_render_ignores_double_colon_casts():
    out = render_sql_with_params("SELECT x::text FROM t WHERE a = :a", {"a": 1})
    assert out == "SELECT x::text FROM t WHERE a = 1"


def test_render_extra_values_ok():
    out = render_sql_with_params("WHERE a = :a", {"a": 1, "unused": 2})
    assert out == "WHERE a = 1"


def test_render_non_finite_rejected():
    with pytest.raises(ParamError):
        render_sql_with_params("WHERE a = :a", {"a": float("inf")})


# ---------------------------------------------------------------------------
# String literals per dialect, through the run's query wrapper
# ---------------------------------------------------------------------------

# (client module, client class, sqlglot dialect). The second and third
# groups' engines read a backslash inside a string literal as an escape
# character. An engine sqlglot has no dialect for is read with one whose
# string-literal rules are the engine's (the client's sql_literal_style
# comment cites them); those are listed in _STAND_IN_DIALECT.
# (MsFabricClient and SybaseClient are left out: their modules need the ODBC
# driver to import.)
_STANDARD_LITERAL_CLIENTS = [
    ("postgresql_client", "PostgresqlClient", "postgres"),
    ("duckdb_client", "DuckDBClient", "duckdb"),
    ("mssql_client", "MSSQLClient", "tsql"),
    ("oracledb_client", "OracledbClient", "oracle"),
    ("trino_client", "TrinoClient", "trino"),
    ("presto_client", "PrestoClient", "presto"),
    # Athena runs queries on its Trino engine, whose string literals have no
    # backslash escapes; sqlglot's "athena" dialect also accepts a backslash
    # escape (it covers Athena's Hive DDL engine too), so the query is read in
    # the Trino grammar.
    ("aws_athena_client", "AwsAthenaClient", "trino"),
    ("teradata_client", "TeradataClient", "teradata"),
    ("sqlite_client", "SqliteClient", "sqlite"),
    ("druid_client", "DruidClient", "druid"),
    ("dremio_client", "DremioClient", "dremio"),
    # DuckDB SQL underneath.
    ("iceberg_client", "IcebergClient", "duckdb"),
    ("csv_client", "CSVClient", "duckdb"),
    ("qvd_client", "QVDClient", "duckdb"),
    ("pbix_client", "PBIXClient", "duckdb"),
    ("app.data_sources.fast.fast_client", "FastQueryClient", "duckdb"),
    ("db2_client", "Db2Client", "postgres"),
    ("ibmi_client", "IbmiClient", "postgres"),
    ("sap_hana_client", "SapHanaClient", "postgres"),
    ("pinot_client", "PinotClient", "postgres"),
    ("netsuite_client", "NetsuiteClient", "oracle"),
    ("cassandra_client", "CassandraClient", "postgres"),
]
_DOUBLED_LITERAL_CLIENTS = [
    ("mysql_client", "MysqlClient", "mysql"),
    ("mariadb_client", "MariadbClient", "mysql"),
    ("snowflake_client", "SnowflakeClient", "snowflake"),
    ("clickhouse_client", "ClickhouseClient", "clickhouse"),
    ("aws_redshift_client", "AwsRedshiftClient", "redshift"),
    # HogQL's string literal is ClickHouse's.
    ("posthog_client", "PostHogClient", "clickhouse"),
    ("vertica_client", "VerticaClient", "snowflake"),
    ("dynamodb_client", "DynamodbClient", "snowflake"),
    ("oracle_bi_client", "OracleBIClient", "snowflake"),
]
_BACKSLASH_LITERAL_CLIENTS = [
    ("bigquery_client", "BigqueryClient", "bigquery"),
    ("databricks_sql_client", "DatabricksSqlClient", "databricks"),
    ("spark_connect_client", "SparkConnectClient", "spark"),
    ("hive_client", "HiveClient", "hive"),
    ("salesforce_client", "SalesforceClient", "spark"),
    ("cosmosdb_client", "CosmosdbClient", "spark"),
    ("couchbase_client", "CouchbaseClient", "spark"),
]
_ALL_LITERAL_CLIENTS = _STANDARD_LITERAL_CLIENTS + _DOUBLED_LITERAL_CLIENTS + _BACKSLASH_LITERAL_CLIENTS
# Read with a stand-in dialect (see above): sqlglot has none of their own.
_STAND_IN_DIALECT = {
    "Db2Client", "IbmiClient", "SapHanaClient", "PinotClient", "NetsuiteClient", "CassandraClient",
    "VerticaClient", "DynamodbClient", "OracleBIClient", "SalesforceClient", "CosmosdbClient",
    "CouchbaseClient",
}

_AWKWARD_VALUES = [
    "a backslash\\' before a quote, then more words",
    "ends with a backslash\\",
    "it's",
    "two '' quotes",
    "line one\nline two",
    "carriage\r\nreturn",
    "double \\\\ backslash",
    "C:\\temp\\",
]


def _recording_client(module: str, class_name: str, *, keep_name: bool = False):
    """A subclass of the real client class that records the SQL it is handed
    instead of reaching a database. Being a subclass under its own name also
    checks that the literal style is inherited, not looked up by class name;
    `keep_name` gives it the real class's name (what the identity check reads
    the SQL grammar from)."""
    import importlib

    import pandas as pd

    path = module if module.startswith("app.") else f"app.data_sources.clients.{module}"
    base = getattr(importlib.import_module(path), class_name)

    def __init__(self):
        self.received = []

    def execute_query(self, sql, *args, **kwargs):
        self.received.append(sql)
        return pd.DataFrame({"ok": [1]})

    name = class_name if keep_name else "RecordingClient"
    return type(name, (base,), {"__init__": __init__, "execute_query": execute_query})()


def _sql_received(module: str, class_name: str, sql: str, value, captured=None, values=None, **kw) -> str:
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _recording_client(module, class_name, **kw)
    QueryCapturingClientWrapper(client, captured if captured is not None else [], [],
                                query_timeout_seconds=30).execute_query(
                                    sql, params=values if values is not None else {"v": value})
    (received,) = client.received
    return received


@pytest.mark.parametrize("module,class_name,dialect", _ALL_LITERAL_CLIENTS)
@pytest.mark.parametrize("value", _AWKWARD_VALUES)
def test_a_string_param_stays_one_string_literal_in_its_engines_grammar(module, class_name, dialect, value):
    """Whatever the value holds (quotes, backslashes, a line break), the engine
    reads it back as exactly one string literal equal to the value: nothing of
    it is left outside the literal to be read as SQL. (sqlglot's token text is
    the literal's value with its escapes already read, so `\\n` in the
    literal compares equal to a line break in the value.)"""
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import TokenType

    rendered = _sql_received(module, class_name, ":v", value)
    tokens = Dialect.get_or_raise(dialect).tokenize(rendered)
    assert [t.token_type for t in tokens] in ([TokenType.STRING], [TokenType.NATIONAL_STRING]), (
        f"{class_name}: {rendered!r} tokenizes as {[(t.token_type, t.text) for t in tokens]}")
    assert tokens[0].text == value


@pytest.mark.parametrize("module,class_name,dialect", _BACKSLASH_LITERAL_CLIENTS)
@pytest.mark.parametrize("value", ["line one\nline two", "carriage\r\nreturn", "ends with a line break\n"])
def test_a_backslash_style_literal_holds_no_raw_line_break(module, class_name, dialect, value):
    """A single-quoted BigQuery literal cannot hold an unescaped line break
    (only a triple-quoted one can), so the value's line breaks are written
    as escapes. sqlglot accepts the raw break, so this is checked on the
    rendered text."""
    rendered = _sql_received(module, class_name, ":v", value)
    assert "\n" not in rendered and "\r" not in rendered, f"{class_name}: {rendered!r}"


# Query languages where a backslash escapes inside a string literal and no
# style written here is established for them: KQL, PromQL, SPL. A 'standard'
# literal could end early there.
_REFUSING_LITERAL_CLIENTS = [
    ("azure_data_explorer_client", "AzureDataExplorerClient"),
    ("prometheus_client", "PrometheusClient"),
    ("splunk_client", "SplunkClient"),
]

# Connectors whose grammar is not established to need another style: they
# render parameters as before (standard).
_UNESTABLISHED_LITERAL_CLIENTS = [
    ("sisense_client", "SisenseClient"),
    ("timbr_client", "TimbrClient"),
    ("timbr_a2a_client", "TimbrA2aClient"),
    ("sap_datasphere_client", "SapDatasphereClient"),
    ("analysis_services_client", "AnalysisServicesClient"),
]


@pytest.mark.parametrize("module,class_name", _UNESTABLISHED_LITERAL_CLIENTS)
def test_a_source_with_no_other_established_rule_keeps_the_standard_literal(module, class_name):
    """A filtered dashboard over such a source keeps running: the value is
    written with the doubled quote, as before."""
    assert _sql_received(module, class_name, "WHERE a = :v", "it's") == "WHERE a = 'it''s'"


@pytest.mark.parametrize("module,class_name", _REFUSING_LITERAL_CLIENTS)
def test_a_source_without_a_literal_rule_refuses_query_parameters(module, class_name):
    """No query is written with a quoting rule known to be wrong for its
    language: the run is refused with a ParamError naming the reason, and the
    source receives nothing."""
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _recording_client(module, class_name)
    with pytest.raises(ParamError, match="string literal"):
        QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30).execute_query(
            "query where owner = :v", params={"v": "it's"})
    assert client.received == []


@pytest.mark.parametrize("module,class_name", _REFUSING_LITERAL_CLIENTS)
def test_a_query_with_no_placeholder_runs_even_where_parameters_are_refused(module, class_name):
    """Values handed along with a query that uses none of them render
    nothing, so there is nothing to refuse."""
    assert _sql_received(module, class_name, "StormEvents | take 10", "it's") == "StormEvents | take 10"


# Connectors whose query text is a JSON document, not SQL: a placeholder that
# stands as a JSON value takes a number only (anything else would have to be
# written as JSON there).
_JSON_QUERY_CLIENTS = [
    ("mongodb_client", "MongodbClient", '{"collection": "orders", "find": {"owner": :v}}'),
    ("servicenow_client", "ServiceNowClient", '{"table": "incident", "limit": :v}'),
    ("zabbix_client", "ZabbixClient", '{"table": "hosts", "filter": {"hostid": :v}}'),
    # A placeholder as a key is refused whatever the value.
    ("servicenow_client", "ServiceNowClient", '{"table": "incident", ":v": 1}'),
]


@pytest.mark.parametrize("module,class_name,query", _JSON_QUERY_CLIENTS)
@pytest.mark.parametrize("value", ['x", "collection": "users", "z": "', "plain", True, None, [1, 2],
                                   {"$ne": 1}, date(2024, 1, 1), float("nan")])
def test_a_json_query_refuses_a_parameter_that_is_not_a_number(module, class_name, query, value):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _recording_client(module, class_name)
    with pytest.raises(ParamError):
        QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30).execute_query(
            query, params={"v": value})
    assert client.received == []


_JSON_NUMBER_QUERIES = [
    ("servicenow_client", "ServiceNowClient", '{"table": "incident", "limit": :n}', {"table", "limit"}),
    ("zabbix_client", "ZabbixClient", '{"table": "history", "time_from": :n}', {"table", "time_from"}),
    ("mongodb_client", "MongodbClient", '{"collection": "orders", "find": {"qty": {"$gt": :n}}}',
     {"collection", "find"}),
    ("elasticsearch_client", "ElasticsearchClient", '{"index": "logs", "size": :n}', {"index", "size"}),
    ("opensearch_client", "OpenSearchClient", '{"index": "logs", "size": :n}', {"index", "size"}),
]


@pytest.mark.parametrize("module,class_name,query,keys", _JSON_NUMBER_QUERIES)
@pytest.mark.parametrize("number", [5, 2.5, -3])
def test_a_json_query_takes_a_number_parameter(module, class_name, query, keys, number):
    """A finite number is written as a bare JSON number: the document keeps
    exactly its keys and holds the number where the placeholder was."""
    import json

    parsed = json.loads(_sql_received(module, class_name, query, None, values={"n": number}))
    assert set(parsed) == keys

    def numbers(node):
        if isinstance(node, dict):
            return [n for v in node.values() for n in numbers(v)]
        if isinstance(node, list):
            return [n for v in node for n in numbers(v)]
        return [node] if isinstance(node, (int, float)) and not isinstance(node, bool) else []

    assert numbers(parsed) == [number]


# A placeholder inside a JSON string value is written as main wrote it (the
# client's literal), and json.dumps then escapes it for that string.
_JSON_STRING_QUERIES = [
    ("intune_client", "IntuneClient", '{"table": "managed_devices", "filter": "complianceState eq :s"}',
     {"s": "noncompliant"}, "filter", "complianceState eq 'noncompliant'"),
    ("servicenow_client", "ServiceNowClient", '{"table": "incident", "query": "priority=:p"}',
     {"p": 1}, "query", "priority=1"),
    ("servicenow_client", "ServiceNowClient", '{"table": "incident", "query": "caller=:c"}',
     {"c": 'a", "x": "y'}, "query", 'caller=\'a", "x": "y\''),
]


@pytest.mark.parametrize("module,class_name,query,values,field,expected", _JSON_STRING_QUERIES)
def test_a_placeholder_inside_a_json_string_renders_as_before_and_stays_in_it(
        module, class_name, query, values, field, expected):
    import json

    original = json.loads(query)  # placeholders inside strings: valid JSON as written
    parsed = json.loads(_sql_received(module, class_name, query, None, values=values))
    assert set(parsed) == set(original)
    assert parsed[field] == expected


def test_a_placeholder_run_into_other_digits_is_refused():
    """`:n.5` is no placeholder value of the document: nothing may be left
    standing in for one."""
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _recording_client("servicenow_client", "ServiceNowClient")
    with pytest.raises(ParamError):
        QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30).execute_query(
            '{"table": "incident", "x": :n.5}', params={"n": 5})
    assert client.received == []


def _one_pass_render(text: str, values: dict) -> str:
    """How a string with placeholders renders in one pass (as on main):
    each placeholder by the value's literal, the rendered values never
    scanned again."""
    from app.ai.code_execution.query_params import _PLACEHOLDER_RE, _render_literal

    return _PLACEHOLDER_RE.sub(lambda m: _render_literal(values[m.group(1)], literal_style="standard"), text)


def test_a_value_holding_another_placeholders_stand_in_stays_its_own_literal():
    """A value that carries the digits a later placeholder is swapped for
    while rendering must not pull that placeholder's value into itself."""
    import json

    from app.ai.code_execution.identity_bind import CapturedQuery

    filter_text = "deviceName eq :p and model eq :c and userPrincipalName eq :me"
    values = {"p": "x7301000000000000001", "c": " or true or model ne ", "me": "a@x.com"}
    captured: list = []
    rendered = _sql_received("intune_client", "IntuneClient",
                             json.dumps({"table": "managed_devices", "filter": filter_text}),
                             None, values=values, captured=captured)
    parsed = json.loads(rendered)
    assert parsed["filter"] == ("deviceName eq 'x7301000000000000001' and model eq ' or true or model ne ' "
                                "and userPrincipalName eq 'a@x.com'")
    assert set(parsed) == {"table", "filter"}


@pytest.mark.parametrize("values", [
    {"a": "7301000000000000001", "b": "x", "c": "y"},
    {"a": "'7301000000000000002'", "b": "7301000000000000000", "c": "q"},
    {"a": "7301000000000000001 7301000000000000002", "b": " or 1=1 or ", "c": "z"},
    {"a": "8301000000000000001", "b": "7301000000000000002", "c": "8301000000000000000"},
    # A value made of every stand-in base the renderer could pick: the base
    # is chosen from the document alone, and the value still renders as its
    # own literal.
    {"a": "".join(str(7301000000000000000 + i * 1000000000000000)[:12] for i in range(40)),
     "b": "7301000000000000001", "c": "q"},
])
def test_every_string_renders_as_a_one_pass_render_of_its_placeholders(values):
    import json

    document = {"table": "t", "one": "x = :a and y = :b", "two": ":c then :a", "nested": {"three": ":b:c"}}
    parsed = json.loads(_sql_received("servicenow_client", "ServiceNowClient", json.dumps(document), None,
                                      values=values))
    assert parsed == {
        "table": "t",
        "one": _one_pass_render(document["one"], values),
        "two": _one_pass_render(document["two"], values),
        "nested": {"three": _one_pass_render(document["nested"]["three"], values)},
    }


def test_only_an_envelope_client_has_its_captured_sql_read_as_the_statement():
    """A JSON capture of a client that takes no SQL envelope keeps the check
    it had: its "esql"/"sql" keys mean nothing to that client."""
    from app.ai.code_execution.identity_bind import CapturedQuery

    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    text = '{"esql": "FROM t", "sql": "SELECT * FROM t WHERE owner = \'a@x.com\'"}'
    other_client = CapturedQuery(text, "MongodbClient", "standard")
    assert verify_identity_binds_in_queries([other_client], {"v": "a@x.com"}, specs) is None


def test_a_mongo_range_query_takes_a_number():
    import json

    parsed = json.loads(_sql_received("mongodb_client", "MongodbClient",
                                      '{"collection": "orders", "find": {"qty": {"$gt": :n}}}', None,
                                      values={"n": 5}))
    assert parsed == {"collection": "orders", "find": {"qty": {"$gt": 5}}}


def test_a_non_string_query_with_parameters_is_refused():
    """A query handed over as a dict is never rendered, so its placeholders
    would run as written: refused instead."""
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _recording_client("elasticsearch_client", "ElasticsearchClient")
    with pytest.raises(ParamError):
        QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30).execute_query(
            {"index": "logs", "query": {"term": {"owner": ":v"}}}, params={"v": "a@x.com"})
    with pytest.raises(ParamError):
        QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30).execute_query(
            '{"index": "logs"}', params=[("v", 1)])
    assert client.received == []


# Elasticsearch SQL / the OpenSearch SQL plugin, inside the JSON envelope's
# "sql" field. (Stand-in dialects: '' only for Elasticsearch SQL, '' and a
# backslash for OpenSearch's doubled literals.)
_ENVELOPE_SQL_CLIENTS = [
    ("elasticsearch_client", "ElasticsearchClient", "postgres"),
    ("opensearch_client", "OpenSearchClient", "snowflake"),
]
_ENVELOPE_VALUES = _AWKWARD_VALUES + [
    'x", "sql": "SELECT * FROM orders", "z": "',
    'tab\there "quoted" \u0041',
]


@pytest.mark.parametrize("module,class_name,dialect", _ENVELOPE_SQL_CLIENTS)
@pytest.mark.parametrize("value", _ENVELOPE_VALUES)
def test_a_param_in_the_envelopes_sql_stays_one_literal_of_that_sql(module, class_name, dialect, value):
    """The envelope parses with exactly its own keys, and its SQL reads the
    value back as one string literal equal to it."""
    import json

    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import TokenType

    envelope = '{"index": "orders", "sql": "SELECT * FROM orders WHERE owner = :v"}'
    parsed = json.loads(_sql_received(module, class_name, envelope, value))
    assert set(parsed) == {"index", "sql"} and parsed["index"] == "orders"
    strings = [t for t in Dialect.get_or_raise(dialect).tokenize(parsed["sql"])
               if t.token_type == TokenType.STRING]
    assert [t.text for t in strings] == [value], parsed["sql"]


@pytest.mark.parametrize("module,class_name,dialect", _ENVELOPE_SQL_CLIENTS)
def test_a_param_as_an_envelope_key_or_a_non_number_value_is_refused(module, class_name, dialect):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _recording_client(module, class_name)
    for envelope in ('{"index": "orders", "query": {"term": {":v": "x"}}}',
                     '{"index": "orders", "size": :v}'):
        with pytest.raises(ParamError, match="sql"):
            QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30).execute_query(
                envelope, params={"v": "a@x.com"})
    assert client.received == []


@pytest.mark.parametrize("module,class_name,dialect", _ENVELOPE_SQL_CLIENTS)
def test_a_param_in_an_envelope_dsl_string_renders_as_before(module, class_name, dialect):
    import json

    parsed = json.loads(_sql_received(module, class_name,
                                      '{"index": "orders", "query": {"term": {"owner": ":v"}}}', 'a"b'))
    assert set(parsed) == {"index", "query"}
    assert parsed["query"] == {"term": {"owner": "'a\"b'"}}


def test_the_identity_check_reads_the_sql_inside_an_envelope():
    """The SQL of an Elasticsearch envelope is checked by its structure: a
    value present but not restricting the rows is not a filter."""
    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    from app.ai.code_execution.identity_bind import CapturedQuery

    scoped = CapturedQuery('{"sql": "SELECT * FROM t WHERE owner = \'a@x.com\'"}', "ElasticsearchClient", "standard", True)
    widened = CapturedQuery('{"sql": "SELECT * FROM t WHERE owner = \'a@x.com\' OR 1 = 1"}',
                            "ElasticsearchClient", "standard", True)
    assert verify_identity_binds_in_queries([scoped], {"v": "a@x.com"}, specs) is None
    assert verify_identity_binds_in_queries([widened], {"v": "a@x.com"}, specs) is not None


# Every connector's literal rule, written out: a new connector must be added
# here on purpose, not inherit "standard" unseen. "standard" also covers the
# connectors whose query is a JSON document -- those refuse parameters by the
# document's shape (test_a_json_query_refuses_parameters). "+envelope": the
# SQL in the JSON envelope's "sql" field. None: parameters refused.
# MsFabricClient and SybaseClient import only where the ODBC driver is
# installed (CI); elsewhere their entries are simply not compared.
_EXPECTED_LITERAL_RULES = {
    "MsFabricClient": "standard", "SybaseClient": "doubled",
    "AnalysisServicesClient": "standard", "AppDynamicsClient": "standard", "AriaOperationsClient": "standard",
    "AwsAthenaClient": "standard", "AwsCloudWatchClient": "standard", "AwsCostClient": "standard",
    "AwsRedshiftClient": "doubled", "AzureBlobClient": "standard", "AzureCostClient": "standard",
    "AzureDataExplorerClient": None, "BigqueryClient": "backslash", "BitbucketRepoClient": "standard",
    "BrocadeClient": "standard", "BrowserClient": "standard", "BusinessObjectsClient": "standard",
    "CSVClient": "standard", "CassandraClient": "standard", "CheckPointClient": "standard",
    "CiscoApicClient": "standard", "ClickhouseClient": "doubled", "CosmosdbClient": "backslash",
    "CouchbaseClient": "backslash", "CrowdStrikeClient": "standard", "DataSourceClient": "standard",
    "DatabricksSqlClient": "backslash", "Db2Client": "standard", "DellOmeClient": "standard",
    "DocumentumClient": "standard", "DremioClient": "standard", "DruidClient": "standard",
    "DuckDBClient": "standard", "DynamodbClient": "doubled", "ElasticsearchClient": "standard+envelope",
    "F5BigIpClient": "standard", "GCSClient": "standard", "GitHubRepoClient": "standard",
    "GitLabRepoClient": "standard", "GitRepoClient": "standard", "GmailMailClient": "standard",
    "GoogleDriveClient": "standard", "GraphDriveClient": "standard", "GraphMailClient": "standard",
    "GraphOneNoteClient": "standard", "GraphTeamsClient": "standard", "HiveClient": "backslash",
    "HpeOneViewClient": "standard", "IbmiClient": "standard", "IcebergClient": "standard",
    "InforEpmClient": "standard", "InforOlapClient": "standard", "IntuneClient": "standard",
    "JaegerClient": "standard", "KmsLighthouseClient": "standard", "KubernetesClient": "standard",
    "MSSQLClient": "standard", "MariadbClient": "doubled", "MecmClient": "standard",
    "MondayClient": "standard", "MongodbClient": "standard", "MysqlClient": "doubled",
    "Neo4jClient": "standard", "NetAppOntapClient": "standard", "NetsuiteClient": "standard",
    "NetworkDirClient": "standard", "ObjectStoreClient": "standard", "OnedriveClient": "standard",
    "OnenoteClient": "standard", "OpenSearchClient": "doubled+envelope", "OracleBIClient": "doubled",
    "OracledbClient": "standard", "PBIXClient": "standard", "PinotClient": "standard",
    "PostHogClient": "doubled", "PostgresqlClient": "standard", "PowerBIClient": "standard",
    "PowerBIReportServerClient": "standard", "PrestoClient": "standard", "PriorityErpClient": "standard",
    "PrometheusClient": None, "QVDClient": "standard", "QlikSenseClient": "standard",
    "QlikSenseOnPremClient": "standard", "S3Client": "standard", "SalesforceClient": "backslash",
    "SapBwXmlaClient": "standard", "SapDatasphereClient": "standard", "SapHanaClient": "standard",
    "ServiceDemoClient": "standard", "ServiceNowClient": "standard", "SharepointClient": "standard",
    "SharepointListsClient": "standard", "SharepointOnpremClient": "standard", "SisenseClient": "standard",
    "SnowflakeClient": "doubled", "SparkConnectClient": "backslash", "SpectrumClient": "standard",
    "SplunkClient": None, "SqliteClient": "standard", "TableauClient": "standard",
    "TeradataClient": "standard", "TimbrA2aClient": "standard", "TimbrClient": "standard",
    "TrinoClient": "standard", "VCenterClient": "standard", "VerticaClient": "doubled",
    "XmlaClient": "standard", "ZabbixClient": "standard",
}


def test_the_identity_check_reads_no_envelope_sql_another_key_runs_instead_of():
    """Elasticsearch runs "esql" (and a dashboard / saved search) before
    "sql": the envelope's SQL is then not the executed statement."""
    from app.ai.code_execution.identity_bind import CapturedQuery

    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    for first in ('"esql": "FROM t"', '"dashboard": "space/title"', '"saved_search": "space/s"'):
        capture = CapturedQuery('{' + first + ', "sql": "SELECT * FROM t WHERE owner = \'a@x.com\'"}',
                                "ElasticsearchClient", "standard", True)
        assert verify_identity_binds_in_queries([capture], {"v": "a@x.com"}, specs) is not None, first


def test_every_query_client_has_the_literal_rule_listed_for_it():
    import importlib
    import inspect
    import pkgutil

    import app.data_sources.clients as package
    from app.data_sources.clients.base import DataSourceClient

    found = {}
    for info in pkgutil.iter_modules(package.__path__):
        try:
            module = importlib.import_module(f"{package.__name__}.{info.name}")
        except ImportError:
            continue  # a driver this environment lacks (ODBC)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, DataSourceClient) and cls.__module__ == module.__name__:
                rule = cls.sql_literal_style
                found[cls.__name__] = f"{rule}+envelope" if cls.sql_in_json_envelope else rule
    unlisted = sorted(set(found) - set(_EXPECTED_LITERAL_RULES))
    assert not unlisted, f"connectors with no literal rule listed here: {unlisted}"
    assert {n: found[n] for n in _EXPECTED_LITERAL_RULES if n in found} == {
        n: r for n, r in _EXPECTED_LITERAL_RULES.items() if n in found}


@pytest.mark.parametrize("module,class_name,dialect", _STANDARD_LITERAL_CLIENTS)
def test_engines_with_standard_literals_render_exactly_as_before(module, class_name, dialect):
    """Engines where a backslash is an ordinary character keep the doubled
    quote and nothing else."""
    cases = {
        "a backslash\\' before a quote, then more words": "'a backslash\\'' before a quote, then more words'",
        "ends with a backslash\\": "'ends with a backslash\\'",
        "it's": "'it''s'",
        "line one\nline two": "'line one\nline two'",
        "double \\\\ backslash": "'double \\\\ backslash'",
    }
    for value, literal in cases.items():
        assert _sql_received(module, class_name, "SELECT * FROM t WHERE a = :v", value) == (
            "SELECT * FROM t WHERE a = " + literal)


@pytest.mark.parametrize("module,class_name,dialect", _ALL_LITERAL_CLIENTS)
@pytest.mark.parametrize("value", ["O'Brien", "CORP\\jdoe"])
def test_an_identity_value_with_a_quote_still_verifies_as_applied(module, class_name, dialect, value):
    """The identity check reads the executed SQL in the engine's grammar (its
    sqlglot dialect, else one with the same literal rules as its style); a
    quote or a backslash in the identity value (a surname, a domain login)
    must still be found as the value the query filters on."""
    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    captured: list = []
    _sql_received(module, class_name, "SELECT * FROM t WHERE owner = :v", value,
                  captured=captured, keep_name=True)
    assert verify_identity_binds_in_queries(captured, {"v": value}, specs) is None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _specs(*specs):
    return [ParamSpec.model_validate(s) for s in specs]


def test_resolve_input_uses_request_then_default():
    specs = _specs({"name": "region", "type": "string", "default": "EMEA"})
    assert resolve_param_values(specs, {"region": "APAC"})["region"] == "APAC"
    assert resolve_param_values(specs, None)["region"] == "EMEA"


def test_resolve_unknown_param_rejected():
    specs = _specs({"name": "region", "type": "string"})
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"nope": 1})


def test_resolve_identity_locked_rejects_client_value():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"email": "attacker@example.com"}, FakeIdentity())


def test_resolve_identity_binds_email():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    out = resolve_param_values(specs, None, FakeIdentity())
    assert out["email"] == "sarah@example.com"


def test_resolve_identity_profile_attribute():
    specs = _specs({
        "name": "dept", "type": "string", "source": "identity",
        "identity_binding": "viewer.profile_attributes.department",
    })
    assert resolve_param_values(specs, None, FakeIdentity())["dept"] == "Sales"


def test_resolve_identity_groups_sorted():
    specs = _specs({
        "name": "grps", "type": "list", "source": "identity",
        "identity_binding": "viewer.groups",
    })
    assert resolve_param_values(specs, None, FakeIdentity())["grps"] == ["emea", "managers"]


def test_resolve_identity_default_overridable():
    specs = _specs({
        "name": "member", "type": "string", "source": "input_identity_default",
    })
    out = resolve_param_values(specs, {"member": "bob@example.com"}, FakeIdentity())
    assert out["member"] == "bob@example.com"
    out2 = resolve_param_values(specs, None, FakeIdentity())
    assert out2["member"] == "sarah@example.com"


def test_resolve_optional_none_means_all():
    specs = _specs({"name": "status", "type": "string", "required": False})
    assert resolve_param_values(specs, None)["status"] is None


def test_resolve_required_missing_raises():
    specs = _specs({"name": "member_id", "type": "id", "required": True})
    with pytest.raises(ParamError):
        resolve_param_values(specs, None)


def test_resolve_type_validation():
    specs = _specs({"name": "n", "type": "number"})
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"n": "not-a-number"})
    assert resolve_param_values(specs, {"n": "42"})["n"] == 42


def test_resolve_strict_options():
    specs = _specs({
        "name": "r", "type": "string", "options": ["a", "b"], "strict_options": True,
    })
    with pytest.raises(ParamError):
        resolve_param_values(specs, {"r": "c"})
    assert resolve_param_values(specs, {"r": "a"})["r"] == "a"


def test_no_specs_but_values_rejected_via_unknown():
    with pytest.raises(ParamError):
        resolve_param_values([], {"x": 1})


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def test_fingerprint_stable_and_order_independent():
    a = params_fingerprint({"x": 1, "y": "z"})
    b = params_fingerprint({"y": "z", "x": 1})
    assert a == b and len(a) == 64


def test_fingerprint_empty_is_blank():
    assert params_fingerprint({}) == ""
    assert params_fingerprint(None) == ""


# ---------------------------------------------------------------------------
# Code checks
# ---------------------------------------------------------------------------

CODE_OK = """
def generate_df(ds_clients, excel_files, params):
    sql = "SELECT * FROM tasks WHERE assignee = :email"
    return ds_clients["a:b"].execute_query(sql, params={"email": params["email"]})
"""

CODE_FSTRING = """
def generate_df(ds_clients, excel_files, params):
    sql = f"SELECT * FROM tasks WHERE assignee = '{params['email']}'"
    return ds_clients["a:b"].execute_query(sql)
"""


def test_extract_and_accepts():
    assert code_accepts_params(CODE_OK)
    assert extract_code_param_names(CODE_OK) == {"email"}
    assert not code_accepts_params("def generate_df(ds_clients, excel_files):\n    return None")


def test_fstring_guard():
    assert check_params_not_formatted(CODE_OK) is None
    assert check_params_not_formatted(CODE_FSTRING) is not None


def test_declarations_vs_code():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    assert check_declarations_vs_code(CODE_OK, specs) == []
    extra = _specs(
        {"name": "email", "type": "string"},
        {"name": "dead", "type": "string"},
    )
    errs = check_declarations_vs_code(CODE_OK, extra)
    assert any("dead" in e for e in errs)
    errs2 = check_declarations_vs_code(
        "def generate_df(ds_clients, excel_files):\n    return None", specs
    )
    assert errs2


# ---------------------------------------------------------------------------
# Identity verification against executed SQL
# ---------------------------------------------------------------------------

def test_identity_verification_pass_and_fail():
    specs = _specs({"name": "email", "type": "string", "source": "identity"})
    values = {"email": "sarah@example.com"}
    ok = verify_identity_binds_in_queries(
        ["SELECT * FROM t WHERE assignee = 'sarah@example.com'"], values, specs
    )
    assert ok is None
    bad = verify_identity_binds_in_queries(
        ["SELECT * FROM t"], values, specs
    )
    assert bad is not None


def test_parse_param_specs_skips_garbage():
    specs = parse_param_specs([
        {"name": "ok", "type": "string"},
        {"type": "missing-name"},
        "not-a-dict",
    ])
    assert [s.name for s in specs] == ["ok"]


@pytest.mark.parametrize("value", ["2027-02-29", "2024-04-31", "2024-01-01T25:00:00", "2024-01-01T12:60:00", "2024-01-01T12:00:00+25:00"])
def test_date_parameters_reject_impossible_calendar_values(value):
    with pytest.raises(ParamError):
        resolve_param_values([ParamSpec(name="period", type="date")], {"period": value})


def test_date_range_resolution_preserves_bounds_and_discards_ui_metadata():
    default = {
        "from": "2024-06-01",
        "to": "2024-06-01T12:00:00Z",
        "preset": "custom",
        "timezone": "UTC",
    }
    requested = {
        "from": "2024-06-02T12:00:00+02:00",
        "to": "2024-06-02T10:00:00.000000001Z",
        "label": "Last day",
        "timezone": "Europe/Paris",
    }
    specs = [ParamSpec(name="period", type="date_range", default=default)]

    from_default = resolve_param_values(specs, None)["period"]
    from_request = resolve_param_values(specs, {"period": requested})["period"]

    assert from_default == {key: default[key] for key in ("from", "to")}
    assert from_request == {key: requested[key] for key in ("from", "to")}


@pytest.mark.parametrize("bounds", [
    {"from": "2024-06-02", "to": "2024-06-01"},
    {"from": "2024-06-01", "to": "2024-06-01T12:00:00Z"},
    {"from": "2024-06-01T12:00:00", "to": "2024-06-01T13:00:00Z"},
])
def test_legacy_date_ranges_resolve_unchanged_from_defaults_and_requests(bounds):
    spec = ParamSpec(name="period", type="date_range", default=bounds)
    expected = {key: bounds[key] for key in ("from", "to") if bounds.get(key) is not None}

    assert resolve_param_values([spec], None)["period"] == expected
    assert resolve_param_values([ParamSpec(name="period", type="date_range")], {"period": bounds})["period"] == expected


@pytest.mark.parametrize("access", ["params['window']", "params.get('window')", "params.get('window') or {}"])
def test_declared_date_ranges_cannot_silently_read_wrong_bound_names(access):
    code = f"def generate_df(ds_clients, excel_files, params):\n    window = {access}\n    return window.get('start'), window.get('end')"
    assert check_declarations_vs_code(code, [ParamSpec(name="window", type="date_range")])


@pytest.mark.parametrize(("value", "expected"), [
    ({"from": "2024-02-29", "to": "2024-03-31"}, ("2024-02-29", "2024-04-01")),
    ({"from": "2024-12-31", "to": "2024-12-31"}, ("2024-12-31", "2025-01-01")),
    ({"to": "2024-02-29"}, (None, "2024-03-01")),
    ({"from": "2024-02-29"}, ("2024-02-29", None)),
    (None, (None, None)),
])
def test_calendar_date_bounds_return_inclusive_start_and_exclusive_next_day(value, expected):
    assert calendar_date_bounds(value) == expected


@pytest.mark.parametrize("value", [
    {"from": "2024-02-29T00:00:00", "to": "2024-02-29"},
    {"from": "2024-02-29T00:00:00+02:00"},
])
def test_calendar_date_bounds_reject_timestamp_bounds(value):
    with pytest.raises(ParamError):
        calendar_date_bounds(value)


def test_calendar_date_bounds_reject_reversed_calendar_dates():
    with pytest.raises(ParamError):
        calendar_date_bounds({"from": "2024-06-02", "to": "2024-06-01"})


@pytest.mark.parametrize("value", [
    "2024-02-29T12:34:56.123456+05:30",
    "2024-02-29T12:34:56.123456-0530",
    "2024-02-29 12:34:56.123456",
    "2024-02-29T12:34:56",
])
def test_valid_timestamp_parameters_preserve_original_precision_and_offset(value):
    resolved = resolve_param_values([ParamSpec(name="period", type="date")], {"period": value})
    assert resolved["period"] == value


def test_python_temporal_values_normalize_to_iso_strings():
    values = resolve_param_values(
        [ParamSpec(name="day", type="date"), ParamSpec(name="instant", type="date")],
        {"day": date(2024, 2, 29), "instant": datetime.fromisoformat("2024-02-29T12:34:56.123456+05:30")},
    )
    assert values == {"day": "2024-02-29", "instant": "2024-02-29T12:34:56.123456+05:30"}


def test_date_range_check_ignores_unrelated_dicts_and_reassigned_aliases():
    code = """\
def generate_df(ds_clients, excel_files, params):
    window = params.get("window") or {}
    window = {"start": "local", "end": "local"}
    unrelated = {"start": "local", "end": "local"}
    return window.get("start"), unrelated.get("end")
"""
    assert check_declarations_vs_code(code, [ParamSpec(name="window", type="date_range")]) == []


def test_date_range_check_ignores_non_range_parameter_aliases():
    code = """\
def generate_df(ds_clients, excel_files, params):
    window = params.get("window") or {}
    return window.get("start")
"""
    specs = [ParamSpec(name="window", type="string")]
    assert check_declarations_vs_code(code, specs) == []


def test_execution_stream_rejects_wrong_date_range_keys_before_execution():
    from app.ai.code_execution.code_execution import StreamingCodeExecutor
    from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

    code = """\
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    window = params.get("window") or {}
    start = window.get("start")
    return pd.DataFrame({"start": [start]})
"""

    async def codegen(**kwargs):
        return code

    executor = StreamingCodeExecutor()

    async def collect():
        return [event async for event in executor.generate_and_execute_stream_v2(
            request=CodeGenRequest(
                context=CodeGenContext(user_prompt="filter", schemas_excerpt=""),
                retries=1,
            ),
            ds_clients={},
            excel_files=[],
            code_generator_fn=codegen,
            param_specs=[ParamSpec(name="window", type="date_range")],
            # The guard must work even when the optional parameter has no value.
            params=None,
        )]

    events = asyncio.run(collect())
    done = next(event["payload"] for event in events if event["type"] == "done")
    assert done["df"] is None
    assert any("date_range uses 'from'/'to'" in message for _, message in done["errors"])


@pytest.mark.parametrize("offset", ["+01:60", "-0160", "+24:00"])
def test_timestamp_parameters_reject_invalid_offset_components(offset):
    with pytest.raises(ParamError):
        resolve_param_values([ParamSpec(name="instant", type="date")], {"instant": "2024-02-29T12:00:00" + offset})


@pytest.mark.parametrize("fraction", ["123456789", "12345678901234567890123456789"])
def test_timestamp_comparison_preserves_fractional_precision_across_offsets(fraction):
    from app.ai.code_execution.query_params import date_range_issue, param_values_equal
    spec = ParamSpec(name="instant", type="date")
    expected = "2024-02-29T12:00:00." + fraction + "Z"
    assert param_values_equal(spec, expected, "2024-02-29T14:00:00." + fraction + "+02:00")
    different = "2024-02-29T12:00:00." + fraction[:-1] + "8Z"
    assert not param_values_equal(spec, expected, different)
    bounds = {
        "from": "2024-02-29T14:00:00." + fraction + "+02:00",
        "to": different,
    }
    resolved = resolve_param_values([ParamSpec(name="period", type="date_range")], {"period": bounds})["period"]
    assert resolved == bounds
    assert date_range_issue(resolved) == "reversed_date_range"


@pytest.mark.parametrize("bounds", [
    {"from": "2024-06-01", "to": "2024-06-01T12:00:00Z"},
    {"from": "2024-06-01T12:00:00", "to": "2024-06-01T13:00:00Z"},
])
def test_date_range_issue_marks_mixed_conventions_without_rejecting_resolution(bounds):
    from app.ai.code_execution.query_params import date_range_issue

    resolved = resolve_param_values([ParamSpec(name="period", type="date_range")], {"period": bounds})["period"]
    assert resolved == bounds
    assert date_range_issue(resolved) == "mixed_date_range"


def test_date_range_issue_accepts_open_bounds_and_compares_timezone_equivalent_instants():
    from app.ai.code_execution.query_params import date_range_issue

    assert date_range_issue({"from": "2024-06-01T10:00:00.123456789Z"}) is None
    assert date_range_issue({
        "from": "2024-06-01T12:00:00.123456789+02:00",
        "to": "2024-06-01T10:00:00.123456789Z",
    }) is None


@pytest.mark.parametrize("bounds", [
    {"from": "2024-02-30"},
    {"from": "2024-01-01", "to": "not-a-date"},
])
def test_date_range_issue_identifies_invalid_bounds(bounds):
    from app.ai.code_execution.query_params import date_range_issue

    assert date_range_issue(bounds) == "invalid_date_range"


# Identity verification must hold for EVERY table-reading query and must not
# be satisfiable by an echoed literal or a comment.
_ID_SPECS = [{"name": "email", "type": "string", "source": "identity"}]


@pytest.mark.parametrize("email", ["sarah@example.com", "o'neil@corp.io"])
@pytest.mark.parametrize("queries", [
    # value echoed in the SELECT list, never filtered
    ["SELECT '{v}' AS who, * FROM orders"],
    # value only in a comment
    ["SELECT * FROM orders -- WHERE assignee = '{v}'"],
    ["SELECT * FROM orders /* WHERE assignee = '{v}' */"],
    # one scoped query does not excuse an unscoped one
    ["SELECT * FROM orders WHERE assignee = '{v}'", "SELECT * FROM salaries"],
    # dummy statement carrying the value next to an unscoped read
    ["SELECT '{v}'", "SELECT * FROM orders"],
])
def test_identity_verification_rejects_unscoped_reads(email, queries):
    v = email.replace("'", "''")
    specs = _specs(*_ID_SPECS)
    rendered = [q.format(v=v) for q in queries]
    assert verify_identity_binds_in_queries(rendered, {"email": v}, specs) is not None


@pytest.mark.parametrize("queries", [
    ["SELECT * FROM orders WHERE assignee = '{v}'"],
    ["SELECT * FROM orders o JOIN users u ON u.email = '{v}' AND u.id = o.uid"],
    ["WITH mine AS (SELECT * FROM orders WHERE assignee = '{v}') SELECT * FROM mine WHERE 1=1"],
    # a statement that reads no table can't leak rows
    ["SELECT 1", "SELECT * FROM orders WHERE assignee = '{v}' -- scoped"],
    # non-SQL (structured connector) captures fall back to containment
    ['execute_query({{"filter": {{"owner": "{v}"}}}})'],
])
def test_identity_verification_accepts_scoped_reads(queries):
    v = "sarah@example.com"
    specs = _specs(*_ID_SPECS)
    assert verify_identity_binds_in_queries([q.format(v=v) for q in queries], {"email": v}, specs) is None


def test_identity_verification_ignores_specs_without_values():
    specs = _specs(*_ID_SPECS)
    assert verify_identity_binds_in_queries([], {"email": None}, specs) is None


# ---------------------------------------------------------------------------
# missing_required_inputs: what Publish, a refresh and a reader's dashboard run
# never run without (the skip rule of a context-driven query).
# ---------------------------------------------------------------------------

def _required_input(name, **extra):
    return ParamSpec(name=name, type="number", required=True, source="input", **extra)


def test_missing_required_inputs_names_only_required_inputs_without_a_default():
    from app.ai.code_execution.query_params import missing_required_inputs

    specs = [
        ParamSpec(name="viewer", type="string", required=True, source="identity",
                  identity_binding="viewer.email"),
        ParamSpec(name="dept", type="string", required=True, source="input_identity_default",
                  identity_binding="viewer.email"),
        ParamSpec(name="genre", type="string", required=False, source="input"),
        _required_input("region", default=3),
        # A falsy default is still a default: a naive `not s.default` would call these missing.
        _required_input("zero", default=0),
        ParamSpec(name="blank", type="string", required=True, source="input", default=""),
        _required_input("customer_id"),
    ]
    # Identity-sourced, optional and defaulted params are never "missing".
    assert missing_required_inputs(specs, None) == ["customer_id"]
    assert missing_required_inputs(specs, {}) == ["customer_id"]


@pytest.mark.parametrize("value", [[], 0, "", False, 0.0])
def test_missing_required_inputs_treats_only_none_or_absent_as_missing(value):
    from app.ai.code_execution.query_params import missing_required_inputs

    specs = [_required_input("customer_id")]
    assert missing_required_inputs(specs, {"customer_id": value}) == []
    assert missing_required_inputs(specs, {"customer_id": None}) == ["customer_id"]
    assert missing_required_inputs(specs, {"other": 14}) == ["customer_id"]


def test_missing_required_inputs_names_each_missing_one_in_declaration_order():
    from app.ai.code_execution.query_params import missing_required_inputs

    specs = [_required_input("b_second"), _required_input("a_first"), _required_input("c_stored")]
    assert missing_required_inputs(specs, {"c_stored": 27}) == ["b_second", "a_first"]
    assert missing_required_inputs(specs, {"b_second": 1, "a_first": 2, "c_stored": 3}) == []
    assert missing_required_inputs([], None) == []
