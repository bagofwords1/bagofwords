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


@pytest.mark.parametrize("cls_path", [
    "app.data_sources.clients.oracle_bi_client.OracleBIClient",
    "app.data_sources.clients.sisense_client.SisenseClient",
    "app.data_sources.clients.vertica_client.VerticaClient",
    "app.data_sources.clients.netsuite_client.NetsuiteClient",
])
def test_ansi_connections_render_as_today_and_stay_unguarded(cls_path):
    client = _bare(cls_path)
    renderer = query_params.renderer_for(client)
    assert renderer.name == "ansi"
    assert guard_profile(client) is None
    assert render_sql_with_params("Name eq :v", {"v": "it's"}, renderer=renderer) == "Name eq 'it''s'"


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


def test_timbr_doubles_quotes_and_refuses_a_backslash_and_stays_unguarded():
    from app.data_sources.clients.timbr_client import TimbrClient

    client = object.__new__(TimbrClient)
    renderer = query_params.renderer_for(client)
    assert renderer.name == "timbr"
    assert guard_profile(client) is None
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
    monkeypatch.setattr(query_params, "_separated", lambda sql, start, text: text, raising=False)
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


def test_a_negative_number_is_spaced_only_after_a_minus():
    odata = query_params._RENDERERS["odata"]
    assert render_sql_with_params("Orders(:k)?$top=:n", {"k": -5, "n": -1}, renderer=odata) == "Orders(-5)?$top=-1"
    for name in ("ansi", "postgres", "kql"):
        renderer = query_params._RENDERERS[name]
        assert render_sql_with_params("f(:a, =:a, 1-:a)", {"a": -5}, renderer=renderer) == "f(-5, =-5, 1- -5)"


def test_an_odata_value_that_cannot_be_encoded_is_a_param_error():
    with pytest.raises(ParamError):
        render_sql_with_params("$filter=Name eq :v", {"v": "a" + chr(0xD800)}, renderer=query_params._RENDERERS["odata"])


def test_a_reading_count_that_differs_between_template_and_rendered_text_fails_closed(monkeypatch):
    # One list for the template, two for the rendered text: nothing may be
    # left unchecked.
    monkeypatch.setitem(query_params._READINGS, "ansi", lambda text: [[]] * (1 + (":" not in text)))
    with pytest.raises(ValueError):
        render_sql_with_params("a = :v", {"v": 5}, renderer=query_params._RENDERERS["ansi"])


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


def test_a_keyword_only_call_with_params_on_such_a_connector_is_unchanged(mode):
    client = _ConnectorWithoutRenderer()
    df, _, _ = _run({"c": client},
                    "return ds_clients['c'].execute_query(table_name='t', params={'v': 'x'})")
    assert df["rows"].tolist() == [1]
    assert client.received == [(None, {"table_name": "t"})]


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


def test_a_keyword_call_whose_params_are_not_rendered_is_logged_by_structure_and_noted_on_its_error(mode, caplog):
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
    assert client.received == [(None, {"sql": "SELECT * FROM t WHERE a = :owner"})]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "params_not_rendered" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "_Failing" in warnings[0] and "sql" in warnings[0] and "owner" in warnings[0]
    assert "value-not-for-logs" not in warnings[0]
    assert "first positional argument" in timings[-1]["error"]


def test_a_keyword_call_that_times_out_also_notes_its_params_were_not_rendered(mode):
    from app.ai.code_execution.code_execution import QueryTimeoutError, StreamingCodeExecutor

    class _Slow(_ConnectorWithoutRenderer):
        def execute_query(self, query=None, **kwargs):
            super().execute_query(query, **kwargs)
            raise QueryTimeoutError(5)

    timings: list = []
    code = ("def generate_df(ds_clients, excel_files):\n"
            "    return ds_clients['c'].execute_query(sql='SELECT * FROM t WHERE a = :owner', "
            "params={'owner': 'value-not-for-logs'})\n")
    with pytest.raises(Exception):
        StreamingCodeExecutor().execute_code(code=code, ds_clients={"c": _Slow()}, excel_files=[],
                                             captured_timings=timings)
    assert timings[-1]["error_type"] == "timeout"
    assert "first positional argument" in timings[-1]["error"]
    assert "value-not-for-logs" not in timings[-1]["error"]


@pytest.mark.parametrize("call, structure", [
    ("execute_query({'q': 'SELECT 1'}, params={'owner': 'value-not-for-logs'})", ["statement=dict", "owner"]),
    ("execute_query('SELECT * FROM t WHERE a = :owner', params=['value-not-for-logs'])", ["statement=str", "params=list"]),
])
def test_params_an_unguarded_connector_does_not_render_are_logged_by_structure(mode, caplog, call, structure):
    import logging

    logger = logging.getLogger("app.ai.code_execution.code_execution")
    logger.disabled = False  # alembic's fileConfig disables existing loggers
    client = _ConnectorWithoutRenderer()
    with caplog.at_level(logging.WARNING, logger="app.ai.code_execution.code_execution"):
        _run({"c": client}, "return ds_clients['c']." + call)
    assert len(client.received) == 1 and client.received[0][1] == {}
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
