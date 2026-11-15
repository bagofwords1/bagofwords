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
from tests.unit.test_splunk_client import metering_on  # noqa: F401  (fixture)


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
# Rendering per connection: each engine's own string literal, refusals where
# no rendering is declared or the placeholder sits inside a string
# ---------------------------------------------------------------------------

import sys

import pandas as pd

from app.ai.code_execution import query_params
from app.ai.code_execution.client_profiles import guard_profile
from app.data_sources.clients.discovery import DiscoveryClient

_LINUX = sys.platform.startswith("linux")


@pytest.mark.parametrize("sql, client_type_name", [
    ("WHERE a = ':v'", ""),
    ("WHERE a = 'x :v y'", ""),
    ("WHERE a = 'it''s :v'", ""),
    ("WHERE a = $$ :v $$", "PostgresqlClient"),
    ("WHERE a = 'x\\' :v'", "MysqlClient"),
    ("WHERE a = N':v'", "MSSQLClient"),
])
def test_placeholder_inside_a_quoted_string_is_refused(sql, client_type_name):
    with pytest.raises(ParamError):
        render_sql_with_params(sql, {"v": "x"}, client_type_name=client_type_name)


def test_placeholder_inside_a_quoted_spl_value_is_refused():
    spl = query_params.renderer_for_name("SplunkClient")
    with pytest.raises(ParamError):
        render_sql_with_params('search index=web user=":v"', {"v": "x"}, renderer=spl)
    assert render_sql_with_params('search index=web note=":" user=:v', {"v": "x"}, renderer=spl) == \
        'search index=web note=":" user="x"'


def test_placeholder_next_to_a_quoted_string_still_renders():
    out = render_sql_with_params("WHERE a = 'x:' AND b = :v AND c = 'y'", {"v": "it's"})
    assert out == "WHERE a = 'x:' AND b = 'it''s' AND c = 'y'"


def test_a_json_envelope_with_params_is_refused_for_splunk():
    spl = query_params.renderer_for_name("SplunkClient")
    with pytest.raises(ParamError):
        render_sql_with_params('{"spl": "search user=:v"}', {"v": "x"}, renderer=spl)


def _bare(cls_path):
    import importlib

    module, _, name = cls_path.rpartition(".")
    return object.__new__(getattr(importlib.import_module(module), name))


@pytest.mark.parametrize("cls_path, renderer_name", [
    ("app.data_sources.clients.oracle_bi_client.OracleBIClient", "ansi_or_backslash"),
    ("app.data_sources.clients.sisense_client.SisenseClient", "ansi"),
    ("app.data_sources.clients.netsuite_client.NetsuiteClient", "ansi"),
])
def test_ansi_connections_render_as_today_and_stay_unguarded(cls_path, renderer_name):
    client = _bare(cls_path)
    renderer = query_params.renderer_for(client)
    assert renderer.name == renderer_name
    assert guard_profile(client) is None
    assert render_sql_with_params("Name eq :v", {"v": "it's"}, renderer=renderer) == "Name eq 'it''s'"


@pytest.mark.parametrize("cls_path, renderer_name", [
    ("app.data_sources.clients.vertica_client.VerticaClient", "ansi_or_backslash"),
    ("app.data_sources.clients.sybase_client.SybaseClient", "ansi_or_backslash"),
    ("app.data_sources.clients.sap_hana_client.SapHanaClient", "ansi"),
    ("app.data_sources.clients.pinot_client.PinotClient", "ansi"),
])
def test_ansi_sql_connections_without_a_parser_dialect_render_as_today_and_are_guarded(cls_path, renderer_name):
    client = _bare(cls_path)
    renderer = query_params.renderer_for(client)
    assert renderer.name == renderer_name
    assert guard_profile(client).kind == "nonparsing_sql"
    assert render_sql_with_params("WHERE a = :v", {"v": "it's"}, renderer=renderer) == "WHERE a = 'it''s'"


# Engines that may also read a backslash as an escape inside a string
# (their sql_literal_style is "doubled"): a backslash value is refused, and
# a placeholder that a backslash reading puts inside a string is refused.
@pytest.mark.parametrize("cls_path", [
    "app.data_sources.clients.vertica_client.VerticaClient",
    "app.data_sources.clients.sybase_client.SybaseClient",
    "app.data_sources.clients.oracle_bi_client.OracleBIClient",
])
def test_ansi_connections_that_may_read_backslash_escapes_refuse_a_backslash(cls_path):
    renderer = query_params.renderer_for(_bare(cls_path))
    for value in ("ends with a backslash\\", "x\\' OR 1=1 --"):
        with pytest.raises(ParamError, match="backslash"):
            render_sql_with_params("WHERE a = :v", {"v": value}, renderer=renderer)
    with pytest.raises(ParamError):
        render_sql_with_params("WHERE a = 'x\\' AND b = :v OR c = ''", {"v": "y"}, renderer=renderer)


ODATA_CLIENTS = [
    "app.data_sources.clients.priority_erp_client.PriorityErpClient",
    "app.data_sources.clients.sap_datasphere_client.SapDatasphereClient",
    "app.data_sources.clients.mecm_client.MecmClient",
]


@pytest.mark.parametrize("cls_path", ODATA_CLIENTS)
def test_odata_connections_percent_encode_the_literal_body_and_stay_unguarded(cls_path):
    client = _bare(cls_path)
    renderer = query_params.renderer_for(client)
    assert renderer.name == "odata"
    assert guard_profile(client) is None
    assert render_sql_with_params("$filter=Name eq :v", {"v": "it's"}, renderer=renderer) == \
        "$filter=Name eq 'it%27%27s'"


# Values holding what the server decodes or splits the options text on.
ODATA_VALUES = ["%27", "a&$top=1", "a#b", "a+b", "it's", "x%27 or 1 eq 1 or Name eq %27y", "50%", "a=b", "%2527"]


@pytest.mark.parametrize("value", ODATA_VALUES)
@pytest.mark.parametrize("cls_path", ODATA_CLIENTS)
def test_the_odata_server_reads_a_rendered_value_as_that_value_in_one_literal(cls_path, value):
    import re
    from urllib.parse import parse_qs

    renderer = query_params.renderer_for(_bare(cls_path))
    options = render_sql_with_params("$filter=Name eq :v and Id gt 5&$top=10", {"v": value}, renderer=renderer)
    # The server's reading: options split at '&', each value percent-decoded.
    assert parse_qs(options, keep_blank_values=True, strict_parsing=True) == {
        "$filter": ["Name eq '" + value.replace("'", "''") + "' and Id gt 5"],
        "$top": ["10"],
    }
    body = options[options.index("'") + 1:options.rindex("'")]
    assert re.fullmatch(r"[A-Za-z0-9_.~%-]*", body), options  # no '#', '&', '+' or quote as written


def test_timbr_doubles_quotes_and_refuses_a_backslash_and_is_guarded_as_spark_sql():
    from app.data_sources.clients.timbr_client import TimbrClient

    client = object.__new__(TimbrClient)
    renderer = query_params.renderer_for(client)
    assert renderer.name == "timbr"
    assert (guard_profile(client).kind, guard_profile(client).dialect) == ("sql", "spark")
    assert render_sql_with_params("WHERE a = :v", {"v": "it's"}, renderer=renderer) == "WHERE a = 'it''s'"
    with pytest.raises(ParamError):
        render_sql_with_params("WHERE a = :v", {"v": "a" + chr(92) + "b"}, renderer=renderer)


# A placeholder is rendered only where it stands as code. In a comment, a
# quoted identifier, or a string some reader of the text may see (an E'..'
# with a backslash, a q'..' quote, a verbatim or multi-line string, the text
# an OData server decodes) the value would land somewhere else: refused.
B = chr(92)
MISPLACED = [
    ("PostgresqlClient", "SELECT 1 -- :v"),
    ("PostgresqlClient", "SELECT 1 /* :v */"),
    ("PostgresqlClient", 'SELECT ":v"'),
    ("PostgresqlClient", "SELECT E'a" + B + "' AND :v = '1'"),
    ("PostgresqlClient", "SELECT 1 /* /* */ :v */"),
    ("DuckDBClient", "SELECT E'a" + B + "' AND :v = '1'"),
    ("FastQueryClient", "SELECT 1 -- :v"),
    ("AwsRedshiftClient", "SELECT E'a" + B + "' AND :v = '1'"),
    ("SqliteClient", 'SELECT ":v"'),
    ("SqliteClient", "SELECT [:v]"),
    ("SqliteClient", "SELECT `:v`"),
    ("MysqlClient", "SELECT 1 # :v"),
    ("MysqlClient", "SELECT `:v`"),
    ("MysqlClient", "SELECT /*! 1 */ :v"),
    ("MSSQLClient", "SELECT [:v]"),
    ("BigqueryClient", "SELECT '':v"),
    ("BigqueryClient", "SELECT ''' :v '''"),
    ("OracledbClient", "SELECT q'[ ' :v ' ]' FROM dual"),
    ("SnowflakeClient", "SELECT $$ :v $$"),
    ("SparkConnectClient", "SELECT `:v`"),
    ("VerticaClient", "SELECT 1 -- :v"),
    ("VerticaClient", "SELECT 1 /* :v */"),
    ("VerticaClient", 'SELECT ":v"'),
    ("VerticaClient", "SELECT E'a" + B + "' AND :v = '1'"),
    ("VerticaClient", "SELECT $$ :v $$"),
    ("SybaseClient", "SELECT [:v]"),
    ("Db2Client", "SELECT 1 /* /* */ :v */"),
    ("AzureDataExplorerClient", "T | where c == 'a' // :v"),
    ("AzureDataExplorerClient", "T | where c == ```x :v```"),
    ("AzureDataExplorerClient", "T | where c == @'a" + B + "' or c == ' :v '"),
    ("AzureDataExplorerClient", "T | where c == h@'a" + B + "' or c == ' :v '"),
    ("SplunkClient", "search index=x ``` :v ```"),
    ("SplunkClient", "search index=x | eval c=':v'"),
    ("SplunkClient", "search index=x a" + B + '" b " :v "'),
    ("TimbrClient", "SELECT `:v`"),
    ("TimbrClient", "SELECT 'a" + B + "' AND :v = '1'"),
    ("MecmClient", "$filter=Name eq %27 :v %27"),
    ("PriorityErpClient", "ORDERS?$filter=A eq 'x&$top=' :v '"),
    ("SapDatasphereClient", "$filter=A eq 'x':v"),
    # Read as a name or a string by some engine behind the ANSI renderer:
    # Sybase's [..]] ..] and SuiteQL's q'[..]'.
    ("SybaseClient", "SELECT [a]] :v ]"),
    ("NetsuiteClient", "SELECT q'[ ' :v ' ]' FROM dual"),
    # A MySQL user variable name, not a value.
    ("MysqlClient", "SELECT @:v"),
]


@pytest.mark.parametrize("family, template", MISPLACED)
def test_a_placeholder_that_is_not_code_is_refused(family, template):
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"v": "x"}, renderer=query_params.renderer_for_name(family))


@pytest.mark.parametrize("family, template, expected", [
    ("PostgresqlClient", "SELECT x::text FROM t WHERE a = :v -- owner filter",
     "SELECT x::text FROM t WHERE a = 'x' -- owner filter"),
    ("SqliteClient", 'SELECT "a" FROM t WHERE "a" = :v', "SELECT \"a\" FROM t WHERE \"a\" = 'x'"),
    ("BigqueryClient", "SELECT a FROM t WHERE a = :v AND b = ''", "SELECT a FROM t WHERE a = 'x' AND b = ''"),
    ("VerticaClient", "SELECT 1 FROM t WHERE a = :v /* c */", "SELECT 1 FROM t WHERE a = 'x' /* c */"),
    ("AzureDataExplorerClient", "T | where c == :v // owner", "T | where c == 'x' // owner"),
    ("SplunkClient", "search index=x ``` scoped ``` user=:v", 'search index=x ``` scoped ``` user="x"'),
    ("MecmClient", "$filter=Name eq :v&$top=5", "$filter=Name eq 'x'&$top=5"),
    ("TimbrClient", "SELECT `a` FROM t WHERE a = :v", "SELECT `a` FROM t WHERE a = 'x'"),
])
def test_a_placeholder_that_is_code_still_renders(family, template, expected):
    assert render_sql_with_params(template, {"v": "x"}, renderer=query_params.renderer_for_name(family)) == expected


# On the engines themselves: the literal written where such a placeholder
# stands reads every row; the same value where a value goes matches none.
NL = chr(10)
_ROWS = [(1, "alice"), (2, "bob"), (3, "carol")]
ENGINE_CASES = [
    ("duckdb", "CSVClient", "SELECT id FROM t WHERE owner = 'nobody' -- :v", "x" + NL + "OR 1=1 --"),
    ("duckdb", "CSVClient", "SELECT id FROM t WHERE owner = E'a" + B + "' AND :v = '1'", " OR 1=1 --"),
    ("duckdb", "CSVClient", "SELECT id FROM t WHERE owner = 'nobody' /* :v */", "*/ OR 1=1 --"),
    ("sqlite", "SqliteClient", 'SELECT id FROM t WHERE owner = ":v"', 'x" OR 1=1 --'),
    ("sqlite", "SqliteClient", "SELECT id FROM t WHERE owner = 'nobody' -- :v", "x" + NL + "OR 1=1 --"),
    ("postgres", "PostgresqlClient", "SELECT id FROM t WHERE owner = E'a" + B + "' AND :v = '1'", " OR 1=1 --"),
    ("postgres", "PostgresqlClient", "SELECT id FROM t WHERE owner = 'nobody' -- :v", "x" + NL + "OR 1=1 --"),
]


# id, score, is_public
_SCORES = [(1, 200, 1), (2, 150, 0), (3, 50, 1)]


def _fill(execute, executemany, mark):
    execute("CREATE TABLE t (id INT, owner TEXT)")
    executemany(f"INSERT INTO t VALUES ({mark}, {mark})", _ROWS)
    execute("CREATE TABLE s (id INT, score INT, is_public INT)")
    executemany(f"INSERT INTO s VALUES ({mark}, {mark}, {mark})", _SCORES)


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def postgres_rows():
    """A run_query over a throwaway Postgres server holding _ROWS."""
    if not _docker_available():
        pytest.skip("Docker daemon not reachable")
    import psycopg2
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15") as pg:
        con = psycopg2.connect(host=pg.get_container_host_ip(), port=pg.get_exposed_port(5432),
                               user=pg.username, password=pg.password, dbname=pg.dbname)
        con.autocommit = True
        cur = con.cursor()
        _fill(cur.execute, cur.executemany, "%s")

        def run(sql):
            cur.execute(sql.replace("%", "%%"))
            return sorted(r[0] for r in cur.fetchall())

        yield run
        con.close()


def _engine(name, request):
    if name == "postgres":
        return request.getfixturevalue("postgres_rows")
    if name == "duckdb":
        import duckdb

        con = duckdb.connect()
        _fill(con.execute, con.executemany, "?")
        return lambda sql: sorted(r[0] for r in con.execute(sql).fetchall())
    import sqlite3

    con = sqlite3.connect(":memory:")
    _fill(con.execute, con.executemany, "?")
    return lambda sql: sorted(r[0] for r in con.execute(sql).fetchall())


@pytest.mark.parametrize("engine, family, template, value", ENGINE_CASES)
def test_a_placeholder_the_engine_reads_outside_code_is_refused_before_anything_is_sent(
        request, engine, family, template, value):
    run = _engine(engine, request)
    renderer = query_params.renderer_for_name(family)
    assert run(template.replace(":v", renderer.quote(value))) == [1, 2, 3]
    assert run(render_sql_with_params("SELECT id FROM t WHERE owner = :v", {"v": value}, renderer=renderer)) == []
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"v": value}, renderer=renderer)


# A value must not join the template text next to it. Written right after a
# '-', a negative number would make '--': a line comment that drops the rest
# of the filter.
SCORE_FILTER = "SELECT id FROM s WHERE score >= 100-:p AND is_public = 1"


@pytest.mark.parametrize("engine, family", [
    ("duckdb", "CSVClient"), ("sqlite", "SqliteClient"), ("postgres", "PostgresqlClient"),
])
def test_a_negative_number_after_a_minus_keeps_the_rest_of_the_filter(request, engine, family):
    run = _engine(engine, request)
    renderer = query_params.renderer_for_name(family)
    assert run(SCORE_FILTER.replace(":p", "-5")) == [1, 2]  # joined: the rest of the filter is a comment
    rendered = render_sql_with_params(SCORE_FILTER, {"p": -5}, renderer=renderer)
    assert rendered == "SELECT id FROM s WHERE score >= 100- -5 AND is_public = 1"
    assert run(rendered) == [1]


def test_a_negative_number_in_a_function_argument_stays_that_argument():
    import sqlglot

    rendered = render_sql_with_params("SELECT DATEADD(day, -:days, d) FROM t", {"days": -5},
                                      renderer=query_params.renderer_for_name("MSSQLClient"))
    assert rendered == "SELECT DATEADD(day, - -5, d) FROM t"
    tokens = sqlglot.tokenize(rendered, read="tsql")
    assert [t.text for t in tokens] == ["SELECT", "DATEADD", "(", "day", ",", "-", "-", "5", ",", "d", ")", "FROM", "t"]
    assert not any(t.comments for t in tokens)


@pytest.mark.parametrize("family", ["VerticaClient", "AzureDataExplorerClient", "SplunkClient", "SalesforceClient"])
def test_a_negative_number_after_a_minus_is_spaced_for_scanned_grammars_too(family):
    renderer = query_params.renderer_for_name(family)
    assert render_sql_with_params("a >= 100-:p and b", {"p": -5}, renderer=renderer) == "a >= 100- -5 and b"
    assert render_sql_with_params("a >= 100 - :p and b", {"p": -5}, renderer=renderer) == "a >= 100 - -5 and b"
    assert render_sql_with_params("a >= 100-:p and b", {"p": -0.5}, renderer=renderer) == "a >= 100- -0.5 and b"


# Only some characters may stand right before a placeholder: start of text,
# a space, an operator or an opening bracket. Others change how the literal
# is read: KQL's @'..' escapes nothing, SPL's backslash takes the quote,
# MySQL's @ names a variable, a quote or a letter joins the literal.
ALWAYS_REFUSED_BEFORE = set("@" + B + "$#`'\".?{})")
# After a name character or a colon there is no placeholder at all (a:v, x::v).
NOT_A_PLACEHOLDER_AFTER = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:")
ACCEPTED_BEFORE = set(" =(,")
OPERATORS_BEFORE = set("<>!+-*/|%^&~;")
PRINTABLE = [chr(c) for c in range(32, 127)] + ["\t", "\n"]
# A value that would leave its literal wherever it is misread.
TRICKY = "a'b\"c" + B + "d -- e /* f // g"


def _readable_value(renderer):
    try:
        renderer.quote(TRICKY)
        return TRICKY
    except ParamError:
        return TRICKY.replace(B, "")  # Athena, Redshift and Timbr refuse a backslash


def _one_literal(renderer, rendered, start, end):
    """Whether rendered[start:end] is read as exactly one string literal and
    nothing next to it is read differently: the dialect's tokenizer, or every
    span of the scanned grammar."""
    import sqlglot

    dialect = query_params._sqlglot_dialect_of(renderer)
    if dialect is not None:
        tokens = sqlglot.tokenize(rendered, read=dialect)
        inside = [t for t in tokens if t.end >= start and t.start < end]
        return (not any(t.comments for t in tokens)
                and [(t.token_type.name in query_params._STRING_TOKEN_NAMES, t.start, t.end + 1) for t in inside]
                == [(True, start, end)])
    spans = [s for s in renderer.string_spans(rendered) if s[1] > start and s[0] < end]
    return spans == [(start, end)]


def _no_span_or_comment(renderer, rendered, start, end):
    """Whether rendered[start:end] (a number) is code and nothing joins it."""
    import sqlglot

    dialect = query_params._sqlglot_dialect_of(renderer)
    if dialect is not None:
        tokens = sqlglot.tokenize(rendered, read=dialect)
        return not any(t.comments for t in tokens) and [t.text for t in tokens if t.end >= start and t.start < end] == ["-", "5"]
    return not [s for s in renderer.string_spans(rendered) if s[1] >= start and s[0] <= end]


@pytest.mark.parametrize("name", sorted(query_params._RENDERERS))
def test_each_character_before_a_placeholder_is_refused_or_leaves_the_value_one_literal(name):
    renderer = query_params._RENDERERS[name]
    value = _readable_value(renderer)
    literal = renderer.quote(value)
    accepted = set()
    for c in PRINTABLE:
        template = "x " + c + ":v y"
        if c in NOT_A_PLACEHOLDER_AFTER:
            assert render_sql_with_params(template, {"v": value}, renderer=renderer) == template
            continue
        try:
            rendered = render_sql_with_params(template, {"v": value}, renderer=renderer)
        except ParamError:
            assert c not in ACCEPTED_BEFORE, (name, c)
            continue
        accepted.add(c)
        assert c not in ALWAYS_REFUSED_BEFORE, (name, c, rendered)
        assert rendered == "x " + c + literal + " y", (name, c)
        assert _one_literal(renderer, rendered, 3, 3 + len(literal)), (name, c, rendered)
        number = render_sql_with_params(template, {"v": -5}, renderer=renderer)
        start = number.index("-5")
        assert _no_span_or_comment(renderer, number, start, start + 2), (name, c, number)
    assert ACCEPTED_BEFORE <= accepted, (name, sorted(ACCEPTED_BEFORE - accepted))
    if name != "odata":  # the server reads an option name or a path after these
        assert OPERATORS_BEFORE <= accepted, (name, sorted(OPERATORS_BEFORE - accepted))


def test_a_unicode_escape_prefix_before_a_placeholder_is_refused():
    with pytest.raises(ParamError):
        render_sql_with_params("SELECT 1 WHERE a = U&:v", {"v": "x"}, client_type_name="PostgresqlClient")


# The breakouts the prefix rule closes, shown in each grammar's reading.
PREFIX_BREAKOUTS = [
    ("AzureDataExplorerClient", "T | where c == @:v or c == 'z'", "x' or 1==1 //"),
    ("AzureDataExplorerClient", "T | where c == h@:v or c == 'z'", "x' or 1==1 //"),
    ("SplunkClient", "search index=x user=" + B + ":v", " OR user=*"),
]


@pytest.mark.parametrize("family, template, value", PREFIX_BREAKOUTS)
def test_a_prefix_that_changes_how_the_literal_is_read_is_refused(family, template, value):
    renderer = query_params.renderer_for_name(family)
    written = template.replace(":v", renderer.quote(value))
    start = template.index(":v")
    # Today's text: the literal is not one span in some reading of the grammar.
    assert not _one_literal(renderer, written, start, start + len(renderer.quote(value)))
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"v": value}, renderer=renderer)


# MySQL-family comments sqlglot drops but the engines run: MariaDB's /*M! */,
# TiDB's /*T! */, and '--' not followed by a space (MySQL reads it as code).
MYSQL_COMMENT_TEMPLATES = [
    "SELECT id FROM t WHERE owner = 'nobody' /*M! OR owner = ' */ OR owner = :v /*M! ' */",
    "SELECT id FROM t WHERE owner = 'nobody' --'" + NL + "OR owner = :v -- '",
]
UNSPACED = " OR 1=1 OR owner = "


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("template", MYSQL_COMMENT_TEMPLATES + [
    "SELECT 1 /*T! , 2 */ WHERE a = :v",
    "SELECT 1 /*m! , 2 */ WHERE a = :v",
    "SELECT 1 --x" + NL + "WHERE a = :v",
    "SELECT 1 WHERE a = :v --x",
    "SELECT 1 WHERE a = :v ---",
])
def test_params_are_refused_with_a_comment_a_mysql_family_engine_runs(family, template):
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"v": "x"}, renderer=query_params.renderer_for_name(family))


@pytest.mark.parametrize("template, expected", [
    ("SELECT 1 WHERE a = :v -- note", "SELECT 1 WHERE a = 'x' -- note"),
    ("SELECT 1 WHERE a = :v --" + chr(9) + "note", "SELECT 1 WHERE a = 'x' --" + chr(9) + "note"),
    ("SELECT 1 WHERE a = :v --", "SELECT 1 WHERE a = 'x' --"),
    ("SELECT 1 WHERE a = :v AND b = '--x'", "SELECT 1 WHERE a = 'x' AND b = '--x'"),
    ("SELECT 1 /* M! */ WHERE a = :v", "SELECT 1 /* M! */ WHERE a = 'x'"),
] + [(template, template.replace(":v", "'x'")) for template in [
    # '--' inside a comment every MySQL reading sees is part of that comment.
    "-- ---------" + NL + "SELECT 1 WHERE a = :v",
    "SELECT 1 WHERE a = :v -- a--b",
    "SELECT 1 /* a--b */ WHERE a = :v",
    "SELECT 1 WHERE a = :v # see--x",
]])
def test_mysql_comments_the_engine_also_skips_still_render(template, expected):
    assert render_sql_with_params(template, {"v": "x"}, renderer=query_params.renderer_for_name("MariadbClient")) == expected


def test_a_bare_dash_line_is_still_refused_on_mysql():
    with pytest.raises(ParamError):
        render_sql_with_params("----------" + NL + "SELECT 1 WHERE a = :v", {"v": "x"},
                               renderer=query_params.renderer_for_name("MysqlClient"))


@pytest.fixture(scope="module", params=["mariadb:11.4", "mysql:8"])
def mariadb_rows(request):
    """A run_query over a throwaway MariaDB (or MySQL) server holding _ROWS."""
    if not _docker_available():
        pytest.skip("Docker daemon not reachable")
    import pymysql
    from testcontainers.mysql import MySqlContainer

    with MySqlContainer(request.param) as db:
        con = pymysql.connect(host=db.get_container_host_ip(), port=int(db.get_exposed_port(3306)),
                              user=db.username, password=db.password, database=db.dbname, autocommit=True)
        cur = con.cursor()
        _fill(cur.execute, cur.executemany, "%s")

        def run(sql):
            cur.execute(sql)  # no args: pymysql sends the text as it is
            return sorted(r[0] for r in cur.fetchall())

        yield run
        con.close()


@pytest.mark.parametrize("template", MYSQL_COMMENT_TEMPLATES)
def test_a_comment_mariadb_runs_is_refused_before_anything_is_sent(request, mariadb_rows, template):
    renderer = query_params.renderer_for_name("MariadbClient")
    mysql_skips_it = "/*M!" in template and request.node.callspec.params["mariadb_rows"].startswith("mysql")
    assert mariadb_rows(template.replace(":v", renderer.quote(UNSPACED))) == ([] if mysql_skips_it else [1, 2, 3])
    assert mariadb_rows(render_sql_with_params("SELECT id FROM t WHERE owner = :v", {"v": UNSPACED},
                                               renderer=renderer)) == []
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"v": UNSPACED}, renderer=renderer)


def test_the_negative_number_filter_keeps_its_rest_on_mariadb(mariadb_rows):
    rendered = render_sql_with_params(SCORE_FILTER, {"p": -5}, renderer=query_params.renderer_for_name("MariadbClient"))
    assert mariadb_rows(rendered) == [1]


@pytest.mark.parametrize("template", [
    "-- ---------" + NL + "SELECT id FROM t WHERE owner = :v",
    "SELECT id FROM t WHERE owner = :v -- a--b",
    "SELECT id FROM t /* a--b */ WHERE owner = :v",
    "SELECT id FROM t WHERE owner = :v # see--x",
])
def test_a_double_dash_inside_a_comment_renders_and_runs_on_the_engine(mariadb_rows, template):
    rendered = render_sql_with_params(template, {"v": "bob"}, renderer=query_params.renderer_for_name("MysqlClient"))
    assert mariadb_rows(rendered) == [2]


# The re-check of the rendered text catches each of the cases above on its
# own: with the specific rules (spacing, the characters before a
# placeholder, the MySQL-family comments) turned off, the rendered text is
# re-read and refused.
RECHECK_CASES = [
    ("PostgresqlClient", SCORE_FILTER, {"p": -5}),
    ("SqliteClient", SCORE_FILTER, {"p": -5}),
    ("CSVClient", SCORE_FILTER, {"p": -5}),
    ("MSSQLClient", "SELECT DATEADD(day, -:days, d) FROM t", {"days": -5}),
    ("VerticaClient", SCORE_FILTER, {"p": -5}),
    ("TimbrClient", SCORE_FILTER, {"p": -5.5}),
] + [(family, template, {"v": value}) for family, template, value in PREFIX_BREAKOUTS] + [
    ("MariadbClient", template, {"v": UNSPACED}) for template in MYSQL_COMMENT_TEMPLATES
] + [
    ("MysqlClient", "SELECT id FROM t WHERE owner = 'nobody' /*T! OR owner = ' */ OR owner = :v /*T! ' */", {"v": UNSPACED}),
]


def _without_the_specific_rules(monkeypatch):
    # raising=False: before the rules exist, this patches nothing.
    monkeypatch.setattr(query_params, "_separated", lambda sql, start, text, renderer: text, raising=False)
    monkeypatch.setattr(query_params, "_refuse_prefixes", lambda sql, matches, renderer: None, raising=False)
    monkeypatch.setattr(query_params, "_refuse_engine_comments", lambda sql, tokens, dialect: None, raising=False)


@pytest.mark.parametrize("family, template, values", RECHECK_CASES)
def test_the_rendered_text_is_re_read_and_refused_without_the_specific_rules(monkeypatch, family, template, values):
    renderer = query_params.renderer_for_name(family)
    _without_the_specific_rules(monkeypatch)
    with pytest.raises(ParamError):
        render_sql_with_params(template, values, renderer=renderer)
    # Without the re-check as well, the text goes out as written today.
    monkeypatch.setattr(query_params, "_check_rendered", lambda *a: None, raising=False)
    render_sql_with_params(template, values, renderer=renderer)


@pytest.mark.parametrize("family, template, value", [
    ("PostgresqlClient", "SELECT 1 WHERE a = :v -- owner" + NL + "AND b = 2", "x" + NL + "y"),
    ("OracledbClient", "SELECT 1 FROM dual WHERE a IN :v /* c */", ["a'b", "c"]),
    ("MSSQLClient", "SELECT 1 WHERE a = :v", "".join(map(chr, (0x5E9, 0x5DC)))),
    ("BigqueryClient", "SELECT 1 WHERE a = :v AND b = ''", B + "'"),
    ("MecmClient", "$filter=Name eq :v&$top=5", "a&b=%27"),
    ("PriorityErpClient", "ORDERS(:v)?$filter=A eq :v", "it's"),
    ("SplunkClient", "search index=x user=:v | where a=:v", 'a"b' + B),
    ("AzureDataExplorerClient", "T | where c == :v // owner", "x' //"),
    ("MariadbClient", "SELECT 1 WHERE a = :v # note", "a" + B + "'b"),
])
def test_the_re_read_accepts_a_value_that_is_one_literal(monkeypatch, family, template, value):
    renderer = query_params.renderer_for_name(family)
    _without_the_specific_rules(monkeypatch)
    rendered = render_sql_with_params(template, {"v": value}, renderer=renderer)
    literal = query_params._render_literal(value, renderer)
    assert rendered == template.replace(":v", literal)


# A line comment ends at LF for every engine; sqlglot also ends it at CR,
# Postgres and DuckDB at CR too, SQLite and MySQL not. Other characters some
# reader may take for a line end: VT, FF, FS, GS, RS, NEL, LS, PS. The
# re-read of the rendered text uses the same reading, so it cannot see this.
CR = chr(13)
ODD_BREAKS = [CR, chr(11), chr(12), chr(0x1C), chr(0x1D), chr(0x1E), chr(0x85), chr(0x2028), chr(0x2029)]
# The renderers whose grammars have line comments, and how one opens.
LINE_COMMENT_RENDERERS = {name: ("//" if name == "kql" else "--")
                          for name in query_params._RENDERERS if name not in ("spl", "soql", "odata")}


@pytest.mark.parametrize("brk", ODD_BREAKS, ids=lambda c: "U+%04X" % ord(c))
@pytest.mark.parametrize("name", sorted(LINE_COMMENT_RENDERERS))
def test_a_line_comment_holding_another_line_break_refuses_params(name, brk):
    renderer = query_params._RENDERERS[name]
    mark = LINE_COMMENT_RENDERERS[name]
    for template in [
        # Code to a reader that ends the comment at the character, comment
        # to one that does not.
        "SELECT 1 " + mark + " c" + brk + "AND a = :v",
        # The other way round: a string to a reader that ends the comment
        # at the character.
        "SELECT 1 " + mark + " c" + brk + "' " + NL + "AND a = :v " + mark + " '",
    ]:
        with pytest.raises(ParamError):
            render_sql_with_params(template, {"v": "x"}, renderer=renderer)


@pytest.mark.parametrize("name", sorted(query_params._RENDERERS))
def test_line_breaks_inside_a_string_and_crlf_still_render(name):
    renderer = query_params._RENDERERS[name]
    mark = LINE_COMMENT_RENDERERS.get(name)
    quote = '"' if name == "spl" else "'"
    inside = quote + "a" + "".join(ODD_BREAKS) + "b" + quote
    template = "x = " + inside + " and y = :v"
    assert render_sql_with_params(template, {"v": 5}, renderer=renderer) == template.replace(":v", "5")
    if mark is not None:
        template = "SELECT 1 " + mark + " c" + CR + NL + "AND a = :v"
        assert render_sql_with_params(template, {"v": 5}, renderer=renderer) == template.replace(":v", "5")


def test_a_lone_cr_in_a_comment_would_read_another_table_on_sqlite_and_is_refused_before_anything_is_sent(request):
    run = _engine("sqlite", request)
    renderer = query_params.renderer_for_name("SqliteClient")
    template = "SELECT id FROM t WHERE owner = 'nobody' -- note" + CR + "OR owner = :v"
    value = NL + "UNION SELECT score FROM s --"
    # As written today: SQLite reads the rest of the line as the comment, so
    # the value's text after its LF runs as SQL and reads table s.
    assert run(template.replace(":v", renderer.quote(value))) == [50, 150, 200]
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"v": value}, renderer=renderer)


# Scanned grammars: what follows a placeholder must not join the value
# either (':v.5' with 5 would read 5.5).
SCANNED = sorted(name for name in query_params._RENDERERS if query_params._sqlglot_dialect_of(query_params._RENDERERS[name]) is None)


@pytest.mark.parametrize("after", [".5", ".x", chr(0xE9)])
@pytest.mark.parametrize("name", SCANNED)
def test_a_scanned_placeholder_followed_by_a_dot_or_a_name_character_is_refused(name, after):
    with pytest.raises(ParamError):
        render_sql_with_params("a = :v" + after + " and b", {"v": 5}, renderer=query_params._RENDERERS[name])


def test_a_negative_number_is_spaced_after_any_character_but_a_space_except_on_odata_and_spl():
    odata = query_params._RENDERERS["odata"]
    assert render_sql_with_params("Orders(:k)?$top=:n", {"k": -5, "n": -1}, renderer=odata) == "Orders(-5)?$top=-1"
    assert render_sql_with_params("$filter=A eq :k and B eq (:k)", {"k": -5}, renderer=odata) == "$filter=A eq -5 and B eq (-5)"
    # SPL reads `field=value` as one pair: the value stays glued to '=',
    # spaced only after a '-' (which would make '--').
    spl = query_params._RENDERERS["spl"]
    assert render_sql_with_params("search owner=:v n>:v", {"v": -5}, renderer=spl) == "search owner=-5 n>-5"
    assert render_sql_with_params("eval x=1-:v", {"v": -5}, renderer=spl) == "eval x=1- -5"
    for name in sorted(set(query_params._RENDERERS) - {"odata", "spl"}):
        renderer = query_params._RENDERERS[name]
        assert render_sql_with_params("f(:a, =:a, 1-:a)", {"a": -5}, renderer=renderer) == "f( -5, = -5, 1- -5)", name
        assert render_sql_with_params(":a = 1 - :a", {"a": -5}, renderer=renderer) == "-5 = 1 - -5", name
        for c in sorted(OPERATORS_BEFORE | set("=(,")):
            assert render_sql_with_params("x " + c + ":v y", {"v": -5}, renderer=renderer) == "x " + c + " -5 y", (name, c)
        assert render_sql_with_params("x ||:v y", {"v": -5}, renderer=renderer) == "x || -5 y", name


# Postgres-family lexers join a minus to the operator characters before it
# into one operator ('!=-', '%-', '^-'), which does not exist.
GLUED_OPERATOR_FILTERS = [
    "SELECT id FROM s WHERE 3 !=:p AND is_public = 1",
    "SELECT id FROM s WHERE 7 %:p = 2 AND is_public = 1",
    "SELECT id FROM s WHERE 2 ^:p < 1 AND is_public = 1",
]


# The Postgres run doubles '%' (postgres_rows), so '%' runs on DuckDB only.
@pytest.mark.parametrize("engine, family, template", [
    ("duckdb", "CSVClient", template) for template in GLUED_OPERATOR_FILTERS
] + [
    ("postgres", "PostgresqlClient", template) for template in GLUED_OPERATOR_FILTERS if "%" not in template
])
def test_a_negative_number_after_an_operator_character_runs_on_the_engine(request, engine, family, template):
    run = _engine(engine, request)
    with pytest.raises(Exception):  # glued: one operator the engine does not know
        run(template.replace(":p", "-5"))
    rendered = render_sql_with_params(template, {"p": -5}, renderer=query_params.renderer_for_name(family))
    assert rendered == template.replace(":p", " -5")
    assert run(rendered) == [1, 3]


def test_an_odata_value_that_cannot_be_encoded_is_a_param_error():
    with pytest.raises(ParamError):
        render_sql_with_params("$filter=Name eq :v", {"v": "a" + chr(0xD800)}, renderer=query_params._RENDERERS["odata"])


def test_a_reading_count_that_differs_between_template_and_rendered_text_fails_closed(monkeypatch):
    # One list for the template, two for the rendered text: nothing may be
    # left unchecked. The refusal says so, not that the connection has no
    # checker (a different failure).
    monkeypatch.setitem(query_params._READINGS, "ansi", lambda text: [[]] * (1 + (":" not in text)))
    with pytest.raises(ParamError) as refused:
        render_sql_with_params("a = :v", {"v": 5}, renderer=query_params._RENDERERS["ansi"])
    assert str(refused.value) != "parameters cannot be checked for this connection"
    assert str(refused.value) == (
        "the query with its parameter values was not read the way its template was, so the values were not placed"
    )


@pytest.mark.parametrize("value", [b"it's", bytearray(b"x"), memoryview(b"x")], ids=["bytes", "bytearray", "memoryview"])
@pytest.mark.parametrize("name", sorted(query_params._RENDERERS))
def test_a_bytes_value_is_refused_not_rendered_as_its_repr(name, value):
    with pytest.raises(ParamError):
        render_sql_with_params("a = :v", {"v": value}, renderer=query_params._RENDERERS[name])
    with pytest.raises(ParamError):
        render_sql_with_params("a = :v", {"v": ["ok", value]}, renderer=query_params._RENDERERS[name])


# OData options text travels in a URL: requests drops everything after a '#'
# (a fragment), and the server decodes '%00' to a NUL, where it may stop.
# Either way a filter written after it is checked here and never applied.
def _odata_urls(text):
    """The URL each OData client builds from `text`, as requests sends it."""
    import requests
    from app.data_sources.clients.mecm_client import MecmClient
    from app.data_sources.clients.priority_erp_client import PriorityErpClient
    from app.data_sources.clients.sap_datasphere_client import SapDatasphereClient

    sap = "https://dsp.example.com/api/v1/dwc/consumption/analytical/S/M/M"
    sap += "?" + SapDatasphereClient._build_query_string(text, None, None, None, None)
    priority = object.__new__(PriorityErpClient)
    priority.service_root = "https://erp.example.com/odata/Priority/t.ini/c"
    path, _, query_string = ("ORDERS?" + text).partition("?")
    priority_url = f"{priority.service_root}/{path}?{query_string}"  # as execute_query and _get join it
    mecm = object.__new__(MecmClient)
    mecm.base_url = "https://cm.example.com/AdminService"
    mecm._known = {}
    path, query_string = mecm._resolve("SMS_R_System?" + text)
    mecm_url = mecm._url(path) + "?" + query_string
    return {
        name: requests.Request("GET", url).prepare().path_url
        for name, url in (("sap", sap), ("priority", priority_url), ("mecm", mecm_url))
    }


ODATA_END_TEMPLATES = [
    "$select=Name#&$filter=Owner eq :me",
    "$select=Name%00&$filter=Owner eq :me",
    "$select=Name%00x&$filter=Owner eq :me",
    "$filter=Owner eq :me&$select=Name#x",
]


@pytest.mark.parametrize("template", ODATA_END_TEMPLATES)
def test_odata_params_are_refused_when_the_text_holds_a_fragment_mark_or_an_encoded_nul(template):
    renderer = query_params._RENDERERS["odata"]
    written = template.replace(":me", renderer.quote("alice"))
    for name, path_url in _odata_urls(written).items():
        if "#" in template:
            assert "#" not in path_url, name
            if template.index("#") < template.index(":me"):
                assert "Owner" not in path_url, name  # the filter never reaches the server
        else:
            assert "%00" in path_url, name  # sent as is; the server decodes it to NUL
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"me": "alice"}, renderer=renderer)
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"me": 7}, renderer=renderer)


ODATA_FAMILIES = ["PriorityErpClient", "SapDatasphereClient", "MecmClient"]
ODATA_INLINE_AFTER_END = ["$select=Name#&$filter=Owner eq 'alice'", "$select=Name%00&$filter=Owner eq 'alice'"]


@pytest.mark.parametrize("text", ODATA_INLINE_AFTER_END)
@pytest.mark.parametrize("family", ODATA_FAMILIES)
def test_an_odata_identity_value_after_a_fragment_mark_or_an_encoded_nul_does_not_bind(family, text):
    from app.ai.code_execution.identity_bind import CapturedQuery

    spec = [ParamSpec.model_validate({"name": "me", "type": "string", "source": "identity"})]
    assert verify_identity_binds_in_queries([CapturedQuery(text, family)], {"me": "alice"}, spec) is not None
    before_end = "$filter=Owner eq 'alice'&$select=Name"
    assert verify_identity_binds_in_queries([CapturedQuery(before_end, family)], {"me": "alice"}, spec) is None


ODATA_CALL_WITH_OPTIONS = {
    "PriorityErpClient": "execute_query('CUSTOMERS?' + {options!r})",
    "MecmClient": "execute_query('SMS_R_System?' + {options!r})",
    "SapDatasphereClient": "execute_query({options!r}, 'S/M')",
}


@pytest.mark.parametrize("text", ODATA_INLINE_AFTER_END)
@pytest.mark.parametrize("family", ODATA_FAMILIES)
def test_odata_text_holding_a_fragment_mark_or_an_encoded_nul_is_refused_before_it_is_sent(mode, odata_http, family, text):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS[family]
    _, _, error = _run_capturing(_odata_client(family), call.format(options=text))
    assert error is not None and "'#' or '%00'" in str(error)
    assert odata_http == []
    _, _, error = _run_capturing(_odata_client(family), call.format(options="$filter=Owner eq 'alice'"))
    assert error is None and len(odata_http) == 1 and "alice" in odata_http[0]


def test_odata_params_still_render_without_a_fragment_mark_or_an_encoded_nul():
    renderer = query_params._RENDERERS["odata"]
    assert render_sql_with_params("$filter=Owner eq :me&$select=Pct%25", {"me": "a#b"}, renderer=renderer) == \
        "$filter=Owner eq 'a%23b'&$select=Pct%25"
    assert all("Owner" in url for url in _odata_urls("$filter=Owner eq 'a%23b'").values())


# The unguarded OData clients build their request from `query or
# table_name` (Priority, Configuration Manager), or from table_name plus
# option keywords (SAP Datasphere). What is captured, and what the identity
# check reads, is each request as it reached the wire. The real clients run;
# only the transport under their sessions is stubbed.
ODATA_ENTITY = {"PriorityErpClient": "CUSTOMERS", "MecmClient": "SMS_R_System", "SapDatasphereClient": "S/M"}
IDENTITY_ME = [ParamSpec.model_validate({"name": "me", "type": "string", "source": "identity"})]


class _SentUrls(list):
    """The URL of every request that reached the wire. With `next_link` set,
    each data response not already for that URL points to it as a next page."""

    next_link = None


@pytest.fixture
def odata_http(monkeypatch):
    """The transport under the clients' sessions: records each request and
    answers a GET with one row, a POST (an OAuth token grant) with a token."""
    import json
    import requests
    from requests.adapters import HTTPAdapter

    sent = _SentUrls()

    def send(adapter, request, **kwargs):
        sent.append(request.url)
        if request.method == "POST":
            payload = {"access_token": "issued", "expires_in": 600}
        else:
            payload = {"value": [{"OWNER": "bob", "N": 1}]}
            if sent.next_link and request.url != sent.next_link:
                payload["@odata.nextLink"] = sent.next_link
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        response.headers["Content-Type"] = "application/json"
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return sent


def _odata_client(family):
    from app.data_sources.clients.mecm_client import MecmClient
    from app.data_sources.clients.priority_erp_client import PriorityErpClient
    from app.data_sources.clients.sap_datasphere_client import SapDatasphereClient

    if family == "PriorityErpClient":
        return PriorityErpClient(service_root="https://erp.example.com/odata/Priority/t.ini/c", pat="token")
    if family == "MecmClient":
        return MecmClient(server_url="https://cm.example.com", username="svc", password="pw")
    client = SapDatasphereClient(host="dsp.example.com", access_token="token")
    client.attach_table_metadata([{"name": "S/M", "metadata_json": {"sap_datasphere": {
        "data_url": "https://dsp.example.com/api/v1/dwc/consumption/analytical/S/M/M"}}}])
    return client


def _run_capturing(client, body: str):
    """Run `body` against `client`; return (captured queries, timing rows, the error raised or None)."""
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    code = "def generate_df(ds_clients, excel_files):\n" + "".join(
        f"    {line}\n" for line in body.strip().splitlines()
    )
    queries, timings = [], []
    try:
        StreamingCodeExecutor().execute_code(code=code, ds_clients={"c": client}, excel_files=[],
                                             captured_timings=timings, captured_queries=queries)
    except Exception as e:
        return queries, timings, e
    return queries, timings, None


@pytest.mark.parametrize("call", [
    "execute_query('', table_name={e!r})",
    "execute_query('', {e!r})",
    "execute_query(None, {e!r})",
    "execute_query(table_name={e!r})",
])
@pytest.mark.parametrize("family", list(ODATA_ENTITY))
def test_an_odata_call_that_sends_only_its_entity_fails_the_identity_check(mode, odata_http, family, call):
    entity = ODATA_ENTITY[family]
    queries, _, error = _run_capturing(_odata_client(family), "return ds_clients['c']." + call.format(e=entity))
    if family == "PriorityErpClient" and call.startswith("execute_query(table_name"):
        assert "query" in str(error) and odata_http == []  # its statement is a required argument
        return
    assert error is None and len(odata_http) == 1  # the request went out, with no filter
    assert "alice" not in odata_http[0]
    assert verify_identity_binds_in_queries(queries, {"me": "alice"}, IDENTITY_ME) is not None


@pytest.mark.parametrize("family", ["PriorityErpClient", "MecmClient"])
def test_an_odata_statement_passed_as_table_name_is_what_the_identity_check_reads(mode, odata_http, family):
    entity = ODATA_ENTITY[family]
    scoped = entity + "?$filter=OWNER eq 'alice'"
    queries, _, error = _run_capturing(_odata_client(family), f"return ds_clients['c'].execute_query('', table_name={scoped!r})")
    assert error is None and "alice" in odata_http[0]
    assert verify_identity_binds_in_queries(queries, {"me": "alice"}, IDENTITY_ME) is None


def test_datasphere_option_keywords_are_what_the_identity_check_reads(mode, odata_http):
    client = _odata_client("SapDatasphereClient")
    queries, _, error = _run_capturing(client, "return ds_clients['c'].execute_query(None, 'S/M', filter=\"OWNER eq 'alice'\")")
    assert error is None and "alice" in odata_http[0]
    assert verify_identity_binds_in_queries(queries, {"me": "alice"}, IDENTITY_ME) is None
    # Positional options win over the keywords on this client: the filter
    # keyword is not sent, so it does not count.
    del odata_http[:]
    queries, _, error = _run_capturing(client, "return ds_clients['c'].execute_query('$top=5', 'S/M', filter=\"OWNER eq 'alice'\")")
    assert error is None and "alice" not in odata_http[0]
    assert verify_identity_binds_in_queries(queries, {"me": "alice"}, IDENTITY_ME) is not None


@pytest.mark.parametrize("family", list(ODATA_ENTITY))
def test_an_odata_table_name_holding_a_fragment_mark_is_refused_before_it_is_sent(mode, odata_http, family):
    hidden = ODATA_ENTITY[family] + "#$filter=OWNER eq 'alice'"
    queries, _, error = _run_capturing(_odata_client(family), f"return ds_clients['c'].execute_query('', table_name={hidden!r})")
    # Configuration Manager and SAP Datasphere refuse the name itself first.
    assert error is not None and ("'#'" in str(error) or family != "PriorityErpClient")
    assert odata_http == []
    assert [str(q) for q in queries] == [""]  # the statement the code wrote
    assert _reads(queries) == []  # the call raised before sending anything


def _reads(queries) -> list:
    """What the identity check reads of each OData call: the requests it sent."""
    return [str(r) for q in queries for r in q.identity_reads]


# The Queries tab, the planner's observation and the tool audit show
# `executed_queries`: for an OData call that is the query the code wrote,
# whatever the identity check reads of the requests it sent.
ODATA_TOP_FIVE = {"PriorityErpClient": "CUSTOMERS?$top=5", "MecmClient": "SMS_R_System?$top=5",
                  "SapDatasphereClient": "$top=5"}


@pytest.mark.parametrize("family", list(ODATA_TOP_FIVE))
def test_an_odata_call_records_the_query_the_code_wrote(mode, odata_http, family):
    import json

    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS[family].format(options="$top=5")
    queries, timings, error = _run_capturing(_odata_client(family), call)
    assert error is None and len(odata_http) == 1
    assert json.loads(json.dumps(queries)) == [ODATA_TOP_FIVE[family]]
    assert timings[0]["sql"] == ODATA_TOP_FIVE[family]
    assert not _bound(queries)  # the request sent reads as blank: it restricts no rows


def test_a_caught_odata_fragment_refusal_is_recorded_as_a_fixable_error(mode, odata_http):
    body = (
        "try:\n"
        "    ds_clients['c'].execute_query(\"$select=Name#x\")\n"
        "except Exception:\n"
        "    pass\n"
        "return ds_clients['c'].execute_query(\"$filter=OWNER eq 'alice'\")"
    )
    queries, timings, error = _run_capturing(_odata_client("SapDatasphereClient"), body.replace("execute_query(", "execute_query(table_name='S/M', query="))
    assert error is None and len(odata_http) == 1
    refused = timings[0]
    assert "'#'" in refused["error"] and refused["sql"] == "$select=Name#x"
    assert "refusal" not in refused  # a fixable error, not a security refusal
    # The refused call raised before sending anything: the identity check
    # reads nothing of it.
    assert [str(q) for q in queries] == ["$select=Name#x", "$filter=OWNER eq 'alice'"]
    assert _reads(queries) == ["?$filter=OWNER eq 'alice'"]


# What the identity check reads for these clients is each request as it
# reached the wire, not the call's arguments.
DSP_DATA_PATH = "/api/v1/dwc/consumption/analytical/S/M/M"
FILTER_ME = "OWNER eq 'alice'"


def _bound(queries) -> bool:
    return verify_identity_binds_in_queries(queries, {"me": "alice"}, IDENTITY_ME) is None


@pytest.mark.parametrize("statement", ["0", "False", "[]", "()", "0.0"])
@pytest.mark.parametrize("family", list(ODATA_ENTITY))
def test_a_falsy_odata_statement_is_checked_on_the_request_it_sends(mode, odata_http, family, statement):
    entity = ODATA_ENTITY[family]
    queries, _, error = _run_capturing(
        _odata_client(family), f"return ds_clients['c'].execute_query({statement}, table_name={entity!r})")
    assert error is None and len(odata_http) == 1 and "alice" not in odata_http[0]  # the whole table
    assert not _bound(queries)
    del odata_http[:]
    if family == "SapDatasphereClient":
        call = f"execute_query({statement}, table_name={entity!r}, filter={FILTER_ME!r})"
    else:
        call = f"execute_query({statement}, table_name={entity + '?$filter=' + FILTER_ME!r})"
    queries, _, error = _run_capturing(_odata_client(family), "return ds_clients['c']." + call)
    assert error is None and len(odata_http) == 1 and "alice" in odata_http[0]
    assert _bound(queries)


@pytest.mark.parametrize("family, call", [
    ("SapDatasphereClient", f"execute_query(table_name='S/M', filter={FILTER_ME!r})"),
    ("MecmClient", f"execute_query(table_name={'SMS_R_System?$filter=' + FILTER_ME!r})"),
    ("PriorityErpClient", f"execute_query(query={'CUSTOMERS?$filter=' + FILTER_ME!r})"),
])
def test_a_keyword_only_odata_call_is_checked_on_the_request_it_sends(mode, odata_http, family, call):
    queries, _, error = _run_capturing(_odata_client(family), "return ds_clients['c']." + call)
    assert error is None and len(odata_http) == 1 and "alice" in odata_http[0]
    assert _bound(queries)


def test_a_datasphere_model_name_passed_as_the_statement_is_checked_on_the_request_it_sends(mode, odata_http):
    client = _odata_client("SapDatasphereClient")
    queries, _, error = _run_capturing(client, f"return ds_clients['c'].execute_query('S/M', filter={FILTER_ME!r})")
    assert error is None and len(odata_http) == 1
    assert [str(q) for q in queries] == ["S/M"] and _reads(queries) == ["?$filter=" + FILTER_ME]
    assert _bound(queries)
    del odata_http[:]
    queries, _, error = _run_capturing(client, "return ds_clients['c'].execute_query('S/M')")
    assert error is None and len(odata_http) == 1
    assert not _bound(queries)


@pytest.mark.parametrize("mark", ["#", "?"])
def test_a_datasphere_parameter_value_that_cuts_the_request_fails_the_identity_check(mode, odata_http, mark):
    client = _odata_client("SapDatasphereClient")
    body = "return ds_clients['c'].execute_query(None, 'S/M', parameters={{'P': {v!r}}}, filter=" + repr(FILTER_ME) + ")"
    queries, _, error = _run_capturing(client, body.format(v="x" + mark + "y"))
    if mark == "#":
        # Not sent: the call raised, so it returned no rows and leaves no capture.
        assert error is not None and "'#'" in str(error) and odata_http == [] and _reads(queries) == []
    else:
        # Sent: the '?' ends the path, and the filter becomes part of an
        # option name the server does not read as a filter.
        assert error is None and len(odata_http) == 1
        assert not _bound(queries)
    del odata_http[:]
    queries, _, error = _run_capturing(client, body.format(v="xy"))
    assert error is None and len(odata_http) == 1 and "alice" in odata_http[0]
    assert _bound(queries)


def test_a_datasphere_select_keyword_holding_a_filter_option_fails_the_identity_check(mode, odata_http):
    body = "return ds_clients['c'].execute_query(None, 'S/M', select=" + repr("Name&$filter=" + FILTER_ME) + ")"
    queries, _, error = _run_capturing(_odata_client("SapDatasphereClient"), body)
    # The client encodes the '&': the filter text is part of $select's value.
    assert error is None and len(odata_http) == 1 and "%26" in odata_http[0]
    assert not _bound(queries)


def test_a_datasphere_filter_keyword_holding_a_fragment_mark_is_sent_encoded_and_binds(mode, odata_http):
    body = "return ds_clients['c'].execute_query(None, 'S/M', filter=" + repr("NAME eq 'a#b' and " + FILTER_ME) + ")"
    queries, _, error = _run_capturing(_odata_client("SapDatasphereClient"), body)
    assert error is None and len(odata_http) == 1 and "%23" in odata_http[0]
    assert _bound(queries)


def test_an_odata_plus_sign_must_bind_both_ways_the_server_may_read_it(mode, odata_http):
    from app.ai.code_execution.identity_bind import CapturedQuery

    client = _odata_client("PriorityErpClient")
    queries, _, error = _run_capturing(client, "return ds_clients['c'].execute_query(\"CUSTOMERS?$filter=OWNER eq 'a+b'\")")
    assert error is None and len(odata_http) == 1
    assert verify_identity_binds_in_queries(queries, {"me": "a+b"}, IDENTITY_ME) is not None
    del odata_http[:]
    queries, _, error = _run_capturing(client, "return ds_clients['c'].execute_query(\"CUSTOMERS?$filter=OWNER eq 'a%2Bb'\")")
    assert error is None and len(odata_http) == 1
    assert verify_identity_binds_in_queries(queries, {"me": "a+b"}, IDENTITY_ME) is None
    assert all(isinstance(q, CapturedQuery) and q.client_type == "PriorityErpClient" for q in queries)


def test_every_odata_request_a_call_sends_must_bind_the_identity(mode, odata_http):
    client = _odata_client("SapDatasphereClient")
    body = f"return ds_clients['c'].execute_query(None, 'S/M', filter={FILTER_ME!r})"
    odata_http.next_link = "https://dsp.example.com" + DSP_DATA_PATH + "?$filter=OWNER%20eq%20'bob'&$skiptoken=2"
    queries, _, error = _run_capturing(client, body)
    assert error is None and len(odata_http) == 2 and "alice" not in odata_http[1]
    assert not _bound(queries)
    del odata_http[:]
    odata_http.next_link = "https://dsp.example.com" + DSP_DATA_PATH + "?$filter=OWNER%20eq%20'alice'&$skiptoken=2"
    queries, _, error = _run_capturing(client, body)
    assert error is None and len(odata_http) == 2
    assert _bound(queries)


DSP_HOST = "https://dsp.example.com"


@pytest.mark.parametrize("filtered", [True, False])
def test_a_datasphere_next_page_carrying_only_its_skiptoken_is_read_through_the_first_page(mode, odata_http, filtered):
    # The server's next link keeps the first request's path and holds only
    # $skiptoken: what the first request asked for is what the next page reads.
    body = "return ds_clients['c'].execute_query(None, 'S/M'" + (f", filter={FILTER_ME!r})" if filtered else ")")
    odata_http.next_link = DSP_HOST + DSP_DATA_PATH + "?$skiptoken=2"
    queries, _, error = _run_capturing(_odata_client("SapDatasphereClient"), body)
    assert error is None and len(odata_http) == 2
    assert _bound(queries) is filtered


@pytest.mark.parametrize("next_link", [
    DSP_DATA_PATH + "X?$skiptoken=2",  # another path
    DSP_DATA_PATH + "?$skiptoken=2&$top=5",  # another option
    DSP_DATA_PATH + "?$skiptoken=2&$skiptoken=3",
    DSP_DATA_PATH + "?$skiptoken=2&$filter=OWNER%20eq%20'bob'",
    "/elsewhere/S/M/M?$filter=OWNER%20eq%20'alice'",  # outside the consumption root
    "https://other.example.com" + DSP_DATA_PATH + "?$skiptoken=2",  # another host
    "http://dsp.example.com" + DSP_DATA_PATH + "?$skiptoken=2",  # another scheme
    "https://dsp.example.com:8443" + DSP_DATA_PATH + "?$skiptoken=2",  # another port
])
def test_a_datasphere_next_page_that_is_not_only_a_skiptoken_is_checked(mode, odata_http, next_link):
    odata_http.next_link = next_link if next_link.startswith("http") else DSP_HOST + next_link
    body = f"return ds_clients['c'].execute_query(None, 'S/M', filter={FILTER_ME!r})"
    queries, _, error = _run_capturing(_odata_client("SapDatasphereClient"), body)
    assert error is None and len(odata_http) == 2
    assert not _bound(queries)


# What the identity check reads of an OData request is only text that
# restricts rows: the row options and a key predicate in the path, never the
# configured service root, the entity set, the space or the model.
ODATA_ROOT_VALUES = [
    ("PriorityErpClient", "odata"), ("PriorityErpClient", "Priority"), ("PriorityErpClient", "t.ini"),
    ("PriorityErpClient", "c"), ("PriorityErpClient", "CUSTOMERS"),
    ("MecmClient", "AdminService"), ("MecmClient", "Admin"), ("MecmClient", "wmi"), ("MecmClient", "SMS_R_System"),
    ("SapDatasphereClient", "1"), ("SapDatasphereClient", "consumption"), ("SapDatasphereClient", "analytical"),
    ("SapDatasphereClient", "S"), ("SapDatasphereClient", "M"),
]


def _bound_to(queries, value) -> bool:
    return verify_identity_binds_in_queries(queries, {"me": value}, IDENTITY_ME) is None


@pytest.mark.parametrize("family, value", ODATA_ROOT_VALUES)
def test_an_identity_value_found_only_in_the_odata_request_root_is_not_a_bind(mode, odata_http, family, value):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS[family]
    queries, _, error = _run_capturing(_odata_client(family), call.format(options="$filter=N gt 0"))
    assert error is None and len(odata_http) == 1 and value in odata_http[0]
    assert not _bound_to(queries, value)
    del odata_http[:]
    queries, _, error = _run_capturing(_odata_client(family), call.format(options=f"$filter=OWNER eq '{value}'"))
    assert error is None and len(odata_http) == 1
    assert _bound_to(queries, value)


@pytest.mark.parametrize("family, target, binds", [
    ("PriorityErpClient", "CUSTOMERS('alice')", True),
    ("PriorityErpClient", "CUSTOMERS(CUSTNAME='alice')/ORDERS_SUBFORM", True),
    ("PriorityErpClient", "CUSTOMERS('x')/alice_SUBFORM", False),
    ("PriorityErpClient", "CUSTOMERS(SELECT 1)", False),
    ("MecmClient", "SMS_R_System('alice')", True),
    ("MecmClient", "SMS_R_System('x')", False),
] + [("PriorityErpClient", target, False) for target in [
    "(S(alice))/CUSTOMERS", "('alice')/CUSTOMERS", "CUSTOMERS/(S(alice))", "CUSTOMERS%3F$filter=(OWNER eq 'alice')",
]] + [
    ("MecmClient", "SMS_R_System/(S(alice))", False),
])
def test_an_odata_key_predicate_in_the_path_is_read_as_a_row_filter(mode, odata_http, family, target, binds):
    queries, _, error = _run_capturing(_odata_client(family), f"return ds_clients['c'].execute_query({target!r})")
    assert error is None and len(odata_http) == 1
    assert _bound(queries) is binds


# Only a path segment that is a name and one key predicate is read as a key.
# Any other segment holding a parenthesis (ASP.NET's cookieless session
# segment, a predicate with no name, an encoded '?') reads as blank.
ODATA_PATH_PARENTHESES_NOT_A_KEY = [
    "/(S(alice))/CUSTOMERS", "/('alice')/CUSTOMERS", "/CUSTOMERS/(S(alice))",
    "/CUSTOMERS%3F$filter=(OWNER eq 'alice')", "/CUSTOMERS('x')" + chr(92) + "(S(alice))",
    "/CUSTOMERS('alice'", "/CUSTOMERS)('alice')",
]


def test_an_odata_path_segment_with_parentheses_that_is_not_a_key_reads_as_blank():
    from app.ai.code_execution.code_execution import _odata_request_texts

    root = "/odata/Priority/t.ini/c"
    for rest in ODATA_PATH_PARENTHESES_NOT_A_KEY:
        assert _odata_request_texts(root + rest, root, True) == [""], rest
    assert _odata_request_texts(root + "/CUSTOMERS('alice')", root, True) == ["/('alice')"]
    assert _odata_request_texts(root + "/CUSTOMERS(CUSTNAME='alice')/ORDERS_SUBFORM", root, True) == \
        ["/(CUSTNAME='alice')"]


def test_a_datasphere_input_parameter_is_not_a_row_filter(mode, odata_http):
    client = _odata_client("SapDatasphereClient")
    queries, _, error = _run_capturing(client, "return ds_clients['c'].execute_query(None, 'S/M', parameters={'P': 'alice'})")
    assert error is None and len(odata_http) == 1 and "alice" in odata_http[0]
    assert not _bound(queries)


# A row option spelled another way the server may read as that option, or
# may ignore. Only the exact name (percent-decoded) binds; any other
# spelling, alone or next to the exact one, fails the check.
ODATA_FILTER_LOOSE_SPELLINGS = [
    "$FILTER", "filter", "$filter%20", "%20$Filter", "%20$filter", "+$filter", "%24FILTER",
    "$" + chr(0xFB01) + "lter",  # LATIN SMALL LIGATURE FI, case folded to "fi"
    "$f" + chr(0x131) + "lter",  # LATIN SMALL LETTER DOTLESS I, which casefold keeps
    "$F" + chr(0x130) + "LTER",  # LATIN CAPITAL LETTER I WITH DOT ABOVE, folded to "i" and a combining dot
]
ODATA_FILTER_SPELLINGS = ["%24filter"] + ODATA_FILTER_LOOSE_SPELLINGS


@pytest.mark.parametrize("spelling", ODATA_FILTER_SPELLINGS)
@pytest.mark.parametrize("family", ODATA_FAMILIES)
def test_an_odata_row_option_given_twice_in_any_spelling_fails_the_identity_check(mode, odata_http, family, spelling):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS[family]
    options = f"$filter=OWNER eq 'alice'&{spelling}=N gt 0"
    queries, _, error = _run_capturing(_odata_client(family), call.format(options=options))
    assert error is None and len(odata_http) == 1
    assert not _bound(queries)


@pytest.mark.parametrize("spelling", ODATA_FILTER_LOOSE_SPELLINGS)
@pytest.mark.parametrize("family", ODATA_FAMILIES)
def test_an_odata_row_option_alone_in_a_loose_spelling_fails_the_identity_check(mode, odata_http, family, spelling):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS[family]
    queries, _, error = _run_capturing(_odata_client(family), call.format(options=f"{spelling}=OWNER eq 'alice'"))
    assert error is None and len(odata_http) == 1 and "alice" in odata_http[0]
    assert not _bound(queries)
    # Next to an exact filter that binds, it still fails the check.
    del odata_http[:]
    options = f"$filter=OWNER eq 'alice'&{spelling}=OWNER eq 'alice'"
    queries, _, error = _run_capturing(_odata_client(family), call.format(options=options))
    assert error is None and len(odata_http) == 1
    assert not _bound(queries)


@pytest.mark.parametrize("family, options", [
    (family, options) for family in ODATA_FAMILIES
    for options in ["$filter=OWNER eq 'alice'", "%24filter=OWNER eq 'alice'", "$top=5&$filter=OWNER eq 'alice'"]
] + [
    ("SapDatasphereClient", "$apply=filter(OWNER eq 'alice')"),
])
def test_an_odata_row_option_in_its_exact_name_binds(mode, odata_http, family, options):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS[family]
    queries, _, error = _run_capturing(_odata_client(family), call.format(options=options))
    assert error is None and len(odata_http) == 1
    assert _bound(queries)


# Configuration Manager's AdminService documents no $apply: a server that
# ignores it returns every row. There $apply never binds, and it still counts
# as a row option given twice.
@pytest.mark.parametrize("options, binds", [
    ("$apply=filter(OWNER eq 'alice')", False),
    ("$filter=OWNER eq 'alice'&$apply=filter(OWNER eq 'alice')&$APPLY=filter(N gt 0)", False),
    ("$filter=OWNER eq 'alice'&$apply=groupby((OWNER))", True),
])
def test_an_odata_apply_does_not_bind_on_configuration_manager(mode, odata_http, options, binds):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS["MecmClient"]
    queries, _, error = _run_capturing(_odata_client("MecmClient"), call.format(options=options))
    assert error is None and len(odata_http) == 1
    assert _bound(queries) is binds


# OData returns every top-level row whatever an $expand filters: a filter
# inside $expand restricts only the expanded rows.
@pytest.mark.parametrize("family", ODATA_FAMILIES)
def test_an_odata_filter_inside_expand_is_not_a_row_filter(mode, odata_http, family):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS[family]
    expand = "$top=5&$expand=Nav($filter=OWNER eq 'alice')"  # $top: no Priority size probe
    queries, _, error = _run_capturing(_odata_client(family), call.format(options=expand))
    assert error is None and len(odata_http) == 1 and "alice" in odata_http[0]
    assert not _bound(queries)
    del odata_http[:]
    queries, _, error = _run_capturing(_odata_client(family), call.format(options=expand + "&$filter=OWNER eq 'alice'"))
    assert error is None and len(odata_http) == 1
    assert _bound(queries)


@pytest.mark.parametrize("spelling", ["$EXPAND", "expand", "%20$expand"])
def test_an_odata_expand_in_a_loose_spelling_fails_the_identity_check(mode, odata_http, spelling):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS["MecmClient"]
    options = f"$filter=OWNER eq 'alice'&{spelling}=Nav"
    queries, _, error = _run_capturing(_odata_client("MecmClient"), call.format(options=options))
    assert error is None and len(odata_http) == 1
    assert not _bound(queries)


# Dot segments written percent-encoded or with a backslash: a path the
# server may read differently from the client.
BACKSLASH = chr(92)
ODATA_ENCODED_DOT_SEGMENTS = [
    "%2E%2E", "%2e%2e", ".%2E", "%2E.", "%2E",
    BACKSLASH + ".." + BACKSLASH, "%5C..%5C", "..%5C", "%5C%2E%2E", "%5c%2e%2e%5c", BACKSLASH + "..",
]


@pytest.mark.parametrize("dots", ODATA_ENCODED_DOT_SEGMENTS)
@pytest.mark.parametrize("family, target", [
    ("PriorityErpClient", "CUSTOMERS('alice')/{dots}/ORDERS"),
    ("PriorityErpClient", "CUSTOMERS('alice')/{dots}"),
    ("MecmClient", "SMS_R_System('alice')/{dots}/SMS_R_User"),
    ("MecmClient", "wmi/SMS_R_System('alice')/{dots}"),
    ("MecmClient", "SMS_R_System('alice')%2F{dots}"),  # past the client's own check
])
def test_an_odata_path_with_an_encoded_dot_segment_is_refused_before_it_is_sent(mode, odata_http, family, target, dots):
    queries, _, error = _run_capturing(_odata_client(family), f"return ds_clients['c'].execute_query({target.format(dots=dots)!r})")
    # Configuration Manager refuses the class path itself first.
    assert error is not None and ("'.'" in str(error) or (family == "MecmClient" and "Invalid class path" in str(error)))
    assert odata_http == []
    assert _reads(queries) == []  # the call raised before sending anything


# A backslash dot segment with no '/' around it: also a path the server may
# read differently.
ODATA_BACKSLASH_DOT_PATHS = [
    "{key}" + BACKSLASH + ".." + BACKSLASH + "{entity}",
    "{key}%5C..%5C{entity}",
    "{key}/..%5C{entity}",
    "{key}%5C%2E%2E%5C{entity}",
    "{key}" + BACKSLASH + "..",
]
ODATA_KEYED = {
    "PriorityErpClient": ("CUSTOMERS('alice')", "CUSTOMERS"),
    "MecmClient": ("SMS_R_System('alice')", "SMS_R_System"),
}


@pytest.mark.parametrize("shape", ODATA_BACKSLASH_DOT_PATHS)
@pytest.mark.parametrize("family", list(ODATA_KEYED))
def test_an_odata_path_with_a_backslash_dot_segment_is_refused_before_it_is_sent(mode, odata_http, family, shape):
    key, entity = ODATA_KEYED[family]
    target = shape.format(key=key, entity=entity)
    queries, _, error = _run_capturing(_odata_client(family), f"return ds_clients['c'].execute_query({target!r})")
    assert error is not None and ("'.'" in str(error) or (family == "MecmClient" and "Invalid class path" in str(error)))
    assert odata_http == []
    assert [str(q) for q in queries] == [target] and _reads(queries) == []


@pytest.mark.parametrize("shape", ODATA_BACKSLASH_DOT_PATHS + ["{key}%2F%2E%2E%2F{entity}"])
def test_configuration_manager_refuses_a_backslash_or_encoded_dot_class_path_itself(odata_http, shape):
    # Without the wrapper's send check: the client's own class path check.
    key, entity = ODATA_KEYED["MecmClient"]
    with pytest.raises(ValueError, match="Invalid class path"):
        _odata_client("MecmClient").execute_query(shape.format(key=key, entity=entity))
    assert odata_http == []


@pytest.mark.parametrize("rest", [
    "wmi/SMS_R_System('alice')%5C..%5CSMS_R_System",
    "wmi/SMS_R_System('alice')/..%5CSMS_R_System",
    "wmi/SMS_R_System('alice')%5C%2E%2E",
])
def test_configuration_manager_stops_at_a_next_link_with_a_backslash_dot_segment(odata_http, rest):
    from app.data_sources.clients.mecm_client import MecmError

    odata_http.next_link = "https://cm.example.com/AdminService/" + rest
    with pytest.raises(MecmError, match="'..' segments"):
        _odata_client("MecmClient").execute_query("SMS_R_System")
    assert len(odata_http) == 1  # the first page only


@pytest.fixture
def wire(monkeypatch):
    """A real HTTP server on 127.0.0.1 answering every GET with one OData
    row; yields (service root under it, the request paths that reached it)."""
    from app.core import egress
    from tests.utils.egress_fakes import local_server

    class _AnyPath(dict):
        def get(self, key, default=None):
            return 200, {}, {"value": [{"OWNER": "bob", "N": 1}]}

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)
    with local_server(_AnyPath()) as (port, seen):
        yield f"http://127.0.0.1:{port}/odata/Priority/t.ini/c", seen


@pytest.mark.parametrize("shape", ODATA_BACKSLASH_DOT_PATHS)
def test_an_odata_backslash_dot_segment_never_reaches_the_server(mode, wire, shape):
    from app.data_sources.clients.priority_erp_client import PriorityErpClient

    root, seen = wire
    client = PriorityErpClient(service_root=root, pat="token")
    queries, _, error = _run_capturing(client, "return ds_clients['c'].execute_query('CUSTOMERS?$top=1')")
    assert error is None and len(seen) == 1  # the server answers a plain read
    del seen[:]
    target = shape.format(key="CUSTOMERS('alice')", entity="CUSTOMERS")
    queries, _, error = _run_capturing(client, f"return ds_clients['c'].execute_query({target!r})")
    assert [path for _, path, _ in seen] == []
    assert error is not None and "'.'" in str(error)
    assert [str(q) for q in queries] == [target] and _reads(queries) == []


@pytest.mark.parametrize("target, binds", [
    ("(S(alice))/CUSTOMERS", False), ("('alice')/CUSTOMERS", False), ("CUSTOMERS/(S(alice))", False),
    ("CUSTOMERS%3F$filter=(OWNER eq 'alice')", False),
    ("CUSTOMERS('alice')", True), ("CUSTOMERS(CUSTNAME='alice')/ORDERS_SUBFORM", True),
])
def test_a_parenthesised_path_segment_reaches_the_server_and_binds_only_as_a_key(mode, wire, target, binds):
    from urllib.parse import unquote

    from app.data_sources.clients.priority_erp_client import PriorityErpClient

    root, seen = wire
    client = PriorityErpClient(service_root=root, pat="token")
    queries, _, error = _run_capturing(client, f"return ds_clients['c'].execute_query({target!r})")
    assert error is None
    paths = [unquote(path) for _, path, _ in seen]
    assert len(paths) == 1 and "alice" in paths[0], paths  # sent as written
    assert _bound(queries) is binds


@pytest.mark.parametrize("url", ["https://[erp.example.com/c/CUSTOMERS?$skiptoken=2", "https://erp.example.com:x/c?$skiptoken=2"])
def test_a_malformed_next_page_url_is_not_a_continuation(url):
    from app.ai.code_execution.code_execution import _odata_next_page

    assert _odata_next_page(url, "https://erp.example.com/c/CUSTOMERS") is False


def test_a_priority_size_probe_refused_before_sending_is_logged_by_type(mode, odata_http, caplog):
    import logging

    name = "app.data_sources.clients.priority_erp_client"
    logging.getLogger(name).disabled = False
    with caplog.at_level(logging.DEBUG, logger=name):
        _, _, error = _run_capturing(_odata_client("PriorityErpClient"),
                                     "return ds_clients['c'].execute_query('CUSTOMERS%00?$expand=ORDERS_SUBFORM')")
    assert error is not None and odata_http == []
    logged = [r.getMessage() for r in caplog.records if r.name == name and "probe" in r.getMessage()]
    assert len(logged) == 1 and "ParamError" in logged[0], [r.getMessage() for r in caplog.records]
    assert "CUSTOMERS" not in logged[0] and "%00" not in logged[0]


@pytest.mark.parametrize("family, target", [
    ("PriorityErpClient", "CUSTOMERS('alice')/../ORDERS"),
    ("MecmClient", "SMS_R_System('alice')/../SMS_R_User"),
])
def test_an_odata_path_with_a_literal_dot_segment_behaves_as_before(mode, odata_http, family, target):
    queries, _, error = _run_capturing(_odata_client(family), f"return ds_clients['c'].execute_query({target!r})")
    if family == "MecmClient":
        # The client refuses the class path itself; nothing is sent.
        assert error is not None and "Invalid class path" in str(error) and odata_http == []
        return
    # The client resolves it before sending: the key predicate is not sent.
    assert error is None and len(odata_http) == 1 and "alice" not in odata_http[0]
    assert not _bound(queries)


def test_an_odata_request_path_with_a_dot_segment_reads_as_blank():
    from app.ai.code_execution.code_execution import _odata_request_texts

    root = "/odata/Priority/t.ini/c"
    assert _odata_request_texts(root + "/CUSTOMERS('alice')/%2E%2E/ORDERS", root, True) == [""]
    for shape in ODATA_BACKSLASH_DOT_PATHS:
        path = shape.format(key="/CUSTOMERS('alice')", entity="ORDERS")
        assert _odata_request_texts(root + path, root, True) == [""], path
    assert _odata_request_texts(root + "/CUSTOMERS('alice')/ORDERS", root, True) == ["/('alice')"]


def test_an_odata_request_outside_the_configured_root_gets_its_own_refusal(mode, odata_http):
    odata_http.next_link = DSP_HOST + "/elsewhere/S/M/M?$filter=OWNER%20eq%20'alice'"
    body = f"return ds_clients['c'].execute_query(None, 'S/M', filter={FILTER_ME!r})"
    queries, _, error = _run_capturing(_odata_client("SapDatasphereClient"), body)
    assert error is None and len(odata_http) == 2
    message = verify_identity_binds_in_queries(queries, {"me": "alice"}, IDENTITY_ME)
    assert message is not None and "configured path" in message and "left nothing" not in message
    assert "alice" not in message and "elsewhere" not in message


def _priority_answering_without_a_request():
    from app.data_sources.clients.priority_erp_client import PriorityErpClient

    # The declared client answering without a request. Set on the instance: a
    # subclass with its own execute_query is not covered by the client's
    # exemption (test_client_classification.py) and would be refused text.
    client = PriorityErpClient(service_root="https://erp.example.com/odata/Priority/t.ini/c", pat="token")
    client.execute_query = lambda query=None, table_name=None, max_rows=None: pd.DataFrame([{"OWNER": "bob"}])
    return client


def test_an_odata_call_that_raised_before_sending_leaves_nothing_for_the_identity_check(mode, odata_http):
    body = (
        "try:\n"
        "    ds_clients['c'].execute_query('$top=1', 'Other/Model')\n"
        "except Exception:\n"
        "    pass\n"
        f"return ds_clients['c'].execute_query(None, 'S/M', filter={FILTER_ME!r})"
    )
    queries, timings, error = _run_capturing(_odata_client("SapDatasphereClient"), body)
    assert error is None and len(odata_http) == 1
    assert timings[0]["error"]  # the failed call is still recorded
    assert [str(q) for q in queries] == ["$top=1", ""]  # the statements the code wrote
    assert _reads(queries) == ["?$filter=" + FILTER_ME]
    assert _bound(queries)


def test_an_odata_call_that_returned_rows_without_sending_fails_the_identity_check(mode, odata_http):
    queries, _, error = _run_capturing(_priority_answering_without_a_request(),
                                       f"return ds_clients['c'].execute_query({'CUSTOMERS?$filter=' + FILTER_ME!r})")
    assert error is None and odata_http == []
    assert [str(q) for q in queries] == ["CUSTOMERS?$filter=" + FILTER_ME] and _reads(queries) == [""]
    assert not _bound(queries)


@pytest.mark.parametrize("token_url", [
    "https://AUTH.Example.COM/oauth/token",
    "HTTPS://auth.example.com:443/oauth/token",
    "https://auth.example.com",
])
def test_a_datasphere_token_grant_is_recognised_however_its_url_is_written(mode, odata_http, token_url):
    from app.data_sources.clients.sap_datasphere_client import SapDatasphereClient

    client = SapDatasphereClient(host="dsp.example.com", token_url=token_url, client_id="id", client_secret="pw")
    client.attach_table_metadata([{"name": "S/M", "metadata_json": {"sap_datasphere": {
        "data_url": DSP_HOST + DSP_DATA_PATH}}}])
    queries, _, error = _run_capturing(client, f"return ds_clients['c'].execute_query(None, 'S/M', filter={FILTER_ME!r})")
    assert error is None and len(odata_http) == 2
    assert _bound(queries)


@pytest.fixture
def servicenow_http(monkeypatch):
    """The transport under the ServiceNow client's session: records each
    request's URL and answers with one record."""
    import json
    import requests
    from requests.adapters import HTTPAdapter

    sent = []

    def send(adapter, request, **kwargs):
        sent.append(request.url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"result": [{"number": "INC1", "caller_id": "bob"}]}).encode()
        response.headers["Content-Type"] = "application/json"
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return sent


# A dict statement to an unguarded client is captured as its JSON text.
@pytest.mark.parametrize("spec, binds", [
    ({"table": "incident"}, False),
    ({"table": "incident", "query": "caller_id=alice"}, True),
])
def test_a_servicenow_dict_statement_is_what_the_identity_check_reads(mode, servicenow_http, spec, binds):
    from app.data_sources.clients.servicenow_client import ServiceNowClient

    client = ServiceNowClient(instance_url="https://sn.example.com", access_token="tok")
    queries, _, error = _run_capturing(client, f"return ds_clients['c'].execute_query({spec!r})")
    assert error is None and len(servicenow_http) == 1
    assert ("alice" in servicenow_http[0]) is binds
    assert len(queries) == 1 and _bound(queries) is binds


@pytest.mark.parametrize("spec, binds", [
    ({"list": "2017_Expense_Data"}, False),
    ({"list": "2017_Expense_Data", "filter": "Title eq 'alice'"}, True),
])
def test_a_sharepoint_list_dict_statement_is_what_the_identity_check_reads(mode, spec, binds):
    items = []
    queries, _, error = _run_capturing(_sharepoint_list_client(items), f"return ds_clients['c'].execute_query({spec!r})")
    assert error is None and len(items) == 1
    assert ("alice" in items[0]) is binds
    assert len(queries) == 1 and _bound(queries) is binds


def test_a_datasphere_token_grant_is_not_read_as_a_query(mode, odata_http):
    from app.data_sources.clients.sap_datasphere_client import SapDatasphereClient

    client = SapDatasphereClient(host="dsp.example.com", token_url="https://auth.example.com/oauth/token",
                                 client_id="id", client_secret="pw")
    client.attach_table_metadata([{"name": "S/M", "metadata_json": {"sap_datasphere": {
        "data_url": "https://dsp.example.com" + DSP_DATA_PATH}}}])
    queries, _, error = _run_capturing(client, f"return ds_clients['c'].execute_query(None, 'S/M', filter={FILTER_ME!r})")
    assert error is None and odata_http[0] == "https://auth.example.com/oauth/token" and len(odata_http) == 2
    assert _bound(queries)


# A request the send check refuses was never sent: like a guard refusal, it
# is not charged as a data query. The rate-limit request stays counted.
@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("target, charged", [
    ("CUSTOMERS?$top=1&$select=NAME%00", 0),
    ("CUSTOMERS('alice')/%2E%2E/ORDERS?$top=1", 0),
    ("CUSTOMERS?$top=1", 1),
])
async def test_an_odata_request_refused_before_it_is_sent_is_not_charged(odata_http, metering_on, target, charged):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper
    from app.dependencies import async_session_maker
    from app.services.usage_policy_service import UsageLimitContext
    from tests.unit.test_splunk_client import _metered_splunk, _usage_and_limiter

    client = _odata_client("PriorityErpClient")
    org_id, user_id, conn_id = await _metered_splunk(client)
    ctx = UsageLimitContext(organization_id=org_id, user_id=user_id, source="agent",
                            source_ref_id="run-1", session_maker=async_session_maker)
    ctx.loop = asyncio.get_running_loop()
    wrapper = QueryCapturingClientWrapper(client, [], [], usage_context=ctx, client_key="priority:1")
    outcome = None
    try:
        await asyncio.to_thread(wrapper.execute_query, target)
    except Exception as e:
        outcome = e
    await ctx.flush()
    assert (outcome is None) is bool(charged), outcome
    assert len(odata_http) == charged
    assert await _usage_and_limiter(org_id, conn_id) == (charged, 1)


@pytest.fixture
def ntlm_http(monkeypatch, tmp_path):
    """The transport under Configuration Manager's session, answering as an
    NTLM server: a request with no credentials gets a 401, the negotiate leg
    a challenge, the authenticate leg (checked by a real NTLM acceptor) the
    rows. Records each request's URL as it reached the transport."""
    import base64
    import json

    import requests
    import spnego
    from requests.adapters import HTTPAdapter

    users = tmp_path / "ntlm-users"
    users.write_text(":svc:pw\n")
    monkeypatch.setenv("NTLM_USER_FILE", str(users))
    sent = []
    acceptor = {}

    def send(adapter, request, **kwargs):
        sent.append(request.url)
        response = requests.Response()
        response.url = request.url
        response.request = request
        response.connection = adapter
        response.raw = type("Raw", (), {"release_conn": lambda self: None})()
        auth = request.headers.get("Authorization", "")
        token = base64.b64decode(auth[len("NTLM "):]) if auth.startswith("NTLM ") else None
        if token is None:
            response.status_code, response._content = 401, b""
            response.headers["WWW-Authenticate"] = "NTLM"
        elif "server" not in acceptor:
            acceptor["server"] = spnego.server(protocol="ntlm")
            challenge = acceptor["server"].step(token)
            response.status_code, response._content = 401, b""
            response.headers["WWW-Authenticate"] = "NTLM " + base64.b64encode(challenge).decode()
        else:
            acceptor.pop("server").step(token)  # raises unless the client signed in as svc
            response.status_code = 200
            response._content = json.dumps({"value": [{"OWNER": "bob", "N": 1}]}).encode()
            response.headers["Content-Type"] = "application/json"
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return sent


@pytest.mark.parametrize("options, binds", [("$filter=OWNER eq 'alice'", True), ("$top=5", False)])
def test_an_ntlm_handshake_leaves_one_capture_per_configuration_manager_call(mode, ntlm_http, options, binds):
    call = "return ds_clients['c']." + ODATA_CALL_WITH_OPTIONS["MecmClient"].format(options=options)
    queries, timings, error = _run_capturing(_odata_client("MecmClient"), call)
    assert error is None and len(ntlm_http) == 3  # the same request, once per handshake leg
    assert [str(q) for q in queries] == ["SMS_R_System?" + options] and len(timings) == 1
    assert len(_reads(queries)) == 1
    assert _bound(queries) is binds


def _sharepoint_list_client(items_seen: list):
    import httpx

    from app.data_sources.clients.graph_list_client import SharepointListsClient

    client = SharepointListsClient(site_url="https://lists.example.com/sites/x", access_token="tok")
    client._site_id = "lists.example.com,a,b"

    def respond(request):
        path = request.url.path
        if path.endswith("/lists"):
            payload = {"value": [{"id": "l1", "name": "2017_Expense_Data", "displayName": "2017_Expense_Data",
                                  "list": {"hidden": False, "template": "genericList"}}]}
        elif path.endswith("/columns"):
            payload = {"value": [{"name": "Title", "displayName": "Title", "text": {}}]}
        else:
            items_seen.append(str(request.url))
            payload = {"value": [{"id": "1", "fields": {"Title": "bob"}}]}
        return httpx.Response(200, json=payload)

    client._http = httpx.Client(transport=httpx.MockTransport(respond))
    return client


# A statement that is not text, whose str() is a query spec: in process the
# client reads it as that spec; sandboxed it crosses as its text.
SPEC_OBJECT = (
    "class Spec:\n"
    "    def __str__(self):\n"
    "        return '{\"list\": \"2017_Expense_Data\"}'\n"
)


@pytest.mark.parametrize("statement", ["Spec()", "0"])
def test_an_unguarded_call_whose_statement_is_not_text_fails_the_identity_check(mode, statement):
    items = []
    body = SPEC_OBJECT + (
        "try:\n"
        f"    rows = ds_clients['c'].execute_query({statement}, table_name='2017_Expense_Data')\n"
        "except Exception:\n"
        "    rows = None\n"  # a falsy statement names no list: nothing is sent
        "return rows"
    )
    queries, _, error = _run_capturing(_sharepoint_list_client(items), body)
    assert error is None and len(items) == (1 if statement == "Spec()" else 0)
    assert len(queries) == 1
    assert not _bound(queries)


def test_the_refusal_for_a_query_with_no_text_names_no_way_to_call_a_client():
    message = verify_identity_binds_in_queries([""], {"me": "alice"}, IDENTITY_ME)
    assert message is not None and "identity check" in message
    assert "first argument" not in message and "statement" not in message


def test_every_executed_call_to_an_unguarded_client_adds_exactly_one_captured_query():
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    class AnyStatement:
        def execute_query(self, query=None, **kwargs):
            return pd.DataFrame([{"n": 1}])

    queries = []
    wrapper = QueryCapturingClientWrapper(AnyStatement(), queries, [])
    statements = ["SELECT 1", 0, None, [], (), False, 0.0, 3.5, b"x", ("a",)]
    for statement in statements:
        wrapper.execute_query(statement)
    assert [str(q) for q in queries] == ["SELECT 1"] + [""] * (len(statements) - 1)
    # A dict statement is captured as its JSON text, keys sorted, characters
    # as written; one JSON cannot hold is captured blank.
    del queries[:]
    accented = "caf" + chr(0xE9)
    for statement in [{"table": "t", "query": "a=1"}, {"q": accented}, {1: "a", "b": 2}]:
        wrapper.execute_query(statement)
    assert [str(q) for q in queries] == ['{"query": "a=1", "table": "t"}', '{"q": "' + accented + '"}', ""]


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_executed_query_with_no_text_fails_the_identity_check(blank):
    from app.ai.code_execution.identity_bind import CapturedQuery

    scoped = CapturedQuery("$filter=Owner eq 'alice'", "PriorityErpClient")
    for queries in ([CapturedQuery(blank, "PriorityErpClient")], [scoped, CapturedQuery(blank, "MecmClient")],
                    [blank], ["SELECT * FROM t WHERE owner = 'alice'", blank]):
        assert verify_identity_binds_in_queries(queries, {"me": "alice"}, IDENTITY_ME) is not None
    assert verify_identity_binds_in_queries([scoped], {"me": "alice"}, IDENTITY_ME) is None
    assert verify_identity_binds_in_queries([], {"me": "alice"}, IDENTITY_ME) is None


# libpq and DuckDB read a query only up to its first NUL; the check reads
# past it. Anything after one, an identity filter included, would be checked
# and never run.
NUL = chr(0)


def _nul_templates(name):
    quote = '"' if name == "spl" else "'"
    templates = [
        "a = :v and b = 1" + NUL,
        "a = 1" + NUL + " and b = :v",
        "a = " + quote + "x" + NUL + quote + " and b = :v",
    ]
    mark = LINE_COMMENT_RENDERERS.get(name)
    if mark is not None:
        templates.append("a = 1 " + mark + " c" + NUL + NL + "and b = :v")
    return templates


@pytest.mark.parametrize("name", sorted(query_params._RENDERERS))
def test_a_nul_in_a_template_with_params_is_refused(name):
    renderer = query_params._RENDERERS[name]
    for template in _nul_templates(name):
        with pytest.raises(ParamError):
            render_sql_with_params(template, {"v": "x"}, renderer=renderer)
        with pytest.raises(ParamError):
            render_sql_with_params(template, {"v": 5}, renderer=renderer)


@pytest.mark.parametrize("name", sorted(query_params._RENDERERS))
@pytest.mark.parametrize("value", ["x" + NUL, NUL, ["a", "b" + NUL + "c"]], ids=["end", "alone", "list"])
def test_a_nul_in_a_value_is_refused(name, value):
    with pytest.raises(ParamError):
        render_sql_with_params("a = :v", {"v": value}, renderer=query_params._RENDERERS[name])


NUL_ENGINE_TEMPLATES = [
    "SELECT id FROM t WHERE id > 0 -- note" + NUL + NL + "AND owner = :me",
    "SELECT id FROM t WHERE id > 0" + NUL + " AND owner = :me",
]


@pytest.mark.parametrize("engine, family", [("duckdb", "CSVClient"), ("postgres", "PostgresqlClient")])
@pytest.mark.parametrize("template", NUL_ENGINE_TEMPLATES, ids=["comment", "code"])
def test_a_nul_that_ends_the_query_for_the_engine_is_refused_before_anything_is_sent(request, engine, family, template):
    run = _engine(engine, request)
    renderer = query_params.renderer_for_name(family)
    # As written today: the engine stops at the NUL, so the owner filter
    # never runs and every row comes back.
    assert run(template.replace(":me", renderer.quote("alice"))) == [1, 2, 3]
    assert run(render_sql_with_params(template.replace(NUL, ""), {"me": "alice"}, renderer=renderer)) == [1]
    with pytest.raises(ParamError):
        render_sql_with_params(template, {"me": "alice"}, renderer=renderer)


# Through the wrapper (StreamingCodeExecutor), in both sandbox modes.

@pytest.fixture(params=[
    "inprocess",
    pytest.param("subprocess", marks=pytest.mark.skipif(not _LINUX, reason="sandbox child needs Linux")),
])
def mode(request, monkeypatch):
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    if request.param == "inprocess":
        # In-process mode is for local development only; CI runs with
        # ENVIRONMENT=production, so the test names the environment itself.
        monkeypatch.setenv("ENVIRONMENT", "development")
    return request.param


def _run(clients, body: str):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    code = "def generate_df(ds_clients, excel_files):\n" + "".join(
        f"    {line}\n" for line in body.strip().splitlines()
    )
    return StreamingCodeExecutor().execute_code(code=code, ds_clients=clients, excel_files=[])


class _ConnectorWithoutRenderer(DiscoveryClient):
    """A connector nobody declared a parameter rendering for; records what
    reaches it (the driver boundary)."""

    def __init__(self):
        self.received: list = []

    def execute_query(self, query=None, **kwargs):
        self.received.append((query, kwargs))
        return pd.DataFrame([{"rows": 1}])


def test_params_on_a_connector_without_a_renderer_are_refused(mode):
    client = _ConnectorWithoutRenderer()
    with pytest.raises(ParamError):
        _run({"c": client}, "return ds_clients['c'].execute_query('owner = :v', params={'v': 'x'})")
    assert client.received == []


def test_a_keyword_only_call_with_params_on_such_a_connector_is_refused(mode):
    # Nothing renders params into a keyword call: refused, nothing sent.
    client = _ConnectorWithoutRenderer()
    with pytest.raises(ParamError, match="rendered only into a query string"):
        _run({"c": client}, "return ds_clients['c'].execute_query(table_name='t', params={'v': 'x'})")
    assert client.received == []


class _PlainObjectClient:
    """Not a connector (a test or helper object): today's ANSI rendering."""

    def __init__(self):
        self.received: list = []

    def execute_query(self, sql):
        self.received.append(sql)
        return pd.DataFrame([{"rows": 1}])


def test_params_on_an_object_that_is_not_a_connector_render_as_today(mode):
    client = _PlainObjectClient()
    _run({"c": client}, "return ds_clients['c'].execute_query('WHERE a = :v', params={'v': \"it's\"})")
    assert client.received == ["WHERE a = 'it''s'"]


def test_an_object_named_like_a_connector_is_read_back_as_the_ansi_it_was_rendered_with(mode):
    from app.ai.code_execution.client_profiles import family_name

    # Not a connector, so its params render ANSI; its captured query must not
    # be read back with the grammar of the connector whose name it shares.
    impostor = type("SalesforceClient", (_PlainObjectClient,), {})()
    _, _, captured = _run({"c": impostor}, "return ds_clients['c'].execute_query('WHERE a = :v', params={'v': \"it's\"})")
    assert impostor.received == ["WHERE a = 'it''s'"]
    assert [q.client_type for q in captured] == [family_name(impostor)]
    assert family_name(impostor) != "SalesforceClient"
    assert query_params.renderer_for_name(family_name(impostor)) is None


def test_a_keyword_call_whose_params_are_not_rendered_is_refused_logged_by_structure_and_recorded(mode, caplog):
    import logging

    class _Failing(_ConnectorWithoutRenderer):
        def execute_query(self, query=None, **kwargs):
            super().execute_query(query, **kwargs)
            raise RuntimeError("syntax error near the colon")

    logger = logging.getLogger("app.ai.code_execution.code_execution")
    logger.disabled = False  # alembic's fileConfig disables existing loggers
    client = _Failing()
    timings: list = []
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    code = ("def generate_df(ds_clients, excel_files):\n"
            "    return ds_clients['c'].execute_query(sql='SELECT * FROM t WHERE a = :owner', "
            "params={'owner': 'value-not-for-logs'})\n")
    with caplog.at_level(logging.WARNING, logger="app.ai.code_execution.code_execution"), pytest.raises(Exception):
        StreamingCodeExecutor().execute_code(code=code, ds_clients={"c": client}, excel_files=[],
                                             captured_timings=timings)
    assert client.received == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "params_not_rendered" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "_Failing" in warnings[0] and "keywords" in warnings[0] and "owner" in warnings[0]
    assert "value-not-for-logs" not in warnings[0]
    assert "first argument" in timings[-1]["error"]
    assert "value-not-for-logs" not in timings[-1]["error"]


def test_a_keyword_call_with_params_is_refused_before_it_can_time_out(mode):
    from app.ai.code_execution.code_execution import QueryTimeoutError, StreamingCodeExecutor

    class _Slow(_ConnectorWithoutRenderer):
        def execute_query(self, query=None, **kwargs):
            super().execute_query(query, **kwargs)
            raise QueryTimeoutError(5)

    client = _Slow()
    timings: list = []
    code = ("def generate_df(ds_clients, excel_files):\n"
            "    return ds_clients['c'].execute_query(sql='SELECT * FROM t WHERE a = :owner', "
            "params={'owner': 'value-not-for-logs'})\n")
    with pytest.raises(Exception):
        StreamingCodeExecutor().execute_code(code=code, ds_clients={"c": client}, excel_files=[],
                                             captured_timings=timings)
    assert client.received == []
    assert timings[-1].get("error_type") != "timeout"
    assert "first argument" in timings[-1]["error"]
    assert "value-not-for-logs" not in timings[-1]["error"]


def test_params_a_guarded_client_cannot_render_are_refused_logged_by_structure_and_recorded(mode, caplog):
    import logging

    from app.ai.code_execution.code_execution import StreamingCodeExecutor
    from app.data_sources.clients.sqlite_client import SqliteClient

    class _Recording(SqliteClient):
        def __init__(self):
            self.received: list = []

        def execute_query(self, sql, *args, **kwargs):
            self.received.append(sql)
            return pd.DataFrame([{"rows": 1}])

    logger = logging.getLogger("app.ai.code_execution.code_execution")
    logger.disabled = False  # alembic's fileConfig disables existing loggers
    client = _Recording()
    timings: list = []
    queries: list = []
    code = ("def generate_df(ds_clients, excel_files):\n"
            "    return ds_clients['c'].execute_query('SELECT * FROM t WHERE a = :owner', "
            "params=['value-not-for-logs'])\n")
    with caplog.at_level(logging.WARNING, logger="app.ai.code_execution.code_execution"), \
            pytest.raises(ParamError, match="first argument"):
        StreamingCodeExecutor().execute_code(code=code, ds_clients={"c": client}, excel_files=[],
                                             captured_timings=timings, captured_queries=queries)
    assert client.received == []
    assert [str(q) for q in queries] == ["SELECT * FROM t WHERE a = :owner"]
    assert len(timings) == 1 and "first argument" in timings[0]["error"]
    assert "value-not-for-logs" not in timings[0]["error"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "params_not_rendered" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "statement=str" in warnings[0] and "params=list" in warnings[0]
    assert "value-not-for-logs" not in warnings[0]


# A statement of any type to a connector nobody declared is refused
# (test_client_classification.py), after the params it did not render are logged.
@pytest.mark.parametrize("call, structure", [
    ("execute_query({'q': 'SELECT 1'}, params={'owner': 'value-not-for-logs'})", ["statement=dict", "owner"]),
    ("execute_query('SELECT * FROM t WHERE a = :owner', params=['value-not-for-logs'])",
     ["statement=str", "params=list"]),
])
def test_params_an_unguarded_connector_does_not_render_are_logged_by_structure(mode, caplog, call, structure):
    import logging

    from app.ai.code_execution.code_execution import UnsafeSQLError

    logger = logging.getLogger("app.ai.code_execution.code_execution")
    logger.disabled = False  # alembic's fileConfig disables existing loggers
    client = _ConnectorWithoutRenderer()
    with caplog.at_level(logging.WARNING, logger="app.ai.code_execution.code_execution"), \
            pytest.raises(UnsafeSQLError, match="does not accept free-text queries"):
        _run({"c": client}, "return ds_clients['c']." + call)
    assert client.received == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "params_not_rendered" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "_ConnectorWithoutRenderer" in warnings[0]
    assert all(part in warnings[0] for part in structure), warnings[0]
    assert "value-not-for-logs" not in warnings[0]


@pytest.mark.parametrize("statement", [
    "{'spl': 'search index=web user=:v'}",
    "'{\"spl\": \"search index=web user=:v\"}'",
])
def test_params_with_a_splunk_envelope_are_refused(mode, monkeypatch, statement):
    from app.data_sources.clients.splunk_client import SplunkClient

    sent = []
    monkeypatch.setattr("app.data_sources.clients.splunk_client.pinned_request",
                        lambda *a, **k: sent.append(a) or pytest.fail("nothing may be sent"))
    client = SplunkClient(host="splunk.example.com", api_token="t")
    with pytest.raises(ParamError):
        _run({"spl": client}, f"return ds_clients['spl'].execute_query({statement}, params={{'v': 'x'}})")
    assert sent == []


def test_params_on_a_csv_connection_render_for_duckdb(mode, tmp_path):
    from app.data_sources.clients.csv_client import CSVClient

    value = "it's \\ ok"
    pd.DataFrame({"id": [1, 2, 3], "v": ["plain", value, "it''s \\\\ ok"]}).to_csv(tmp_path / "items.csv", index=False)
    client = CSVClient(file_paths=f"{tmp_path}/*.csv")
    df, _, _ = _run({"files": client},
                    f"return ds_clients['files'].execute_query('SELECT id FROM items WHERE v = :v', params={{'v': {value!r}}})")
    assert df["id"].tolist() == [2]


# ---------------------------------------------------------------------------
# String literals per engine (main's per-client literal styles): each client
# class's declared sql_literal_style is a rule its renderer must honour
# ---------------------------------------------------------------------------

# (client module, client class, sqlglot dialect the rendered literal is read
# with). The second and third groups' engines read a backslash inside a
# string literal as an escape character. An engine sqlglot has no dialect
# for is read with one whose string-literal rules are the engine's (the
# client's sql_literal_style comment cites them). Values are rendered at the
# renderer seam, with the renderer the run's query wrapper picks for the
# client (`renderer_for`). (MsFabricClient and SybaseClient are left out:
# their modules need the ODBC driver to import. Cassandra, Cosmos DB,
# Couchbase and DynamoDB have no renderer: their params are refused,
# test_a_source_without_a_renderer_refuses_query_parameters.)
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
    # the Trino grammar. Its renderer refuses a backslash value.
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
    ("oracle_bi_client", "OracleBIClient", "snowflake"),
]
_BACKSLASH_LITERAL_CLIENTS = [
    ("bigquery_client", "BigqueryClient", "bigquery"),
    ("databricks_sql_client", "DatabricksSqlClient", "databricks"),
    ("spark_connect_client", "SparkConnectClient", "spark"),
    ("hive_client", "HiveClient", "hive"),
    ("salesforce_client", "SalesforceClient", "spark"),
]
_ALL_LITERAL_CLIENTS = _STANDARD_LITERAL_CLIENTS + _DOUBLED_LITERAL_CLIENTS + _BACKSLASH_LITERAL_CLIENTS
# Renderers that refuse a value holding a backslash rather than write it
# (no one rendering reads back as the value under every reading the engine
# may give it).
_REFUSES_A_BACKSLASH = {"athena", "redshift", "timbr", "ansi_or_backslash"}

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


def _client_class(module: str, class_name: str):
    import importlib

    path = module if module.startswith("app.") else f"app.data_sources.clients.{module}"
    return getattr(importlib.import_module(path), class_name)


def _renderer_of(module: str, class_name: str):
    renderer = query_params.renderer_for(object.__new__(_client_class(module, class_name)))
    assert renderer is not None, class_name
    return renderer


def _rendered_for(module: str, class_name: str, sql: str, values: dict) -> str:
    return render_sql_with_params(sql, values, renderer=_renderer_of(module, class_name))


@pytest.mark.parametrize("module,class_name,dialect", _ALL_LITERAL_CLIENTS)
@pytest.mark.parametrize("value", _AWKWARD_VALUES)
def test_a_string_param_stays_one_string_literal_in_its_engines_grammar(module, class_name, dialect, value):
    """Whatever the value holds (quotes, backslashes, a line break), the engine
    reads it back as exactly one string literal equal to the value: nothing of
    it is left outside the literal to be read as SQL. (sqlglot's token text is
    the literal's value with its escapes already read, so `\\n` in the
    literal compares equal to a line break in the value.) A renderer that
    refuses a backslash refuses only that."""
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import TokenType

    renderer = _renderer_of(module, class_name)
    if "\\" in value and renderer.name in _REFUSES_A_BACKSLASH:
        with pytest.raises(ParamError, match="backslash"):
            _rendered_for(module, class_name, "WHERE a = :v", {"v": value})
        return
    rendered = _rendered_for(module, class_name, "WHERE a = :v", {"v": value})
    assert rendered.startswith("WHERE a = ")
    tokens = Dialect.get_or_raise(dialect).tokenize(rendered[len("WHERE a = "):])
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
    rendered = _rendered_for(module, class_name, "WHERE a = :v", {"v": value})
    assert "\n" not in rendered and "\r" not in rendered, f"{class_name}: {rendered!r}"


def test_every_renderer_honours_its_clients_declared_literal_rule():
    """The renderer each client gets (client_profiles) against the literal
    rule its class declares (sql_literal_style): a "doubled" or "backslash"
    engine's renderer escapes a backslash or refuses it; a "standard"
    engine's literal reads back as the value where only '' escapes; a client
    declaring no rule renders only with a renderer written for its language
    (KQL, SPL) or not at all. OData values are percent-encoded text, checked
    by the OData tests above."""
    from sqlglot.dialects.dialect import Dialect

    from app.ai.code_execution.identity_bind import _STYLE_DIALECTS
    from tests.unit._client_enumeration import enumerated_client_classes

    checked = set()
    for cls in enumerated_client_classes():
        if not hasattr(cls, "sql_literal_style"):
            continue  # not a DataSourceClient (MCP, custom API, remote server)
        renderer = query_params.renderer_for(object.__new__(cls))
        style = cls.sql_literal_style
        if style is None:
            assert renderer is None or renderer.name in ("kql", "spl"), cls.__name__
            continue
        if renderer is None or renderer.name == "odata":
            continue
        for value in ("it's", "x\\", "a\\'b"):
            try:
                rendered = render_sql_with_params(":v", {"v": value}, renderer=renderer)
            except ParamError:
                assert "\\" in value and renderer.name in _REFUSES_A_BACKSLASH, (cls.__name__, value)
                continue
            if renderer.name in ("spl", "kql"):
                continue
            reading = _STYLE_DIALECTS.get(style, "")
            tokens = Dialect.get_or_raise(reading).tokenize(rendered.removeprefix("N"))
            assert [t.text for t in tokens] == [value], (cls.__name__, style, renderer.name, rendered)
        checked.add(cls.__name__)
    assert {"MysqlClient", "VerticaClient", "OracleBIClient", "HiveClient", "SalesforceClient",
            "IcebergClient", "DremioClient"} <= checked


# Sources that render with the plain doubled quote: a filtered dashboard over
# such a source keeps running, the value written as before.
_UNESTABLISHED_LITERAL_CLIENTS = [
    ("sisense_client", "SisenseClient"),
    ("timbr_client", "TimbrClient"),
]


@pytest.mark.parametrize("module,class_name", _UNESTABLISHED_LITERAL_CLIENTS)
def test_a_source_with_no_other_established_rule_keeps_the_standard_literal(module, class_name):
    assert _rendered_for(module, class_name, "WHERE a = :v", {"v": "it's"}) == "WHERE a = 'it''s'"


def _wrapped_recorder(module: str, class_name: str):
    """A bare instance of the real client class whose driver boundary
    records what it is handed: its class (and so its classification and
    renderer) stays the real one."""
    import pandas as pd

    client = object.__new__(_client_class(module, class_name))
    client.received = []

    def execute_query(query, *args, **kwargs):
        client.received.append(query)
        return pd.DataFrame({"ok": [1]})

    client.execute_query = execute_query
    return client


def _sent(module: str, class_name: str, query, values, captured=None):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _wrapped_recorder(module, class_name)
    QueryCapturingClientWrapper(client, captured if captured is not None else [], [],
                                query_timeout_seconds=30).execute_query(query, params=values)
    (received,) = client.received
    return received


def _refused(module: str, class_name: str, query, values, match=None):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    client = _wrapped_recorder(module, class_name)
    with pytest.raises(ParamError, match=match):
        QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30).execute_query(query, params=values)
    assert client.received == []


# Sources with no renderer for their query text (PromQL, a prompt, MDX, and
# the document stores' languages): parameters in text are refused. (KQL and
# SPL, where main refused too, have renderers of their own: see the ADX and
# Splunk tests above.)
_REFUSING_LITERAL_CLIENTS = [
    ("prometheus_client", "PrometheusClient"),
    ("timbr_a2a_client", "TimbrA2aClient"),
    ("analysis_services_client", "AnalysisServicesClient"),
    ("cassandra_client", "CassandraClient"),
    ("cosmosdb_client", "CosmosdbClient"),
    ("couchbase_client", "CouchbaseClient"),
    ("dynamodb_client", "DynamodbClient"),
]


@pytest.mark.parametrize("module,class_name", _REFUSING_LITERAL_CLIENTS)
def test_a_source_without_a_renderer_refuses_query_parameters(module, class_name):
    """No query is written with a quoting rule not established for its
    language: the run is refused with a ParamError, and the source receives
    nothing -- also when the query uses none of the values."""
    _refused(module, class_name, "query where owner = :v", {"v": "it's"}, match="not supported")
    _refused(module, class_name, "query with no placeholder", {"v": "it's"}, match="not supported")


@pytest.mark.parametrize("family", ["AzureDataExplorerClient", "SplunkClient"])
def test_a_query_with_no_placeholder_renders_unchanged_where_the_language_has_a_renderer(family):
    renderer = query_params.renderer_for_name(family)
    assert render_sql_with_params("StormEvents | take 10", {"v": "it's"}, renderer=renderer) == \
        "StormEvents | take 10"


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
    _refused(module, class_name, query, {"v": value})


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

    parsed = json.loads(_sent(module, class_name, query, {"n": number}))
    assert set(parsed) == keys

    def numbers(node):
        if isinstance(node, dict):
            return [n for v in node.values() for n in numbers(v)]
        if isinstance(node, list):
            return [n for v in node for n in numbers(v)]
        return [node] if isinstance(node, (int, float)) and not isinstance(node, bool) else []

    assert numbers(parsed) == [number]


# A placeholder inside a JSON string value is text in the connector's own
# language (a filter, a search, an envelope's SQL), which has no renderer:
# refused, whatever the value. (Main rendered it as the client's literal.)
_JSON_STRING_QUERIES = [
    ("intune_client", "IntuneClient", '{"table": "managed_devices", "filter": "complianceState eq :s"}',
     {"s": "noncompliant"}),
    ("servicenow_client", "ServiceNowClient", '{"table": "incident", "query": "priority=:p"}', {"p": 1}),
    ("servicenow_client", "ServiceNowClient", '{"table": "incident", "query": "caller=:c"}',
     {"c": 'a", "x": "y'}),
    ("elasticsearch_client", "ElasticsearchClient", '{"index": "orders", "query": {"term": {"owner": ":v"}}}',
     {"v": 'a"b'}),
    ("opensearch_client", "OpenSearchClient", '{"index": "orders", "query": {"term": {"owner": ":v"}}}',
     {"v": 'a"b'}),
]


@pytest.mark.parametrize("module,class_name,query,values", _JSON_STRING_QUERIES)
def test_a_placeholder_inside_a_json_string_is_refused(module, class_name, query, values):
    _refused(module, class_name, query, values, match="inside a string")


def test_a_placeholder_run_into_other_digits_is_refused():
    """`:n.5` is no placeholder value of the document: nothing may be left
    standing in for one."""
    _refused("servicenow_client", "ServiceNowClient", '{"table": "incident", "x": :n.5}', {"n": 5})


@pytest.mark.parametrize("values", [
    {"p": "x7301000000000000001", "c": " or true or model ne ", "me": "a@x.com"},
    {"p": "7301000000000000001", "c": "x", "me": "y"},
])
def test_a_value_holding_another_placeholders_stand_in_is_still_refused_inside_a_string(values):
    """The stand-in digits a placeholder is swapped for while rendering do
    not let a string placeholder through, whatever the values carry."""
    import json

    filter_text = "deviceName eq :p and model eq :c and userPrincipalName eq :me"
    _refused("intune_client", "IntuneClient", json.dumps({"table": "managed_devices", "filter": filter_text}),
             values, match="inside a string")


COMPACT_JSON_LITERALS = '{"table":"incident","active":true,"closed":false,"parent":null}'


def test_compact_json_literals_are_not_placeholders_without_params():
    assert query_params.render_json_number_params(COMPACT_JSON_LITERALS, {}) == COMPACT_JSON_LITERALS


def test_compact_json_literals_are_not_placeholders_with_params():
    import json

    query = COMPACT_JSON_LITERALS[:-1] + ',"limit": :n}'
    parsed = json.loads(_sent("servicenow_client", "ServiceNowClient", query, {"n": 5}))
    assert parsed == {"table": "incident", "active": True, "closed": False, "parent": None, "limit": 5}


def test_a_param_named_like_a_json_literal_does_not_replace_the_literal():
    import json

    rendered = query_params.render_json_number_params(
        COMPACT_JSON_LITERALS, {"true": 7, "false": 8, "null": 9})
    assert json.loads(rendered) == {"table": "incident", "active": True, "closed": False, "parent": None}


def test_a_placeholder_named_like_a_json_literal_still_renders_where_a_value_goes():
    import json

    rendered = query_params.render_json_number_params('{"limit": :null}', {"null": 3})
    assert json.loads(rendered) == {"limit": 3}


def _too_long_int():
    # Built here, never in a parametrize id: str() of it raises.
    return 10 ** 5000


@pytest.mark.parametrize("renderer", ["ansi", "postgres", "mysql"])
def test_an_integer_too_long_to_write_is_refused_with_the_param_error(renderer):
    with pytest.raises(ParamError, match="too long"):
        render_sql_with_params("SELECT * FROM t WHERE a = :v", {"v": _too_long_int()},
                               renderer=query_params._RENDERERS[renderer])
    with pytest.raises(ParamError, match="too long"):
        render_sql_with_params("SELECT * FROM t WHERE a IN :v", {"v": [1, _too_long_int()]},
                               renderer=query_params._RENDERERS[renderer])


def test_an_integer_too_long_to_write_is_refused_in_a_json_query():
    with pytest.raises(ParamError, match="too long"):
        query_params.render_json_number_params('{"table": "t", "limit": :n}', {"n": _too_long_int()})


@pytest.mark.parametrize("values", [
    {"a": 7301000000000000001, "b": 5},
    {"a": 7301000000000000000, "b": 7301000000000000001},
    {"a": 8301000000000000001, "b": -1.5},
])
def test_every_number_renders_in_one_pass_of_its_placeholders(values):
    """A number value that carries the digits of another placeholder's
    stand-in is written once, as itself: it never pulls in another value."""
    import json

    parsed = json.loads(_sent("servicenow_client", "ServiceNowClient",
                              '{"table": "t", "one": :a, "two": [:b, :a]}', values))
    assert parsed == {"table": "t", "one": values["a"], "two": [values["b"], values["a"]]}


def test_only_an_envelope_client_has_its_captured_sql_read_as_the_statement():
    """A JSON capture of a client that takes no SQL envelope keeps the check
    it had: its "esql"/"sql" keys mean nothing to that client."""
    from app.ai.code_execution.identity_bind import CapturedQuery

    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    text = '{"esql": "FROM t", "sql": "SELECT * FROM t WHERE owner = \'a@x.com\'"}'
    other_client = CapturedQuery(text, "MongodbClient", literal_style="standard")
    assert verify_identity_binds_in_queries([other_client], {"v": "a@x.com"}, specs) is None


def test_a_mongo_range_query_takes_a_number():
    import json

    parsed = json.loads(_sent("mongodb_client", "MongodbClient",
                              '{"collection": "orders", "find": {"qty": {"$gt": :n}}}', {"n": 5}))
    assert parsed == {"collection": "orders", "find": {"qty": {"$gt": 5}}}


def test_a_non_string_query_with_parameters_is_refused():
    """A query handed over as a dict is never rendered, so its placeholders
    would run as written: refused instead. So are params that are not a
    name -> value dict."""
    _refused("elasticsearch_client", "ElasticsearchClient",
             {"index": "logs", "query": {"term": {"owner": ":v"}}}, {"v": "a@x.com"})
    _refused("elasticsearch_client", "ElasticsearchClient", '{"index": "logs"}', [("v", 1)])


# Elasticsearch SQL / the OpenSearch SQL plugin, inside the JSON envelope's
# "sql" field: no renderer is declared for either, so a placeholder there is
# refused. (Main rendered it with the client's literal style.)
_ENVELOPE_SQL_CLIENTS = [
    ("elasticsearch_client", "ElasticsearchClient"),
    ("opensearch_client", "OpenSearchClient"),
]


@pytest.mark.parametrize("module,class_name", _ENVELOPE_SQL_CLIENTS)
@pytest.mark.parametrize("value", ["it's", 'x", "sql": "SELECT * FROM orders", "z": "', 5])
def test_a_param_in_the_envelopes_sql_is_refused(module, class_name, value):
    envelope = '{"index": "orders", "sql": "SELECT * FROM orders WHERE owner = :v"}'
    _refused(module, class_name, envelope, {"v": value}, match="inside a string")


@pytest.mark.parametrize("module,class_name", _ENVELOPE_SQL_CLIENTS)
def test_a_param_as_an_envelope_key_or_a_non_number_value_is_refused(module, class_name):
    for envelope in ('{"index": "orders", "query": {"term": {":v": "x"}}}',
                     '{"index": "orders", "size": :v}'):
        _refused(module, class_name, envelope, {"v": "a@x.com"})


def test_the_identity_check_reads_the_sql_inside_an_envelope():
    """The SQL of an Elasticsearch envelope is checked by its structure: a
    value present but not restricting the rows is not a filter."""
    from app.ai.code_execution.identity_bind import CapturedQuery

    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    scoped = CapturedQuery('{"sql": "SELECT * FROM t WHERE owner = \'a@x.com\'"}', "ElasticsearchClient",
                           literal_style="standard", sql_in_json_envelope=True)
    widened = CapturedQuery('{"sql": "SELECT * FROM t WHERE owner = \'a@x.com\' OR 1 = 1"}',
                            "ElasticsearchClient", literal_style="standard", sql_in_json_envelope=True)
    assert verify_identity_binds_in_queries([scoped], {"v": "a@x.com"}, specs) is None
    assert verify_identity_binds_in_queries([widened], {"v": "a@x.com"}, specs) is not None


def test_the_identity_check_reads_no_envelope_sql_another_key_runs_instead_of():
    """Elasticsearch runs "esql" (and a dashboard / saved search) before
    "sql": the envelope's SQL is then not the executed statement."""
    from app.ai.code_execution.identity_bind import CapturedQuery

    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    for first in ('"esql": "FROM t"', '"dashboard": "space/title"', '"saved_search": "space/s"'):
        capture = CapturedQuery('{' + first + ', "sql": "SELECT * FROM t WHERE owner = \'a@x.com\'"}',
                                "ElasticsearchClient", literal_style="standard", sql_in_json_envelope=True)
        assert verify_identity_binds_in_queries([capture], {"v": "a@x.com"}, specs) is not None, first


def test_the_wrapper_tags_a_capture_with_its_clients_literal_rule_and_envelope():
    captured: list = []
    _sent("elasticsearch_client", "ElasticsearchClient", '{"index": "logs", "size": :n}', {"n": 5},
          captured=captured)
    (capture,) = captured
    assert (capture.literal_style, capture.sql_in_json_envelope) == ("standard", True)


# Every connector's literal rule, written out: a new connector must be added
# here on purpose, not inherit "standard" unseen. "+envelope": the SQL in the
# JSON envelope's "sql" field. None: no rule established. (The renderer
# each one gets is checked against it in
# test_every_renderer_honours_its_clients_declared_literal_rule.)
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
    "ServiceNowClient": "standard", "SharepointClient": "standard",
    "SharepointListsClient": "standard", "SharepointOnpremClient": "standard", "SisenseClient": "standard",
    "SnowflakeClient": "doubled", "SparkConnectClient": "backslash", "SpectrumClient": "standard",
    "SplunkClient": None, "SqliteClient": "standard", "TableauClient": "standard",
    "TeradataClient": "standard", "TimbrA2aClient": "standard", "TimbrClient": "standard",
    "TrinoClient": "standard", "VCenterClient": "standard", "VerticaClient": "doubled",
    "XmlaClient": "standard", "ZabbixClient": "standard",
}


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
    quote and nothing else (Athena refuses a backslash instead: its
    renderer's reading and the engine's differ on one)."""
    cases = {
        "a backslash\\' before a quote, then more words": "'a backslash\\'' before a quote, then more words'",
        "ends with a backslash\\": "'ends with a backslash\\'",
        "it's": "'it''s'",
        "line one\nline two": "'line one\nline two'",
        "double \\\\ backslash": "'double \\\\ backslash'",
    }
    refuses = _renderer_of(module, class_name).name in _REFUSES_A_BACKSLASH
    for value, literal in cases.items():
        if refuses and "\\" in value:
            with pytest.raises(ParamError, match="backslash"):
                _rendered_for(module, class_name, "SELECT * FROM t WHERE a = :v", {"v": value})
            continue
        assert _rendered_for(module, class_name, "SELECT * FROM t WHERE a = :v", {"v": value}) == (
            "SELECT * FROM t WHERE a = " + literal)


@pytest.mark.parametrize("module,class_name,dialect", _ALL_LITERAL_CLIENTS)
@pytest.mark.parametrize("value", ["O'Brien", "CORP\\jdoe"])
def test_an_identity_value_with_a_quote_still_verifies_as_applied(module, class_name, dialect, value):
    """The identity check reads the executed SQL in the engine's grammar (its
    sqlglot dialect, else its renderer's, else one with the same literal
    rules as its declared style); a quote or a backslash in the identity
    value (a surname, a domain login) must still be found as the value the
    query filters on."""
    from app.ai.code_execution.client_profiles import family_name
    from app.ai.code_execution.identity_bind import CapturedQuery

    specs = _specs({"name": "v", "type": "string", "source": "identity"})
    cls = _client_class(module, class_name)
    if "\\" in value and _renderer_of(module, class_name).name in _REFUSES_A_BACKSLASH:
        with pytest.raises(ParamError, match="backslash"):
            _rendered_for(module, class_name, "SELECT * FROM t WHERE owner = :v", {"v": value})
        return
    rendered = _rendered_for(module, class_name, "SELECT * FROM t WHERE owner = :v", {"v": value})
    capture = CapturedQuery(rendered, family_name(object.__new__(cls)), literal_style=cls.sql_literal_style,
                            sql_in_json_envelope=cls.sql_in_json_envelope)
    assert verify_identity_binds_in_queries([capture], {"v": value}, specs) is None


# The connectors main added that the guard checks with a sqlglot dialect.

def test_an_iceberg_query_is_one_duckdb_select_over_catalog_tables_by_name():
    from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
    from app.data_sources.clients.iceberg_client import IcebergClient

    client = object.__new__(IcebergClient)
    assert guard_profile(client).dialect == "duckdb"
    assert ensure_read_only(client, "SELECT region, SUM(amount) FROM sales.orders GROUP BY region")
    for sql in ("SELECT * FROM read_csv('/etc/hosts')", "SELECT * FROM sales.orders, read_text('/etc/hosts')",
                "SELECT getenv('HOME')", "SELECT * FROM duckdb_settings()", "COPY sales.orders TO 'x.csv'",
                "SELECT * FROM 'data.parquet'"):
        with pytest.raises(ReadOnlyViolation):
            ensure_read_only(client, sql)


@pytest.mark.parametrize("module,class_name", [("dremio_client", "DremioClient"), ("hive_client", "HiveClient")])
def test_a_dremio_or_hive_query_with_a_backslash_is_refused(module, class_name):
    """Their engines' lexers are not pinned against sqlglot's: text whose
    reading turns on a backslash is refused, a rendered one included."""
    from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only

    client = object.__new__(_client_class(module, class_name))
    assert guard_profile(client).unknown_lexer
    assert ensure_read_only(client, "SELECT a FROM s.t WHERE b = 'x'")
    rendered = _rendered_for(module, class_name, "SELECT a FROM s.t WHERE b = :v", {"v": "it's \\ here"})
    with pytest.raises(ReadOnlyViolation, match="backslash"):
        ensure_read_only(client, rendered)


def test_dremio_refuses_a_double_slash_and_hive_a_block_comment():
    """Calcite (Dremio) reads `//` as a line comment; Hive releases differ on
    /* */ comments. Neither is read the way the checks read it."""
    from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only

    dremio = object.__new__(_client_class("dremio_client", "DremioClient"))
    hive = object.__new__(_client_class("hive_client", "HiveClient"))
    with pytest.raises(ReadOnlyViolation, match="//"):
        ensure_read_only(dremio, "SELECT a // '\nFROM s.t --'")
    assert ensure_read_only(dremio, "SELECT a / 2 FROM s.t WHERE b = '//'")
    with pytest.raises(ReadOnlyViolation, match="comment"):
        ensure_read_only(hive, "SELECT a FROM db.t /* x */")
    assert ensure_read_only(hive, "SELECT a FROM db.t WHERE b = '/* x */' -- note")


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
