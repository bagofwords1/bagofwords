"""A parameter value reaches each engine as exactly that value.

Contract: for every declared renderer and any string value, the literal the
renderer writes is read back by that engine's own grammar as one string
whose value is the input: never as more than one token, never as something
else. For SQL dialects the reader is sqlglot's tokenizer for the dialect (the
grammar the guard and table scope read the query with); for DuckDB also the
engine itself; for the renderers without a sqlglot dialect (ANSI, KQL, SOQL,
SPL) their own literal scanner. A value no rendering can carry safely on an
engine is refused.
"""
from __future__ import annotations

import duckdb
import pytest
import sqlglot
from sqlglot.tokens import TokenType

from app.ai.code_execution import query_params
from app.ai.code_execution.query_params import ParamError, render_sql_with_params

BACKSLASH = chr(92)
SHIN = chr(0x5E9)
VALUES = [
    "it's",
    "x" + BACKSLASH,
    BACKSLASH + "'",
    "DOMAIN" + BACKSLASH + "user",
    "a\nb",
    "".join(map(chr, (0x5E9, 0x5DC, 0x5D5, 0x5DD))),  # Hebrew
    "",
    "100%",
    ":x",
    "$$",
    'a"b',
    "a" + BACKSLASH + "nb",
    "''",
    "x" + BACKSLASH + BACKSLASH + "'",
]

# Family -> the sqlglot dialect the guard reads its SQL with.
SQL_FAMILIES = {
    "PostgresqlClient": "postgres",
    "AwsRedshiftClient": "redshift",
    "MysqlClient": "mysql",
    "MariadbClient": "mysql",
    "MSSQLClient": "tsql",
    "MsFabricClient": "fabric",
    "OracledbClient": "oracle",
    "SqliteClient": "sqlite",
    "SnowflakeClient": "snowflake",
    "BigqueryClient": "bigquery",
    "AwsAthenaClient": "athena",
    "PrestoClient": "presto",
    "TrinoClient": "trino",
    "ClickhouseClient": "clickhouse",
    "DatabricksSqlClient": "databricks",
    "SparkConnectClient": "spark",
    "DuckDBClient": "duckdb",
    "TeradataClient": "teradata",
    "DruidClient": "druid",
    # Renderer-only rows, read with the dialect their renderer generates.
    "TimbrClient": "spark",
    "PostHogClient": "clickhouse",
    "CSVClient": "duckdb",
    "QVDClient": "duckdb",
    "PBIXClient": "duckdb",
    "PowerBIReportServerClient": "duckdb",
    "FastQueryClient": "duckdb",
}

# Engines that do not read a backslash the way sqlglot's tokenizer does
# (approved: Athena's DML engine, Redshift): a backslash value is refused.
NO_BACKSLASH = {"AwsAthenaClient", "AwsRedshiftClient"}

STRING_TOKENS = {TokenType.STRING, TokenType.NATIONAL_STRING}


def _renderer(family):
    renderer = query_params.renderer_for_name(family)
    assert renderer is not None, family
    return renderer


def _rendered(family, value):
    return render_sql_with_params("SELECT :v AS c", {"v": value}, renderer=_renderer(family))


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("family", sorted(SQL_FAMILIES))
def test_a_sql_literal_reads_back_as_the_value_in_its_dialect(family, value):
    if family in NO_BACKSLASH and BACKSLASH in value:
        with pytest.raises(ParamError):
            _rendered(family, value)
        return
    tokens = sqlglot.tokenize(_rendered(family, value), read=SQL_FAMILIES[family])
    assert [t.token_type for t in tokens][0] == TokenType.SELECT
    assert len(tokens) == 4, tokens
    assert tokens[1].token_type in STRING_TOKENS and tokens[1].text == value, tokens
    assert (tokens[2].token_type, tokens[3].text) == (TokenType.ALIAS, "c"), tokens


@pytest.mark.parametrize("family", ["AwsRedshiftClient", "AwsAthenaClient"])
def test_quotes_double_where_a_backslash_is_refused(family):
    assert _rendered(family, "it's") == "SELECT 'it''s' AS c"


def test_tsql_keeps_the_national_prefix_only_for_text_outside_ascii():
    assert _rendered("MSSQLClient", "it's") == "SELECT 'it''s' AS c"
    assert _rendered("MsFabricClient", SHIN + "'") == "SELECT N'" + SHIN + "''' AS c"


@pytest.mark.parametrize("value", VALUES)
def test_the_duckdb_engine_reads_its_literal_as_the_value(value):
    rendered = render_sql_with_params("SELECT :v", {"v": value}, renderer=_renderer("CSVClient"))
    assert duckdb.sql(rendered).fetchone()[0] == value


# Renderers without a sqlglot dialect, read back with their own scanner.
SCANNED = {
    "Db2Client": "ansi",
    "IbmiClient": "ansi",
    "VerticaClient": "ansi",
    "SybaseClient": "ansi",
    "SapHanaClient": "ansi",
    "PinotClient": "ansi",
    "NetsuiteClient": "ansi",
    "OracleBIClient": "ansi",
    "SisenseClient": "ansi",
    "PriorityErpClient": "ansi",
    "SapDatasphereClient": "ansi",
    "MecmClient": "ansi",
    "AzureDataExplorerClient": "kql",
    "SalesforceClient": "soql",
    "SplunkClient": "spl",
}


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("family", sorted(SCANNED))
def test_a_scanned_literal_is_one_string_that_decodes_to_the_value(family, value):
    renderer = _renderer(family)
    assert renderer.name == SCANNED[family]
    text = render_sql_with_params("x = :v AND y = 1", {"v": value}, renderer=renderer)
    spans = renderer.string_spans(text)
    assert len(spans) == 1, (text, spans)
    start, end = spans[0]
    assert text[:start] == "x = " and text[end:] == " AND y = 1", text
    assert renderer.decode(text[start + 1:end - 1]) == value


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("family", sorted(set(SQL_FAMILIES) | set(SCANNED)))
def test_decode_inverts_the_body_quote_writes(family, value):
    renderer = _renderer(family)
    try:
        literal = renderer.quote(value)
    except ParamError:
        assert family in NO_BACKSLASH and BACKSLASH in value
        return
    body = literal[literal.index(literal[-1]) + 1:-1]
    assert renderer.decode(body) == value, literal


def test_kql_escapes_backslash_and_quote():
    assert _renderer("AzureDataExplorerClient").quote("D" + BACKSLASH + "u's") == "'D" + BACKSLASH * 2 + "u" + BACKSLASH + "'s'"


def test_soql_escapes_backslash_and_quote():
    assert _renderer("SalesforceClient").quote("D" + BACKSLASH + "u's") == "'D" + BACKSLASH * 2 + "u" + BACKSLASH + "'s'"


def test_spl_double_quotes_and_escapes_backslash_and_double_quote():
    assert _renderer("SplunkClient").quote("D" + BACKSLASH + 'u"s') == '"D' + BACKSLASH * 2 + "u" + BACKSLASH + '"s"'


def test_numbers_booleans_lists_and_null_keep_their_rendering_on_every_renderer():
    for family in sorted(set(SQL_FAMILIES) | set(SCANNED)):
        out = render_sql_with_params("a = :n AND b = :b AND c = :z AND d IN :l AND e IN :e",
                                     {"n": 5, "b": True, "z": None, "l": [1, 2], "e": []},
                                     renderer=_renderer(family))
        assert out == "a = 5 AND b = TRUE AND c = NULL AND d IN (1, 2) AND e IN (NULL)", family
