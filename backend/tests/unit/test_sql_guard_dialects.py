"""Text that sqlglot reads one way and the engine another is refused, for every dialect.

Contract: `ensure_read_only` reads the final text of every statement sent
(after params were rendered, or as written) for the gaps between sqlglot's
tokenizer and the engine's own lexer, and refuses the text where they
differ: a NUL (some readers stop there), comments one reader runs as code,
comment, string and name boundaries the two read differently, spaces and
digits outside ASCII that split or start names differently, and string
forms sqlglot cannot read. Redshift is read by the Postgres lexer in its
Redshift mode, and its canonical text is what is checked and sent. Clients
whose SQL sqlglot has no dialect for (Db2, IBM i, Vertica, Sybase, SAP
HANA, Pinot) are checked without a parser: one statement, a read verb
first, no write word. Table scope refuses what it cannot read either, and
compares Postgres names the way Postgres folds them.

Each shape is described generically. In each refused case the test also
shows what sqlglot's reading lets through: the tables it finds are only the
active ones, or the statement it finds is a read, while the engine's
grammar reads more. Where an engine runs in this process (DuckDB) the test
runs the shape on it too; the server engines are in
tests/integrations/test_sql_guard_dialects_live.py.
"""

from __future__ import annotations

import importlib

import duckdb
import pytest
from sqlglot import exp

from app.ai.code_execution.client_profiles import _CLIENT_TABLE, guard_profile
from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
from app.ai.code_execution.sql_parser import parse_guard_sql
from app.ai.code_execution.table_scope import (
    TABLE_SCOPE_ATTR,
    TableScope,
    TableScopeViolation,
    check_table_scope,
    dialect_for_client,
    enforce_table_scope,
    tables_read,
)

BS = chr(92)
NL = chr(10)
CR = chr(13)
NUL = chr(0)
NBSP = chr(0xA0)
# A digit outside ASCII (Arabic-Indic four).
ARABIC_FOUR = chr(0x664)
# Hebrew: "name".
HE_NAME = chr(0x5E9) + chr(0x5DD)
NON_ASCII_SPACES = [chr(c) for c in range(0x80, 0x110000) if chr(c).isspace()]

SCOPE = TableScope.from_names(active=["orders", "events"], inactive=["secret"])


def _client(family: str):
    """A real client of the registered family, with no connection."""
    for (module, name), entry in _CLIENT_TABLE.items():
        if entry.family == family:
            return object.__new__(getattr(importlib.import_module(module), name))
    raise KeyError(family)


def _families(kind: str) -> list[str]:
    return sorted(e.family for e in _CLIENT_TABLE.values() if e.profile is not None and e.profile.kind == kind)


SQL_FAMILIES = _families("sql")
# Families whose text goes through the Postgres lexer.
LEXED = {"PostgresqlClient", "AwsRedshiftClient"}
GUARDED = sorted(e.family for e in _CLIENT_TABLE.values() if e.profile is not None)


def _sqlglot_tables(sql: str, dialect: str) -> set:
    """The tables sqlglot's reading of `sql` reads by name."""
    return {ref for s in parse_guard_sql(sql, dialect) if s is not None for ref in tables_read(s)}


def _refused(family: str, sql: str, match: str | None = None) -> None:
    with pytest.raises(ReadOnlyViolation, match=match):
        ensure_read_only(_client(family), sql)


def _passes(family: str, sql: str) -> None:
    client = _client(family)
    checked = ensure_read_only(client, sql)
    check_table_scope(checked, dialect_for_client(client), SCOPE)


def _hidden_from_sqlglot(family: str, sql: str) -> None:
    """sqlglot reads only active tables in `sql`: without the new rules,
    table scope would let it run."""
    assert _sqlglot_tables(sql, dialect_for_client(_client(family))) <= {("orders",), ("events",)}


# --------------------------------------------------------------- NUL

def _nul_statement(family: str) -> str:
    kind = guard_profile(_client(family)).kind
    if kind == "spl":
        return "search index=web" + NUL + " | stats count"
    if kind == "cypher":
        return "MATCH (n) RETURN n" + NUL + " LIMIT 1"
    return "SELECT * FROM orders" + NUL + " WHERE owner = 'a'"


@pytest.mark.parametrize("family", GUARDED)
def test_a_nul_character_is_refused_for_every_guarded_client(family):
    _refused(family, _nul_statement(family), match="NUL")


def test_duckdb_stops_reading_at_a_nul_that_sqlglot_reads_past():
    sql = "SELECT * FROM orders" + NUL + " WHERE owner = 'a'"
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id INT, owner VARCHAR)")
    con.execute("INSERT INTO orders VALUES (1, 'a'), (2, 'b')")
    # sqlglot sees the filter; the engine drops it and returns every row.
    assert "WHERE" in parse_guard_sql(sql, "duckdb")[0].sql("duckdb")
    assert len(con.execute(sql).fetchall()) == 2
    _refused("DuckDBClient", sql, match="NUL")


# --------------------------------------------------------------- {# ... #}

@pytest.mark.parametrize("family", SQL_FAMILIES)
def test_a_brace_hash_mark_is_refused_in_every_dialect(family):
    sql = "SELECT * FROM orders {# UNION SELECT * FROM secret #}"
    if family not in LEXED:
        _hidden_from_sqlglot(family, sql)
    _refused(family, sql, match="{#")


@pytest.mark.parametrize("family", SQL_FAMILIES)
def test_a_brace_hash_inside_a_string_still_reads(family):
    _passes(family, "SELECT '{# x #}' AS a FROM orders")


# --------------------------------------------------------------- MySQL / MariaDB

EXECUTABLE_COMMENTS = [
    "SELECT * FROM orders /*! UNION SELECT * FROM secret */",
    "SELECT * FROM orders /*!50001 UNION SELECT * FROM secret */",
    "SELECT * FROM orders /*M! UNION SELECT * FROM secret */",
    "SELECT * FROM orders /*M!100000 UNION SELECT * FROM secret */",
    "SELECT * FROM orders WHERE 1 = 1 /*!UNION*/ SELECT * FROM secret",
]


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", EXECUTABLE_COMMENTS)
def test_an_executable_comment_is_refused_by_the_guard_and_by_table_scope(family, sql):
    if "/*!UNION*/" not in sql:
        _hidden_from_sqlglot(family, sql)
    _refused(family, sql, match="executable comment")
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE 1 = 1 --1 INTO OUTFILE '/tmp/x'",
    "SELECT * FROM orders WHERE id = 2 --1 UNION SELECT * FROM secret",
    "SELECT * FROM orders WHERE id = 1 --" + NBSP + "1 UNION SELECT * FROM secret",
])
def test_two_dashes_not_followed_by_a_space_are_refused_on_mysql(family, sql):
    # sqlglot reads a comment; MySQL reads two minus signs and the rest.
    assert not list(parse_guard_sql(sql, "mysql")[0].find_all(exp.Into))
    assert _sqlglot_tables(sql, "mysql") == {("orders",)}
    _refused(family, sql)


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders -- note",
    "SELECT * FROM orders --",
    "SELECT * FROM orders --\tnote",
    "SELECT '--x' AS a FROM orders",
    "SELECT * FROM orders /* --x */",
    "SELECT * FROM orders -- see --x" + NL + "LIMIT 5",
    "SELECT * FROM orders # note --x",
    "SELECT /*+ MAX_EXECUTION_TIME(1000) */ * FROM orders",
    "SELECT * FROM orders /* note! */",
    "SELECT '/*!' AS a FROM orders",
    "SELECT 3 - -1 AS a FROM orders",
])
def test_ordinary_mysql_comments_and_minus_signs_still_read(family, sql):
    _passes(family, sql)


# A decimal glued to a word: sqlglot reads one name (`1.5from`), MySQL and
# MariaDB read the number and then FROM (proven in the live dialect tests).
DIGIT_GLUED = [
    "SELECT x, 1.0from secret",
    "SELECT id FROM orders UNION SELECT x + 0.0from secret",
    "SELECT id FROM orders UNION SELECT x + 1.5e3from secret",
]


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", DIGIT_GLUED)
def test_a_number_glued_to_a_word_is_refused_on_mysql_by_the_guard_and_by_table_scope(family, sql):
    _hidden_from_sqlglot(family, sql)
    _refused(family, sql, match="starts with a digit")
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", [
    # A number after a lone dot, an exponent, a dot inside the name: the
    # server reads a number and then the word (live dialect tests).
    "SELECT id FROM orders UNION SELECT .5from secret",
    "SELECT id FROM orders UNION SELECT x, -.5from secret",
    "SELECT id FROM orders UNION SELECT x, 1e5from secret",
    "SELECT id FROM orders UNION SELECT x, 1e+5from secret",
    "SELECT id FROM orders UNION SELECT x, 1.e5from secret",
])
def test_a_name_starting_with_a_digit_that_the_server_reads_as_a_number_is_refused_on_mysql(family, sql):
    _refused(family, sql, match="backticks")


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", [
    "SELECT `1st`, id1, x1e5 FROM orders WHERE id = 1.5 LIMIT 10",
    "SELECT 1.5e3 AS a, .5 AS b, 0x1F AS c FROM orders",
    "SELECT '1.5from secret' AS a FROM orders",
    # Names the server reads as one name, as sqlglot does (live dialect
    # tests): digits then letters or underscores, after a table name too.
    "SELECT 2023_sales, 1st_quarter, 12abc, 1_000 FROM orders",
    "SELECT o.1st, o.2023_sales FROM orders o",
    "SELECT 1efrom, 1e FROM orders",
])
def test_numbers_and_names_starting_with_a_digit_that_the_server_reads_as_one_name_still_read_on_mysql(family, sql):
    _passes(family, sql)


# --------------------------------------------------------------- MySQL / MariaDB server modes

# Each hides a read of the inactive table from sqlglot, which reads MySQL's
# default mode, while a server in another mode ends the literal at the
# backslash (proven on mysql:8 and mariadb:11.4 in the live dialect tests).
MYSQL_MODE_HIDDEN = [
    # NO_BACKSLASH_ESCAPES: a backslash is a character, not an escape.
    "SELECT 'a" + BS + "' AS v, x FROM secret -- ' FROM orders",
    "SELECT N'a" + BS + "' AS v, x FROM secret -- ' FROM orders",
    # ANSI_QUOTES: "..." is a name, in which a backslash escapes nothing.
    "SELECT 1 AS \"a" + BS + "\", x FROM secret -- \" FROM orders",
]


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", MYSQL_MODE_HIDDEN)
def test_a_literal_that_a_server_mode_ends_elsewhere_is_refused_by_the_guard_and_by_table_scope(family, sql):
    _hidden_from_sqlglot(family, sql)
    _refused(family, sql, match="backslash")
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", [
    # A backslash that escapes a backslash, or a letter, ends no literal
    # elsewhere in any mode (rendered parameters double every backslash).
    "SELECT 'abc" + BS + BS + "' AS a, 'a" + BS + "nb' AS b FROM orders",
    "SELECT 'it''s' AS a, \"say \"\"hi\"\"\" AS b, \"a" + BS + BS + "\" AS c FROM orders",
    "SELECT * FROM orders WHERE note = '" + BS + BS + "'''",
])
def test_literals_every_server_mode_ends_at_the_same_place_still_read_on_mysql(family, sql):
    _passes(family, sql)


# MariaDB in MSSQL mode reads [...] as a quoted name; sqlglot reads a
# bracket and then a string (live dialect tests). Every other MySQL and
# MariaDB mode rejects a bracket outside a literal.
MYSQL_BRACKET_HIDDEN = [
    "SELECT 1 AS [a'], x FROM secret -- '] FROM orders",
    "SELECT [a'] AS v FROM (SELECT 1 AS [a']) t UNION ALL SELECT x FROM secret -- ']",
    "SELECT id FROM orders WHERE id = 1 ]",
]


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", MYSQL_BRACKET_HIDDEN)
def test_a_square_bracket_outside_a_literal_is_refused_on_mysql_by_the_guard_and_by_table_scope(family, sql):
    _refused(family, sql, match="bracket")
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "mysql", SCOPE)


@pytest.mark.parametrize("family", ["MysqlClient", "MariadbClient"])
@pytest.mark.parametrize("sql", [
    "SELECT JSON_EXTRACT(doc, '$[0]') AS j, `a[1]` FROM orders",
    "SELECT id FROM orders -- see [1]" + NL + "WHERE id = 1 /* [x] */",
])
def test_a_square_bracket_in_a_string_a_quoted_name_or_a_comment_still_reads_on_mysql(family, sql):
    _passes(family, sql)


@pytest.mark.parametrize("family", ["ClickhouseClient", "SparkConnectClient", "DatabricksSqlClient"])
def test_a_digit_led_name_still_reads_where_the_engine_never_splits_a_number_from_a_word(family):
    # ClickHouse 25.8 refuses a decimal glued to a word ("Wrong number") and
    # reads `1from` as one name (live dialect tests); Spark's lexer reads a
    # decimal only before a character that is not a letter, digit or
    # underscore (SqlBaseLexer isValidDecimal) and a digit-led word as one
    # IDENTIFIER. Neither reads a number and then FROM.
    ensure_read_only(_client(family), "SELECT 1abc FROM orders")


# --------------------------------------------------------------- ClickHouse

# ClickHouse reads the curly double quotes as name quotes and the curly
# single quotes as string quotes; sqlglot reads all four as name characters
# (live dialect tests, ClickHouse 25.8).
LDQ, RDQ, LSQ, RSQ = chr(0x201C), chr(0x201D), chr(0x2018), chr(0x2019)
CLICKHOUSE_QUOTE_HIDDEN = [
    "SELECT " + LSQ + "--" + RSQ + " AS a, x FROM secret",
    "SELECT 1 AS " + LDQ + "--" + RDQ + ", x FROM secret",
    "SELECT id FROM orders WHERE " + LSQ + "--" + RSQ + " = '--' UNION ALL SELECT x FROM secret",
    "SELECT " + LSQ + "--" + RSQ + " AS a, * FROM url('http://example.invalid/x', CSV)",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_QUOTE_HIDDEN)
def test_a_curly_quote_is_refused_on_clickhouse_by_the_guard_and_by_table_scope(sql):
    _hidden_from_sqlglot("ClickhouseClient", sql)
    _refused("ClickhouseClient", sql, match="curly quote")
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT 'it" + RSQ + "s " + LDQ + "x" + RDQ + "' AS a FROM orders",
    "SELECT \"a" + LSQ + "b\", `c" + RDQ + "` FROM orders",
])
def test_curly_quotes_inside_a_string_or_a_quoted_name_still_read_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


# ClickHouse reads a name on the right of IN as a table (a set); sqlglot
# reads a column (live dialect tests).
CLICKHOUSE_IN_TABLE = [
    "SELECT id FROM orders WHERE 42 IN secret",
    "SELECT id FROM orders WHERE 42 IN (secret)",
    "SELECT id FROM orders WHERE 42 GLOBAL IN secret",
    "SELECT id FROM orders WHERE 42 NOT IN secret",
    "SELECT id FROM orders WHERE 42 GLOBAL NOT IN secret",
    "SELECT id FROM orders WHERE 42 IN default.secret",
    "SELECT id FROM orders WHERE 42 IN (default.secret)",
    "SELECT id FROM orders WHERE (id, 1) IN `secret`",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_IN_TABLE)
def test_a_name_on_the_right_of_in_is_a_table_that_table_scope_checks_on_clickhouse(sql):
    _hidden_from_sqlglot("ClickhouseClient", sql)
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT id FROM orders WHERE id IN events",
    "SELECT id FROM orders WHERE id GLOBAL IN (events)",
    "SELECT id FROM orders WHERE id IN (1, 2)",
    "SELECT id FROM orders WHERE id IN (SELECT id FROM events)",
])
def test_in_over_an_active_table_a_list_or_a_subquery_still_reads_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


def test_the_tables_read_on_clickhouse_include_the_table_on_the_right_of_in():
    statement = parse_guard_sql("SELECT id FROM orders WHERE 42 NOT IN db.secret", "clickhouse")[0]
    assert tables_read(statement, "clickhouse") == [("orders",), ("db", "secret")]


# ClickHouse's IN operators as functions: the second argument is the set,
# read as a table when it is a name (live dialect tests).
CLICKHOUSE_IN_FUNCTIONS = [
    "globalIn", "notIn", "nullIn", "globalNotIn", "notNullIn", "globalNullIn", "globalNotNullIn",
    "inIgnoreSet", "globalInIgnoreSet", "notInIgnoreSet", "nullInIgnoreSet", "globalNotInIgnoreSet",
    "notNullInIgnoreSet", "globalNullInIgnoreSet", "globalNotNullInIgnoreSet",
]


@pytest.mark.parametrize("name", CLICKHOUSE_IN_FUNCTIONS)
def test_the_tables_read_on_clickhouse_include_the_set_of_an_in_function(name):
    for sql in (f"SELECT id FROM orders WHERE {name}(42, secret)",
                f"SELECT id FROM orders WHERE {name}(42, (db.secret))",
                f"SELECT id FROM orders WHERE `{name}`(42, `secret`)"):
        statement = parse_guard_sql(sql, "clickhouse")[0]
        assert tables_read(statement, "clickhouse")[-1][-1] == "secret", sql
        _hidden_from_sqlglot("ClickhouseClient", sql)
        with pytest.raises(TableScopeViolation, match="SECRET"):
            check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


# A WITH entry whose body is a name is an alias of that name on ClickHouse,
# so IN over the alias reads the table; sqlglot reads a CTE (live dialect
# tests).
CLICKHOUSE_WITH_ALIAS_IN = [
    "WITH secret AS s SELECT id FROM orders WHERE 42 IN s",
    "WITH secret AS s SELECT id FROM orders WHERE 42 GLOBAL IN (s)",
    "WITH (secret) AS s SELECT id FROM orders WHERE 42 NOT IN s",
    "WITH secret AS s, s AS t SELECT id FROM orders WHERE 42 IN t",
    "WITH db.secret AS s SELECT id FROM orders WHERE 42 IN s",
    "WITH secret AS s SELECT id FROM orders WHERE (42, 1) IN s",
    "SELECT * FROM (WITH secret AS s SELECT id FROM orders WHERE 42 IN s)",
    "WITH secret AS s SELECT id FROM orders WHERE id IN (SELECT id FROM events WHERE 42 IN s)",
    "WITH secret AS s SELECT id FROM orders WHERE globalIn(42, s)",
    "WITH secret AS s, (s) AS t SELECT id FROM orders WHERE nullIn(42, (t))",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_WITH_ALIAS_IN)
def test_in_over_a_with_alias_of_a_name_reads_that_table_on_clickhouse(sql):
    _hidden_from_sqlglot("ClickhouseClient", sql)
    statement = parse_guard_sql(sql, "clickhouse")[0]
    assert tables_read(statement, "clickhouse")[-1][-1] == "secret"
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "WITH 5 AS n SELECT id FROM orders WHERE id IN (n)",
    "WITH (1, 2, 3) AS ids SELECT id FROM orders WHERE id IN ids",
    "WITH [1, 2] AS ids SELECT id FROM orders WHERE has(ids, id)",
    "WITH events AS e SELECT id FROM orders WHERE id GLOBAL IN e",
    "WITH e AS (SELECT id FROM events) SELECT id FROM orders WHERE id IN e",
    "SELECT id FROM orders WHERE globalIn(id, (1, 2)) OR notIn(id, (SELECT id FROM events))",
    "SELECT id FROM orders WHERE nullIn(id, events)",
])
def test_scalar_with_aliases_and_in_functions_over_lists_queries_or_active_tables_still_read_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


# Functions that read a Join-engine table or a dictionary named in a string.
CLICKHOUSE_NAMED_READERS = [
    "SELECT joinGet('secret_join', 'x', toUInt64(1)) FROM orders",
    "SELECT joinGetOrNull('db.secret_join', 'x', toUInt64(1)) FROM orders",
    "SELECT dictGet('secret_dict', 'x', toUInt64(1)) FROM orders",
    "SELECT dictGetInt32OrDefault('secret_dict', 'x', toUInt64(1), 0) FROM orders",
    "SELECT dictHas('secret_dict', toUInt64(1)) FROM orders",
    "SELECT dictIsIn('secret_dict', toUInt64(1), toUInt64(2)) FROM orders",
    "SELECT dictGetHierarchy('secret_dict', toUInt64(1)) FROM orders",
    "SELECT dictGetChildren('secret_dict', toUInt64(1)) FROM orders",
    "SELECT dictGetDescendants('secret_dict', toUInt64(1), 0) FROM orders",
    "SELECT `dictGet`('secret_dict', 'x', toUInt64(1)) FROM orders",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_NAMED_READERS)
def test_a_function_that_reads_a_join_table_or_a_dictionary_by_name_is_refused_by_table_scope_on_clickhouse(sql):
    _hidden_from_sqlglot("ClickhouseClient", sql)
    with pytest.raises(TableScopeViolation, match="not allowed"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


def _sqlglot_cte_names(sql: str, dialect: str) -> set:
    """The WITH entry names sqlglot reads in `sql`, lowered as sqlglot
    compares them."""
    return {cte.alias_or_name.lower() for s in parse_guard_sql(sql, dialect) if s is not None
            for cte in s.find_all(exp.CTE)}


# A WITH entry stands for a table of its name only when it is a query named
# exactly as the table is written; otherwise ClickHouse reads the table
# (live dialect tests). sqlglot reads a CTE named `secret`.
CLICKHOUSE_WITH_NAMES_A_TABLE = [
    "WITH SECRET AS (SELECT 7 AS x) SELECT x FROM secret",
    "WITH Secret AS (SELECT 7 AS x) SELECT id FROM orders JOIN secret ON 1 = 1",
    "WITH 1 AS secret SELECT x FROM secret",
    "WITH (SELECT 7) AS secret SELECT x FROM secret",
    "WITH 'a' AS secret SELECT x FROM orders JOIN secret ON 1 = 1",
    "WITH 1 AS secret SELECT * FROM (SELECT x FROM secret)",
    "WITH 1 AS secret SELECT id FROM orders WHERE 42 IN (SELECT x FROM secret)",
    "WITH secret AS (SELECT 1), 1 AS secret SELECT x FROM secret",
    "WITH secret AS (SELECT 1) SELECT * FROM (WITH 1 AS secret SELECT x FROM secret)",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_WITH_NAMES_A_TABLE)
def test_a_scalar_or_differently_named_with_entry_does_not_stand_for_the_table_on_clickhouse(sql):
    assert "secret" in _sqlglot_cte_names(sql, "clickhouse")
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


# IN sets ClickHouse reads from the disabled table: an alias around the
# name, WITH entries followed through every entry of the WITH by exact name,
# and words sqlglot reads as keywords after IN (live dialect tests).
CLICKHOUSE_IN_SET_TABLE = [
    "SELECT id FROM orders WHERE globalIn(42, secret AS s)",
    "SELECT id FROM orders WHERE globalIn(42, (secret AS s))",
    "SELECT id FROM orders WHERE notIn(42, secret AS s)",
    "SELECT arrayMap(v -> globalIn(v, secret AS s), [42]) FROM orders",
    "SELECT arrayExists(v -> v IN secret, [42]) FROM orders",
    "WITH (secret AS x) AS s SELECT id FROM orders WHERE 42 IN s",
    "WITH secret AS s, (s AS q) AS t SELECT id FROM orders WHERE 42 IN t",
    "WITH orders AS t, secret AS orders SELECT 42 IN t",
    "WITH 1 AS SECRET SELECT id FROM orders WHERE 42 IN secret",
    "WITH 1 AS SECRET, secret AS s SELECT id FROM orders WHERE 42 IN s",
    "WITH 1 AS s, secret AS s SELECT id FROM orders WHERE 42 IN s",
    "WITH secret AS s, 1 AS s SELECT id FROM orders WHERE 42 IN s",
    "WITH secret AS s SELECT * FROM (WITH 1 AS s SELECT id FROM orders WHERE 42 IN s)",
    "WITH secret AS s SELECT id FROM orders WHERE 42 IN `s`",
    "WITH secret AS s SELECT id FROM orders WHERE id IN (s, 1)",
    "WITH secret AS s SELECT id FROM orders WHERE (id, 1) IN ((s, 1))",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_IN_SET_TABLE)
def test_an_in_set_that_names_a_disabled_table_through_aliases_or_with_entries_is_refused_on_clickhouse(sql):
    _hidden_from_sqlglot("ClickhouseClient", sql)
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


# IN sets table scope cannot read as one of its shapes: sqlglot left the
# set empty or read something else there, an IN function with another
# number of arguments, a WITH entry that holds neither a value, a name nor
# a query, a loop of WITH entries.
CLICKHOUSE_IN_SET_UNREAD = [
    "SELECT id FROM orders WHERE 42 IN sample",
    "SELECT id FROM orders WHERE 42 IN tablesample",
    "SELECT id FROM orders WHERE 42 IN fetch",
    "SELECT id FROM orders WHERE 42 IN notnull",
    "SELECT id FROM orders WHERE 42 IN current_date",
    "SELECT id FROM orders WHERE globalIn(42, current_date)",
    "SELECT id FROM orders WHERE id IN ()",
    "SELECT id FROM orders WHERE id IN tuple(1, 2)",
    "SELECT id FROM orders WHERE id IN (1, today())",
    "SELECT id FROM orders WHERE globalIn(42)",
    "SELECT id FROM orders WHERE globalIn(42, events, 1)",
    "WITH concat('se', 'cret') AS s SELECT id FROM orders WHERE 42 IN s",
    "WITH t AS s, s AS t SELECT id FROM orders WHERE 42 IN s",
    "SELECT id FROM orders WHERE 42 IN a.b.c.d",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_IN_SET_UNREAD)
def test_an_in_set_outside_the_shapes_table_scope_reads_is_refused_on_clickhouse(sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "WITH 5 AS n SELECT id FROM orders WHERE id IN (n, 1)",
    "WITH -5 AS n, n AS m SELECT id FROM orders WHERE id NOT IN m",
    "WITH [1, 2] AS ids SELECT id FROM orders WHERE id IN ids",
    "WITH ('a', 'b') AS ss SELECT id FROM orders WHERE status GLOBAL IN (ss)",
    "SELECT id FROM orders WHERE (id, 'a') IN ((1, 'a'), (2, 'b'))",
    "SELECT id FROM orders WHERE id IN [1, -2] AND flag IN (TRUE, NULL)",
    "WITH o AS (SELECT id FROM events) SELECT id FROM orders WHERE globalIn(id, o)",
    "WITH (SELECT max(id) FROM events) AS m SELECT id FROM orders WHERE id IN m",
    "SELECT id FROM orders WHERE id IN (SELECT id FROM events) AND notIn(id, events)",
    "WITH Orders AS (SELECT id FROM events) SELECT * FROM Orders",
])
def test_in_over_values_names_of_values_queries_and_active_tables_still_reads_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


def test_the_tables_read_on_clickhouse_follow_every_with_entry_of_an_in_set_name():
    sql = "WITH events AS t, secret AS events SELECT id FROM orders WHERE 42 IN t"
    statement = parse_guard_sql(sql, "clickhouse")[0]
    assert tables_read(statement, "clickhouse") == [("orders",), ("secret",)]


# Inside a WITH entry's own body its name is not the entry: ClickHouse reads
# the table of that name there, on a table reference (also in the first
# operand of a recursive entry's UNION) and as the set of an IN (live
# dialect tests). sqlglot reads the entry.
CLICKHOUSE_OWN_NAME_IN_BODY = [
    "WITH RECURSIVE secret AS (SELECT x FROM secret) SELECT x FROM secret",
    "WITH RECURSIVE secret AS (SELECT x FROM secret) SELECT sum(x) FROM secret",
    "WITH RECURSIVE secret AS (SELECT x FROM secret UNION ALL SELECT x FROM secret WHERE 0) SELECT x FROM secret",
    "WITH secret AS (SELECT 42 IN secret AS r) SELECT r FROM secret",
    "WITH secret AS (SELECT arrayFilter(v -> v IN secret, [7, 42]) AS r) SELECT r FROM secret",
    "WITH secret AS (SELECT arrayJoin([globalIn(42, secret), globalIn(7, secret)]) AS hit) SELECT hit FROM secret",
    "WITH (SELECT groupArray(n) FROM (SELECT arrayJoin(range(50)) AS n) WHERE n IN secret) AS secret SELECT secret",
    "WITH secret AS (SELECT * FROM (SELECT 42 IN secret AS r)) SELECT r FROM secret",
    "WITH secret AS (SELECT globalIn(42, secret) AS r) SELECT r FROM secret",
    "WITH (SELECT 42 IN secret) AS secret SELECT secret",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_OWN_NAME_IN_BODY)
def test_a_with_entry_s_own_name_inside_its_body_is_the_table_on_clickhouse(sql):
    assert "secret" in _sqlglot_cte_names(sql, "clickhouse")
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("family", ["PostgresqlClient", "MysqlClient", "MariadbClient", "ClickhouseClient"])
@pytest.mark.parametrize("sql", [
    "WITH RECURSIVE secret AS (SELECT x FROM secret) SELECT x FROM secret",
    "WITH RECURSIVE secret AS (SELECT x FROM secret UNION ALL SELECT x FROM secret WHERE 1 = 0) SELECT x FROM secret",
    "WITH RECURSIVE secret AS (SELECT x FROM secret EXCEPT SELECT x FROM secret) SELECT x FROM secret",
])
def test_a_recursive_entry_s_own_name_outside_the_recursive_part_of_its_union_is_the_table(family, sql):
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(ensure_read_only(_client(family), sql),
                          dialect_for_client(_client(family)), SCOPE)


RECURSIVE_SUM = "WITH RECURSIVE r AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM r WHERE x<3) SELECT sum(x) FROM r"


RECURSIVE_AND_LATER_ENTRIES = [
    "WITH RECURSIVE walk AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM walk WHERE x<3) SELECT sum(x) FROM walk",
    "WITH RECURSIVE walk AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM walk WHERE x < 3 AND 1 IN walk) "
    "SELECT sum(x) FROM walk",
    "WITH RECURSIVE walk AS (SELECT 1 AS x UNION ALL SELECT x FROM (SELECT x+1 AS x FROM walk WHERE x<3)) "
    "SELECT sum(x) FROM walk",
    "WITH a AS (SELECT 42 IN walk AS r), walk AS (SELECT 1 AS x) SELECT r FROM a",
]


@pytest.mark.parametrize("family, sql", [
    ("ClickhouseClient", RECURSIVE_SUM),
    ("PostgresqlClient", RECURSIVE_SUM),
    *[("ClickhouseClient", sql) for sql in RECURSIVE_AND_LATER_ENTRIES],
])
def test_a_recursive_entry_read_in_its_recursive_part_and_a_later_entry_in_an_in_set_still_read(family, sql):
    _passes(family, sql)


@pytest.mark.parametrize("sql", RECURSIVE_AND_LATER_ENTRIES)
def test_the_same_entries_named_after_a_disabled_table_are_refused(sql):
    # They read the entry, not the table, but an entry may not take a
    # disabled table's name: the over-refusal the name rule accepts.
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(sql.replace("walk", "secret"), "clickhouse", SCOPE)


def _alias_chain(levels: int, bottom: str) -> str:
    """A WITH whose entry `a{k}` holds the pair (a{k-1}, a{k-1}), down to
    `bottom` (the IN set names the top entry): every entry is reached by
    two ways from the one above it."""
    entries = [f"{bottom} AS a0"] + [f"(a{k - 1}, a{k - 1}) AS a{k}" for k in range(1, levels + 1)]
    return f"WITH {', '.join(entries)} SELECT id FROM orders WHERE id IN a{levels}"


def _nested_repeats(levels: int, bottom: str) -> str:
    """`levels` nested subqueries, each with a WITH entry `a{k}` holding
    the pair (a{k-1}, a{k-1}) of the entry around it, under an outer entry
    `a0` holding `bottom` (the IN set, innermost, names the deepest
    entry). Distinct names: entries of one name at several levels would
    name each other, which the loop check refuses."""
    sql = f"SELECT id FROM orders WHERE id IN a{levels}"
    for k in range(levels, 0, -1):
        sql = f"SELECT * FROM (WITH (a{k - 1}, a{k - 1}) AS a{k} {sql})"
    return f"WITH {bottom} AS a0 {sql}"


@pytest.mark.timeout(60)
@pytest.mark.parametrize("build", [_alias_chain, _nested_repeats])
def test_an_in_set_reached_through_30_levels_of_with_entries_is_read_once_per_entry(build):
    import time

    # CPU time of this process, so other workers (-n 8) do not count.
    started = time.process_time()
    check_table_scope(build(30, "orders"), "clickhouse", SCOPE)
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(build(30, "secret"), "clickhouse", SCOPE)
    assert time.process_time() - started < 1.0


def test_an_in_set_reached_through_more_with_entries_than_table_scope_follows_is_refused():
    entries = ["orders AS a0"] + [f"a{k - 1} AS a{k}" for k in range(1, 1200)]
    sql = f"WITH {', '.join(entries)} SELECT id FROM orders WHERE id IN a1199"
    with pytest.raises(TableScopeViolation, match="Could not verify which table this IN reads"):
        check_table_scope(sql, "clickhouse", SCOPE)


# WITH entries that name each other in a loop: while ClickHouse reads an
# entry it reads that entry's name as the table of that name, so the name
# that closes the loop reads the disabled table (live dialect tests). On a
# table reference, as the set of an IN, and as a scalar alias; wrapped in a
# UNION, a subquery, an inner WITH, nested subqueries, a WITH RECURSIVE;
# next to a recursive entry; over three entries.
CLICKHOUSE_WITH_CYCLES = [
    "WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT x FROM secret",
    "WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) "
    "SELECT x FROM secret UNION ALL SELECT x FROM secret",
    "WITH secret AS (SELECT x FROM orders), orders AS (SELECT 0 AS x WHERE 0 UNION ALL SELECT x FROM secret) "
    "SELECT x FROM secret",
    "SELECT * FROM (WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT x FROM secret)",
    "WITH secret AS (SELECT x FROM orders), orders AS (WITH q AS (SELECT x FROM secret) SELECT x FROM q) "
    "SELECT x FROM secret",
    "WITH secret AS (SELECT x FROM (SELECT x FROM orders)), orders AS (SELECT x FROM (SELECT x FROM secret)) "
    "SELECT x FROM secret",
    "WITH RECURSIVE secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT x FROM secret",
    "WITH a AS (SELECT arrayFilter(v -> v IN secret, range(100)) AS r), secret AS (SELECT r FROM a) "
    "SELECT r FROM secret",
    "WITH a AS (SELECT 42 IN secret AS r), secret AS (SELECT r FROM a) SELECT r FROM secret",
    "WITH (SELECT arrayFilter(v -> v IN secret, range(100))) AS r, secret AS (SELECT r AS x) SELECT x FROM secret",
    "WITH (SELECT 42 IN secret) AS r, secret AS (SELECT r + 100 AS v) SELECT 101 IN secret",
    "WITH RECURSIVE r AS (SELECT 1 AS x UNION ALL SELECT x+1 FROM r WHERE x<3), "
    "b AS (SELECT arrayFilter(v -> v IN secret, range(100)) AS a), secret AS (SELECT a FROM b) SELECT a FROM secret",
    "WITH secret AS (SELECT x FROM orders), c AS (SELECT x FROM secret), orders AS (SELECT x FROM c) "
    "SELECT x FROM secret",
    "WITH a AS (SELECT x FROM b), b AS (SELECT x FROM secret), secret AS (SELECT x FROM a) SELECT x FROM secret",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_WITH_CYCLES)
def test_with_entries_that_name_each_other_in_a_loop_are_refused_on_clickhouse(sql):
    # Refused first because an entry takes a disabled table's name; the
    # loop check refuses them too.
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)
    with pytest.raises(TableScopeViolation, match="refer to each other"):
        tables_read(parse_guard_sql(sql, "clickhouse")[0], "clickhouse")


# Where an engine resolves an entry's body where the entry is used
# (ClickHouse; MySQL, whose sqlglot dialect also serves MariaDB) or the
# dialect is not known, WITH entries that name each other are refused,
# across levels too.
LOOPS = [
    "WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT x FROM secret",
    "WITH secret AS (SELECT x FROM orders), c AS (SELECT x FROM secret), orders AS (SELECT x FROM c) "
    "SELECT x FROM secret",
    "SELECT * FROM (WITH a AS (SELECT x FROM (SELECT x FROM B)), b AS (SELECT x FROM a) SELECT x FROM a) s",
    # An entry that names itself next to two that name each other.
    "WITH orders AS (SELECT x FROM orders), a AS (SELECT x FROM b), b AS (SELECT x FROM a) SELECT x FROM a",
    # Across levels.
    "WITH secret AS (SELECT x FROM orders) SELECT x FROM (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) d",
    "WITH a AS (SELECT x FROM b) SELECT (WITH b AS (SELECT x FROM a) SELECT x FROM a) AS v",
]


@pytest.mark.parametrize("dialect", [None, "mysql", "clickhouse", "starrocks"])
@pytest.mark.parametrize("sql", LOOPS)
def test_the_tables_read_refuse_with_entries_that_name_each_other_where_entries_are_resolved_where_used(dialect,
                                                                                                         sql):
    statement = parse_guard_sql(sql, dialect)[0]
    with pytest.raises(TableScopeViolation, match="refer to each other"):
        tables_read(statement, dialect)


@pytest.mark.parametrize("dialect", ["postgres", "redshift", "tsql", "snowflake", "bigquery", "duckdb", "sqlite",
                                     "oracle", "trino", "databricks"])
@pytest.mark.parametrize("sql", LOOPS)
def test_the_tables_read_do_not_look_for_loops_where_an_entry_is_resolved_where_it_is_defined(dialect, sql):
    # Postgres 15 was measured reading each name where the entry is
    # defined; the name rule (an entry named after a disabled table) still
    # applies there.
    tables_read(parse_guard_sql(sql, dialect)[0], dialect)


def test_the_loop_check_runs_over_the_whole_statement_when_given_one_branch():
    # identity_bind asks about a UNION branch whose WITH stands on the UNION.
    sql = "WITH secret AS (SELECT x FROM orders), orders AS (SELECT x FROM secret) SELECT 1 UNION ALL SELECT 2"
    statement = parse_guard_sql(sql, "clickhouse")[0]
    branch = next(s for s in statement.find_all(exp.Select) if s.args.get("with") is None)
    with pytest.raises(TableScopeViolation, match="refer to each other"):
        tables_read(branch, "clickhouse")


# A WITH entry named after a disabled table. ClickHouse and MySQL 8.4
# resolve an entry's body where the entry is used, and read the name of an
# entry being resolved as the table of that name; ClickHouse calls a WITH
# lambda by its name. So a name sqlglot reads as the entry reaches the
# disabled table: across levels, in a scalar subquery, a UNION, a LATERAL,
# and through a lambda (also one named after a built-in, called inside
# another lambda, or written without parentheses). Each reads the disabled
# table on the engine (live dialect tests). The rule that refuses them: no
# WITH entry may take a disabled table's name.
NAMED_AFTER = "is not enabled for this agent, and a WITH entry may not take its name"
NAMED_AFTER_SECRET = "SECRET " + NAMED_AFTER
CROSS_LEVEL = [
    "WITH secret AS (SELECT x FROM orders) SELECT x FROM (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) d",
    "WITH secret AS (SELECT x FROM orders) SELECT (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) AS v",
    "WITH secret AS (SELECT x FROM orders) SELECT x FROM (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret "
    "UNION ALL SELECT x FROM secret) d",
    "WITH secret AS (SELECT x FROM orders) SELECT 1 AS x FROM orders WHERE 0 UNION ALL "
    "SELECT x FROM (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) d",
]
NAMED_AFTER_A_DISABLED_TABLE = [
    *[("ClickhouseClient", sql) for sql in CROSS_LEVEL],
    *[("MysqlClient", sql) for sql in CROSS_LEVEL],
    ("MysqlClient", "WITH secret AS (SELECT x FROM orders) SELECT * FROM (SELECT 1 AS k) z, "
                    "LATERAL (WITH orders AS (SELECT x FROM secret) SELECT x FROM secret) d"),
    ("ClickhouseClient", "WITH secret AS (SELECT F(1) AS x), (y -> (SELECT x FROM secret)) AS F SELECT x FROM secret"),
    ("ClickhouseClient", "WITH secret AS (SELECT length(1) AS x), (y -> (SELECT x FROM secret)) AS length "
                         "SELECT x FROM secret"),
    ("ClickhouseClient", "WITH secret AS (SELECT arrayMap(v -> F(v), [1]) AS x), (y -> (SELECT x FROM secret)) AS F "
                         "SELECT x FROM secret"),
    ("ClickhouseClient", "WITH secret AS (SELECT F(1) AS x), y -> (SELECT x FROM secret) AS F SELECT x FROM secret"),
    # The function form of a ClickHouse lambda.
    ("ClickhouseClient", "WITH secret AS (SELECT F(1) AS x), lambda(tuple(y), (SELECT x FROM secret)) AS F "
                         "SELECT x FROM secret"),
]


@pytest.mark.parametrize("family, sql", NAMED_AFTER_A_DISABLED_TABLE)
def test_a_with_entry_named_after_a_disabled_table_is_refused(family, sql):
    client = _client(family)
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(ensure_read_only(client, sql), dialect_for_client(client), SCOPE)


# An alias is not set aside as a WITH entry is: a name an alias shares with a
# disabled table is still read as that table wherever it is read as one. So
# an alias may take a disabled table's name (ClickHouse runs these and reads
# no other table: live dialect tests).
@pytest.mark.parametrize("sql", [
    "SELECT id AS secret FROM orders",
    "SELECT count() FROM orders WHERE (id + 1 AS Secret) > 0",
    "SELECT * FROM (SELECT (SELECT 42) AS secret FROM orders)",
    "SELECT count() AS events FROM orders",
    "SELECT customer_id, sum(amount) AS revenue FROM orders GROUP BY customer_id",
])
def test_an_alias_named_after_a_disabled_table_still_reads_on_clickhouse(sql):
    scope = TableScope.from_names(active=["default.orders"],
                                  inactive=["default.secret", "default.events", "default.revenue"])
    check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", scope)


# Disabled names the catalog splits differently from a plain name: a dot
# inside the name, brackets, a quote or a backtick inside, a leading or a
# trailing space. ClickHouse stores `database.table`. The entry, written
# quoted, takes the table's name; with the lambda rule and the loop check
# off, the name rule alone refuses it (ClickHouse reads the table: live
# dialect tests).
BT = chr(96)
ODD_TABLE_NAMES = ["my.table", "[hidden]", "it's", "tick" + BT + "name", " padded", "padded "]


def _odd_entry_sql(name: str) -> str:
    quoted = BT + name.replace(BT, BT + BT) + BT
    return (f"WITH {quoted} AS (SELECT F(1) AS x), lambda(tuple(y), (SELECT x FROM {quoted})) AS F "
            f"SELECT x FROM {quoted}")


@pytest.mark.parametrize("stored", ["default.{}", "{}"])
@pytest.mark.parametrize("name", ODD_TABLE_NAMES)
def test_the_name_rule_reads_a_disabled_name_and_an_entry_name_the_same_way(monkeypatch, name, stored):
    from app.ai.code_execution import table_scope

    monkeypatch.setattr(table_scope, "_check_with_loops", lambda statement, dialect=None: None)
    monkeypatch.setattr(table_scope, "_check_clickhouse_with_lambdas", lambda statement: None)
    scope = TableScope.from_names(active=["default.orders"], inactive=[stored.format(name)])
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER):
        check_table_scope(_odd_entry_sql(name), "clickhouse", scope)


@pytest.mark.parametrize("entry", ["`default.my.table`", "`my.table`", "`table`", "`[table]`", "` table`"])
def test_the_name_rule_refuses_an_entry_named_after_any_trailing_part_of_a_disabled_name(entry):
    scope = TableScope.from_names(active=["default.orders"], inactive=["default.my.table"])
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER):
        check_table_scope(f"WITH {entry} AS (SELECT 1 AS x) SELECT x FROM orders", "clickhouse", scope)


def test_the_name_rule_lets_an_entry_through_whose_name_only_ends_like_a_disabled_one():
    scope = TableScope.from_names(active=["default.orders"], inactive=["default.secret"])
    check_table_scope("WITH `x.secret` AS (SELECT id FROM orders) SELECT id FROM `x.secret`", "clickhouse", scope)
    check_table_scope("WITH secrets AS (SELECT id FROM orders) SELECT id FROM secrets", "clickhouse", scope)


@pytest.mark.parametrize("family, sql", NAMED_AFTER_A_DISABLED_TABLE + [
    ("ClickhouseClient", sql) for sql in CLICKHOUSE_WITH_CYCLES])
def test_the_name_rule_alone_refuses_every_shape(monkeypatch, family, sql):
    # With the loop check and the lambda rule off, the name rule still
    # refuses each shape.
    from app.ai.code_execution import table_scope

    monkeypatch.setattr(table_scope, "_check_with_loops", lambda statement, dialect=None: None)
    monkeypatch.setattr(table_scope, "_check_clickhouse_with_lambdas", lambda statement: None)
    client = _client(family)
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(ensure_read_only(client, sql), dialect_for_client(client), SCOPE)


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "tsql", "snowflake", "clickhouse"])
def test_the_name_rule_compares_the_last_part_of_every_disabled_name_in_any_case(dialect):
    scope = TableScope.from_names(active=["public.orders"], inactive=["gold.Secret"])
    sql = "WITH SECRET AS (SELECT id FROM orders) SELECT id FROM SECRET"
    with pytest.raises(TableScopeViolation, match=NAMED_AFTER_SECRET):
        check_table_scope(sql, dialect, scope)


def test_an_entry_named_after_an_enabled_table_or_no_table_still_reads():
    scope = TableScope.from_names(active=["orders", "events"], inactive=["secret"])
    for sql in ("WITH orders AS (SELECT * FROM orders WHERE x > 0) SELECT * FROM orders",
                "WITH secrets AS (SELECT id FROM orders) SELECT id AS secret_id FROM secrets"):
        for dialect in ("postgres", "mysql", "clickhouse"):
            check_table_scope(sql, dialect, scope)


# A WITH entry that is a lambda (ClickHouse), called by its name, which
# can be a built-in's: refused wherever it stands.
@pytest.mark.parametrize("sql", [
    "WITH x -> x + 1 AS f SELECT f(id) FROM orders",
    "WITH (x -> x + 1) AS f SELECT f(id) FROM orders",
    "WITH ((x) -> x + 1) AS f SELECT f(id) FROM orders",
    "WITH (x, y) -> x + y AS f SELECT f(id, 1) FROM orders",
    "WITH x -> length(x) AS length SELECT length('a')",
    "SELECT * FROM (WITH x -> x * 2 AS twice SELECT twice(id) FROM orders)",
])
def test_a_with_lambda_is_refused_on_clickhouse(sql):
    with pytest.raises(TableScopeViolation, match="lambda"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT arrayMap(v -> v * 2, [1, 2]) FROM orders",
    "WITH arrayMap(v -> v * 2, [1]) AS a SELECT a FROM orders",
    "SELECT arrayFilter((v, w) -> v > w, [1, 2], [2, 1]) AS r FROM orders",
])
def test_a_lambda_passed_to_a_function_still_reads_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


# ClickHouse's function form of a lambda, `lambda(tuple(args), body)`, as a
# WITH value (called by the entry's name, which can be a built-in's) or under
# an alias anywhere (called by the alias): refused like `x -> body`.
CLICKHOUSE_FUNCTION_LAMBDAS = [
    "WITH lambda(tuple(y), (SELECT x FROM secret)) AS F SELECT F(1)",
    "WITH (lambda(tuple(y), (SELECT x FROM secret))) AS F SELECT F(1)",
    "WITH lambda(tuple(y), (SELECT x FROM secret)) AS length SELECT length(1)",
    "WITH LAMBDA(tuple(y), y + 1) AS F SELECT F(1)",
    "SELECT arrayMap(lambda(tuple(y), (SELECT x FROM secret)) AS f, [1]), f(5)",
    "SELECT arrayMap((lambda(tuple(y), y + 1)) AS f, [1]), f(5)",
    "WITH tuple(lambda(tuple(y), (SELECT x FROM secret)) AS F) AS u SELECT F(1)",
    "SELECT lambda(tuple(y), y + 1) AS f FROM orders",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_FUNCTION_LAMBDAS)
def test_a_function_form_lambda_as_a_with_value_or_under_an_alias_is_refused_on_clickhouse(sql):
    with pytest.raises(TableScopeViolation, match="lambda"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT arrayMap(lambda(tuple(v), v + 1), [1])",
    "SELECT arrayMap(lambda(tuple(v), v + 1), [id]) AS r FROM orders",
    "WITH arrayMap(lambda(tuple(v), v * 2), [1]) AS a SELECT a FROM orders",
])
def test_a_function_form_lambda_passed_to_a_function_still_reads_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


# The set of an IN named like an alias defined anywhere in the statement
# (other than a WITH entry): ClickHouse may read the alias's value, here the
# disabled table, while table scope reads the enabled table of that name.
# Some of these read the disabled table on the engine and some do not
# (live dialect tests); each is refused.
CLICKHOUSE_IN_SET_NAMED_LIKE_AN_ALIAS = [
    "WITH tuple(secret AS orders) AS unused SELECT 42 IN orders",
    "WITH [secret AS orders] AS unused SELECT arrayFilter(v -> v IN orders, range(100))",
    "WITH tuple(secret AS orders) AS unused SELECT globalIn(42, orders)",
    "SELECT 42 IN orders, (secret AS orders) AS z",
    "SELECT 1 FROM orders WHERE (secret AS orders) IS NOT NULL OR 42 IN orders",
    "WITH [secret AS orders] AS unused SELECT 42 IN orders",
    "WITH (secret AS orders) AS unused SELECT 42 IN orders",
    "WITH tuple(secret AS ORDERS) AS unused SELECT 42 IN orders",
    "SELECT * FROM (WITH tuple(secret AS orders) AS unused SELECT 42 IN orders)",
    "WITH orders AS s, tuple(secret AS orders) AS u SELECT 42 IN s",
    "WITH tuple(secret AS orders) AS unused SELECT 42 IN default.orders",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_IN_SET_NAMED_LIKE_AN_ALIAS)
def test_an_in_set_named_like_an_alias_is_refused_on_clickhouse(sql):
    with pytest.raises(TableScopeViolation, match="named like an alias"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT id AS orders FROM orders WHERE id IN (SELECT id FROM orders)",
    "SELECT id AS n FROM orders WHERE id IN orders",
    "WITH secret AS orders SELECT * FROM orders",
])
def test_an_in_set_or_from_next_to_an_alias_of_another_name_still_reads_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


# The leak shapes above, with the disabled table renamed to a name the
# catalog splits differently from a plain one (written quoted), stay refused.
RENAMED_LEAK_SHAPES = [
    *NAMED_AFTER_A_DISABLED_TABLE,
    *[("ClickhouseClient", sql) for sql in CLICKHOUSE_WITH_CYCLES],
    *[("ClickhouseClient", sql) for sql in CLICKHOUSE_FUNCTION_LAMBDAS if "secret" in sql],
    *[("ClickhouseClient", sql) for sql in CLICKHOUSE_IN_SET_NAMED_LIKE_AN_ALIAS],
]


@pytest.mark.parametrize("name", ["my.table", "[hidden]"])
@pytest.mark.parametrize("family, sql", RENAMED_LEAK_SHAPES)
def test_the_leak_shapes_stay_refused_with_the_disabled_table_renamed(family, sql, name):
    import re

    renamed = re.sub(r"\bsecret\b", BT + name + BT, sql)
    assert renamed != sql
    scope = TableScope.from_names(active=["default.orders", "default.events"], inactive=["default." + name])
    client = _client(family)
    with pytest.raises((ReadOnlyViolation, TableScopeViolation)):
        check_table_scope(ensure_read_only(client, renamed), dialect_for_client(client), scope)


# ClickHouse decodes escapes in a quoted name (`\x74` is `t`); sqlglot keeps
# them as written, so the name it compares is not the one the engine reads.
CLICKHOUSE_ESCAPED_NAMES = [
    "WITH `secre" + BS + "x74` AS (SELECT x FROM orders) SELECT x FROM "
    "(WITH orders AS (SELECT x FROM `secre" + BS + "x74`) SELECT x FROM `secre" + BS + "x74`)",
    'WITH "secre' + BS + 'x74" AS (SELECT x FROM orders) SELECT x FROM '
    '(WITH orders AS (SELECT x FROM "secre' + BS + 'x74") SELECT x FROM "secre' + BS + 'x74")',
    "SELECT `i" + BS + "x64` FROM orders",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_ESCAPED_NAMES)
def test_a_backslash_in_a_quoted_name_is_refused_on_clickhouse(sql):
    _refused("ClickhouseClient", sql, match="backslash")
    with pytest.raises(TableScopeViolation, match="backslash"):
        check_table_scope(sql, "clickhouse", SCOPE)


def test_a_backslash_in_a_clickhouse_string_still_reads():
    _passes("ClickhouseClient", "SELECT id FROM orders WHERE note = 'a" + BS + BS + "b' OR note = '" + BS + "''")


# An entry that names itself is not a loop: inside its own body the name
# is the table of that name, checked against the scope like any table.
SHADOWING = "WITH orders AS (SELECT * FROM orders WHERE x > 0) SELECT * FROM orders"


@pytest.mark.parametrize("family", ["PostgresqlClient", "MysqlClient", "ClickhouseClient"])
def test_an_entry_named_after_the_table_it_reads_passes_when_that_table_is_enabled(family):
    _passes(family, SHADOWING)


@pytest.mark.parametrize("family", ["PostgresqlClient", "MysqlClient", "ClickhouseClient"])
def test_an_entry_named_after_the_table_it_reads_is_refused_when_that_table_is_disabled(family):
    client = _client(family)
    disabled = TableScope.from_names(active=["events"], inactive=["orders"])
    with pytest.raises(TableScopeViolation, match="ORDERS is not enabled"):
        check_table_scope(ensure_read_only(client, SHADOWING), dialect_for_client(client), disabled)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("sql", [
    RECURSIVE_SUM,
    "WITH RECURSIVE tree AS (SELECT id FROM orders WHERE id = 1 "
    "UNION ALL SELECT o.id FROM orders o JOIN tree t ON o.id = t.id + 1 WHERE tree.id < 3) SELECT id FROM tree",
    "WITH a AS (SELECT id FROM orders), b AS (SELECT id FROM a) SELECT id FROM b",
])
def test_a_recursive_entry_named_in_its_recursive_part_and_entries_read_in_order_are_not_a_loop(dialect, sql):
    tables_read(parse_guard_sql(sql, dialect)[0], dialect)


def test_an_entry_is_found_by_identity_where_another_entry_is_written_alike():
    from app.ai.code_execution.table_scope import _resolves_to_cte

    sql = "WITH b AS (SELECT x FROM m), m AS (SELECT 1 AS x), b AS (SELECT x FROM m) SELECT x FROM b"
    statement = parse_guard_sql(sql, "postgres")[0]
    first, _m, last = statement.args["with"].expressions
    assert first == last and first is not last
    # Inside the last entry, `m` (defined before it) is the entry; inside
    # the first one it is the table.
    assert _resolves_to_cte(next(last.find_all(exp.Table)), "postgres") is True
    assert _resolves_to_cte(next(first.find_all(exp.Table)), "postgres") is False


@pytest.mark.timeout(60)
@pytest.mark.parametrize("dialect, sql", [
    ("postgres", "SELECT 1 FROM " + ", ".join(f"t{k}" for k in range(20000))),
    ("clickhouse", "SELECT 1 FROM orders WHERE 42 IN (" + ", ".join(f"t{k}" for k in range(20000)) + ")"),
])
def test_the_tables_read_of_20000_names_take_linear_time(dialect, sql):
    import time

    statement = parse_guard_sql(sql, dialect)[0]
    # CPU time of this process, so other workers (-n 8) do not count.
    started = time.process_time()
    refs = tables_read(statement, dialect)
    assert time.process_time() - started < 1.0
    names = [ref for ref in refs if ref != ("orders",)]
    assert len(names) == 20000 and names[:2] == [("t0",), ("t1",)] and names[-1] == ("t19999",)


# Query settings that hold SQL (or anything outside the common ones) are
# refused, wherever the SELECT stands (live dialect tests).
CLICKHOUSE_SETTINGS_REFUSED = [
    "SELECT count() FROM orders SETTINGS additional_table_filters = {'orders': '42 IN secret'}",
    "SELECT count() FROM orders SETTINGS additional_result_filter = '1 IN secret'",
    "SELECT count() FROM orders SETTINGS parallel_replicas_custom_key = 'x IN secret'",
    "SELECT * FROM (SELECT count() FROM orders SETTINGS additional_table_filters = {'orders': '1'})",
    "SELECT id FROM orders WHERE id IN (SELECT id FROM events SETTINGS additional_result_filter = '1')",
    "SELECT count() FROM orders SETTINGS max_threads = 2, readonly = 0",
    "SELECT count() FROM orders SETTINGS max_threads = (SELECT 2)",
    "SELECT count() FROM orders SETTINGS MAX_THREADS = 2",
]


@pytest.mark.parametrize("sql", CLICKHOUSE_SETTINGS_REFUSED)
def test_a_query_setting_outside_the_common_ones_is_refused_by_table_scope_on_clickhouse(sql):
    with pytest.raises(TableScopeViolation, match="setting"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT count() FROM orders SETTINGS max_execution_time = 5",
    "SELECT count() FROM orders SETTINGS max_threads = 2, max_result_rows = 100, result_overflow_mode = 'break'",
    "SELECT * FROM (SELECT id FROM orders SETTINGS max_rows_to_read = 1000) SETTINGS join_use_nulls = 1",
])
def test_common_query_settings_still_read_on_clickhouse(sql):
    _passes("ClickhouseClient", sql)


@pytest.mark.parametrize("sql", [
    "SELECT hasColumnInTable('default', 'secret', 'x') FROM orders",
    "SELECT `hasColumnInTable`('db', 'secret', 'x') FROM orders",
    "SELECT dictGetAll('secret_dict', 'x', toUInt64(1)) FROM orders",
])
def test_a_function_that_reads_a_table_s_metadata_by_name_is_refused_by_table_scope_on_clickhouse(sql):
    _hidden_from_sqlglot("ClickhouseClient", sql)
    with pytest.raises(TableScopeViolation, match="not allowed"):
        check_table_scope(ensure_read_only(_client("ClickhouseClient"), sql), "clickhouse", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT x, -.5from secret",
    "SELECT id FROM orders UNION ALL SELECT 1-.5from secret",
])
def test_a_number_after_a_lone_dot_glued_to_a_word_is_refused_on_clickhouse(sql):
    # ClickHouse 25.8 reads the number and then FROM (live dialect tests).
    _refused("ClickhouseClient", sql, match="starts with a digit")


def test_a_digit_led_name_after_a_table_name_still_reads_on_clickhouse():
    _passes("ClickhouseClient", "SELECT o.5x FROM orders AS o")


# --------------------------------------------------------------- Druid

DRUID_HIDDEN = "SELECT 2 //* */ 1 AS a, '" + NL + "UNION SELECT * FROM secret --' FROM orders"


def test_a_double_slash_is_refused_on_druid():
    # Calcite reads `//` as a line comment (CommonParser.jj SINGLE_LINE_COMMENT);
    # sqlglot reads two slashes, a division.
    _hidden_from_sqlglot("DruidClient", DRUID_HIDDEN)
    _refused("DruidClient", DRUID_HIDDEN, match="//")


@pytest.mark.parametrize("sql", [
    "SELECT 'http://example.com' AS u FROM events",
    "SELECT COUNT(*) FROM events -- see http://example.com" + NL + "WHERE kind = 'a'",
    "SELECT COUNT(*) FROM events /* http://example.com */",
    "SELECT 4 / 2 AS a FROM events",
])
def test_a_double_slash_in_a_string_or_a_comment_still_reads_on_druid(sql):
    _passes("DruidClient", sql)


# --------------------------------------------------------------- BigQuery

BIGQUERY_HIDDEN = "SELECT `a" + BS + "`, '`, * FROM secret --' FROM `orders`"


def test_a_backslash_in_a_backtick_name_is_refused_on_bigquery():
    # BigQuery applies string escapes inside quoted names, so `\`` does not
    # end the name; sqlglot ends it there.
    _hidden_from_sqlglot("BigqueryClient", BIGQUERY_HIDDEN)
    _refused("BigqueryClient", BIGQUERY_HIDDEN, match="backslash")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM `orders` WHERE login = 'DOMAIN" + BS + BS + "user'",
    "SELECT `id` FROM `proj.dataset.orders`",
])
def test_bigquery_names_and_string_escapes_still_read(sql):
    ensure_read_only(_client("BigqueryClient"), sql)


# --------------------------------------------------------------- Spark / Databricks

SPARK_HIDDEN = "SELECT * FROM orders -- note" + BS + NL + "WHERE a = '" + NL + "UNION SELECT * FROM secret --'"


@pytest.mark.parametrize("family", ["SparkConnectClient", "DatabricksSqlClient"])
def test_a_line_comment_continued_by_a_backslash_is_refused_on_spark(family):
    # Spark's SqlBaseLexer SIMPLE_COMMENT goes on past a backslash-newline;
    # sqlglot ends the comment at the newline.
    _hidden_from_sqlglot(family, SPARK_HIDDEN)
    _refused(family, SPARK_HIDDEN, match="backslash")


@pytest.mark.parametrize("family", ["SparkConnectClient", "DatabricksSqlClient"])
@pytest.mark.parametrize("sql", [
    "-- note" + NL + "SELECT * FROM orders",
    "SELECT 'a" + BS + BS + "' AS a FROM orders -- note" + NL,
    "SELECT * FROM orders -- a " + BS + " in the middle" + NL + "LIMIT 5",
])
def test_spark_comments_and_backslash_strings_still_read(family, sql):
    _passes(family, sql)


# --------------------------------------------------------------- spaces, line breaks, names outside ASCII

# Engines shown to read these as whitespace: SQL Server 2019 every one;
# DuckDB 1.5.4 all but U+0085, U+1680, U+2028 and U+2029 (pinned below).
# U+0085, U+2028 and U+2029 are line breaks too, refused everywhere outside
# a string (readers end line comments at them differently).
_LINE_BREAKS = {chr(0x85), chr(0x2028), chr(0x2029)}
_DUCKDB_SPACES = {c for c in NON_ASCII_SPACES if ord(c) not in (0x85, 0x1680, 0x2028, 0x2029)}


def _engine_space(family: str, c: str) -> bool:
    if c in _LINE_BREAKS:
        return False
    return family == "MSSQLClient" or (family == "DuckDBClient" and c in _DUCKDB_SPACES)


@pytest.mark.parametrize("family", SQL_FAMILIES)
@pytest.mark.parametrize("space", NON_ASCII_SPACES, ids=lambda c: f"U+{ord(c):04X}")
def test_a_space_outside_ascii_is_refused_unless_the_engine_reads_it_as_one(family, space):
    sql = "SELECT id" + space + "FROM orders"
    if _engine_space(family, space):
        _passes(family, sql)
    else:
        _refused(family, sql)


def test_the_guard_knows_every_space_outside_ascii_that_sqlglot_reads_as_whitespace():
    from app.ai.code_execution import sql_guard

    assert sql_guard._NON_ASCII_SPACES == set(NON_ASCII_SPACES)


@pytest.mark.parametrize("family", SQL_FAMILIES)
def test_a_space_outside_ascii_inside_a_string_or_a_quoted_name_still_reads(family):
    quote = {"MysqlClient": "`", "MariadbClient": "`", "BigqueryClient": "`", "SparkConnectClient": "`",
             "DatabricksSqlClient": "`", "ClickhouseClient": '"'}.get(family, '"')
    _passes(family, "SELECT 'a" + NBSP + "b' AS " + quote + "x" + NBSP + "y" + quote + " FROM orders")


def test_duckdb_reads_these_spaces_outside_ascii_as_whitespace_and_the_others_as_name_characters():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id INT)")
    con.execute("INSERT INTO orders VALUES (1)")
    for c in NON_ASCII_SPACES:
        sql = "SELECT id" + c + "FROM orders"
        if c in _DUCKDB_SPACES:
            assert con.execute(sql).fetchall() == [(1,)], f"U+{ord(c):04X}"
        else:
            with pytest.raises(duckdb.BinderException):
                con.execute(sql)


@pytest.mark.parametrize("family", sorted(set(SQL_FAMILIES) - LEXED))
@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders -- note" + CR + "LIMIT 5",
    "SELECT * FROM orders -- note" + chr(0x0B) + "LIMIT 5",
    "SELECT * FROM orders /* note */" + chr(0x0C) + "LIMIT 5",
])
def test_a_line_break_other_than_a_newline_outside_a_string_is_refused(family, sql):
    _refused(family, sql, match="line break")


@pytest.mark.parametrize("family", SQL_FAMILIES)
def test_windows_line_ends_still_read(family):
    _passes(family, "SELECT id -- note" + CR + NL + "FROM orders")


# Dialects whose sqlglot tokenizer lets a name start with a digit: there it
# reads one name, as the engine does.
DIGIT_START_NAMES = {"MysqlClient", "MariadbClient", "SparkConnectClient", "DatabricksSqlClient", "TimbrClient",
                     "ClickhouseClient"}


@pytest.mark.parametrize("family", sorted(set(SQL_FAMILIES) - LEXED - DIGIT_START_NAMES))
def test_a_name_starting_with_a_digit_outside_ascii_is_refused(family):
    # sqlglot reads a number and then a name; engines that take characters
    # outside ASCII in names read one name.
    _refused(family, "SELECT " + ARABIC_FOUR + "abc FROM orders", match="outside ASCII")


@pytest.mark.parametrize("family", sorted(DIGIT_START_NAMES))
def test_a_name_starting_with_a_digit_outside_ascii_reads_where_sqlglot_reads_one_name(family):
    ensure_read_only(_client(family), "SELECT " + ARABIC_FOUR + "abc FROM orders")


@pytest.mark.parametrize("family", sorted(set(SQL_FAMILIES) - LEXED))
def test_a_letter_outside_ascii_that_sqlglot_reads_as_a_keyword_is_refused(family):
    # sqlglot upper-cases the long s to S and reads a keyword; an engine
    # that takes it in names reads a name.
    _refused(family, "SELECT * FROM orders " + chr(0x17F) + "elect", match="outside ASCII")


@pytest.mark.parametrize("family", SQL_FAMILIES)
def test_a_name_outside_ascii_that_sqlglot_reads_as_one_name_still_reads(family):
    ensure_read_only(_client(family), "SELECT " + HE_NAME + " FROM orders")


def test_duckdb_reads_a_name_that_starts_with_a_digit_outside_ascii_as_one_name():
    con = duckdb.connect()
    con.execute('CREATE TABLE orders ("' + ARABIC_FOUR + 'abc" INT)')
    con.execute("INSERT INTO orders VALUES (7)")
    sql = "SELECT " + ARABIC_FOUR + "abc FROM orders"
    # The engine reads the column; sqlglot reads the number 4 named abc.
    assert con.execute(sql).fetchall() == [(7,)]
    assert parse_guard_sql(sql, "duckdb")[0].expressions[0].alias == "abc"
    _refused("DuckDBClient", sql)


@pytest.mark.parametrize("sql", [
    "SELECT $t" + NBSP + "x$ab$t" + NBSP + "x$ AS v FROM orders",
    "SELECT $t" + chr(0x2028) + "$ab$t" + chr(0x2028) + "$ AS v FROM orders",
])
def test_a_dollar_tag_holding_a_space_outside_ascii_is_refused_on_duckdb(sql):
    _refused("DuckDBClient", sql)


E_STRING_HIDDEN = [
    "SELECT E'" + BS + "'' AS a, x FROM secret --' FROM orders",
    "SELECT e'" + BS + "'' AS a, x FROM secret --' FROM orders",
]


@pytest.mark.parametrize("sql", E_STRING_HIDDEN)
def test_duckdb_reads_a_backslash_escape_in_an_e_string_that_sqlglot_does_not(sql):
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (id INT)")
    con.execute("CREATE TABLE secret (x INT)")
    con.execute("INSERT INTO secret VALUES (42)")
    # sqlglot reads one string up to the last quote, then FROM orders;
    # DuckDB ends the string at the escaped quote and reads the secret table.
    assert _sqlglot_tables(sql, "duckdb") == {("orders",)}
    assert con.execute(sql).fetchall() == [("'", 42)]
    _refused("DuckDBClient", sql, match="E'")
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "duckdb", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT E'abc' AS a, 'a" + BS + "b' AS b FROM orders",
    "SELECT 'E" + BS + "' AS a FROM orders",
])
def test_an_e_string_without_a_backslash_and_plain_strings_still_read_on_duckdb(sql):
    _passes("DuckDBClient", sql)


def test_duckdb_reads_a_dollar_tag_holding_a_space_outside_ascii_as_one_tag():
    con = duckdb.connect()
    assert con.execute("SELECT $t" + NBSP + "x$ab$t" + NBSP + "x$ AS v").fetchall() == [("ab",)]


def test_a_dollar_string_with_a_tag_sqlglot_reads_alike_still_reads_on_duckdb():
    _passes("DuckDBClient", "SELECT $q$it's$q$ AS a, $$b$$ AS b FROM orders")


# --------------------------------------------------------------- SQL Server

@pytest.mark.parametrize("family", ["MSSQLClient", "MsFabricClient"])
@pytest.mark.parametrize("sql", [
    "SELECT x = 0x1into t2 FROM orders",
    "SELECT x = 0xinto t2 FROM orders",
    "SELECT x = 0x1Finto t2 FROM orders",
    "SELECT x = $1into t2 FROM orders",
    "SELECT x = " + chr(0x20AC) + "1into t2 FROM orders",
])
def test_a_hex_or_money_literal_glued_to_a_word_is_refused_on_sql_server(family, sql):
    # sqlglot reads one name; SQL Server reads a constant and then INTO.
    assert not list(parse_guard_sql(sql, "tsql")[0].find_all(exp.Into))
    _refused(family, sql)


@pytest.mark.parametrize("family", ["MSSQLClient", "MsFabricClient"])
@pytest.mark.parametrize("sql", [
    "SELECT $1.into t2 FROM orders",
    "SELECT x = $1.into t2 FROM orders",
    "SELECT x = " + chr(0xA3) + "1.into t2 FROM orders",
])
def test_a_money_constant_then_a_dot_and_a_word_is_refused_on_sql_server(family, sql):
    # sqlglot reads a name, a dot and a field named INTO: no INTO clause.
    # SQL Server reads the money constant 1. and then INTO a new table.
    assert not list(parse_guard_sql(sql, "tsql")[0].find_all(exp.Into))
    _refused(family, sql, match="currency sign")


@pytest.mark.parametrize("family", ["MSSQLClient", "MsFabricClient"])
@pytest.mark.parametrize("sql", [
    "SELECT $1.50 AS price FROM orders",
    "SELECT " + chr(0x20AC) + "2 AS price FROM orders",
    "SELECT $IDENTITY FROM orders",
])
def test_every_currency_sign_starting_a_word_is_refused_on_sql_server(family, sql):
    # sqlglot reads a money constant as a name (and then a dot), never as
    # the constant SQL Server reads.
    _refused(family, sql, match="currency sign")


@pytest.mark.parametrize("family", ["MSSQLClient", "MsFabricClient"])
@pytest.mark.parametrize("sql", [
    "SELECT 0x1F AS a, 0xFF AS b FROM orders",
    "SELECT x = 0x1F FROM orders",
    "SELECT CAST(1.50 AS money) AS price FROM orders",
    "SELECT '$1.into' AS a, [$1] AS b FROM orders",
    "SELECT __$operation, a$b FROM orders -- costs $5",
])
def test_hex_literals_and_names_or_strings_holding_a_currency_sign_still_read_on_sql_server(family, sql):
    _passes(family, sql)


# --------------------------------------------------------------- nested comments

NESTED = "SELECT * FROM orders /* /* */ UNION SELECT * FROM secret -- */"


@pytest.mark.parametrize("family", ["DruidClient", "TeradataClient", "ClickhouseClient", "MsFabricClient"])
def test_a_comment_inside_a_comment_is_refused_where_the_engine_may_not_nest_them(family):
    # sqlglot nests block comments here; an engine that does not reads the
    # text after the first `*/` as code.
    _hidden_from_sqlglot(family, NESTED)
    _refused(family, NESTED, match="comment inside")


@pytest.mark.parametrize("family", ["MSSQLClient", "DuckDBClient", "SparkConnectClient", "DatabricksSqlClient",
                                    "PostgresqlClient"])
def test_a_comment_inside_a_comment_still_reads_where_the_engine_nests_them(family):
    _passes(family, "SELECT * FROM orders /* a /* b */ c */")


def test_duckdb_nests_block_comments():
    assert duckdb.connect().execute("SELECT 1 /* /* */ x */ AS v").fetchall() == [(1,)]


def test_a_comment_inside_a_comment_is_refused_on_redshift():
    _refused("AwsRedshiftClient", "SELECT * FROM orders /* a /* b */ c */", match="comment inside")


# --------------------------------------------------------------- Athena

def test_a_backslash_outside_a_quoted_name_is_refused_on_athena():
    # Athena routes text to a Hive-based or a Trino-based engine, which read
    # a backslash in a string differently; so does sqlglot's Athena
    # tokenizer, depending on the statement. Refused before sqlglot runs.
    sql = "SELECT 'x" + BS + "', (SELECT max(a) FROM secret) --' AS v FROM orders"
    _refused("AwsAthenaClient", sql, match="backslash")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE regexp_like(code, '" + BS + "d+')",
    "SELECT * FROM orders -- a " + BS + " in a comment",
    "SELECT 'it''s' AS a, '" + BS + "' AS b FROM orders",
])
def test_athena_refuses_every_backslash_outside_a_quoted_name(sql):
    _refused("AwsAthenaClient", sql, match="backslash")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE status = 'open' LIMIT 10",
    'SELECT "a' + BS + 'b" FROM orders',
    "SELECT '\"' AS q, \"x\" FROM orders",
])
def test_athena_reads_without_a_backslash_outside_quoted_names_still_pass(sql):
    ensure_read_only(_client("AwsAthenaClient"), sql)


def test_athena_does_not_pair_a_double_quote_inside_a_string_with_a_later_one():
    _refused("AwsAthenaClient", "SELECT '\"', '" + BS + "' AS b, \"x\" FROM orders", match="backslash")


# --------------------------------------------------------------- Oracle

@pytest.mark.parametrize("sql", [
    "SELECT q'[it's]' AS a FROM orders",
    "SELECT Q'{a}' AS a FROM orders",
    "SELECT nq'<x>' AS a FROM orders",
    "SELECT NQ'!x!' AS a FROM orders",
    "SELECT Nq'[a]' AS a, nQ'[b]' AS b FROM orders",
    "SELECT * FROM orders WHERE note = q'[it's]'",
    "SELECT * FROM orders WHERE note = q'[x]' UNION SELECT * FROM orders",
    "SELECT * FROM orders WHERE note = Q'{a'b}'",
])
def test_an_alternative_quote_string_is_refused_on_oracle(sql):
    _refused("OracledbClient", sql, match="alternative-quote")


@pytest.mark.parametrize("sql", [
    "SELECT N'text' AS a FROM orders",
    "SELECT * FROM orders WHERE name = 'it''s'",
    "SELECT q FROM orders WHERE q = 'x'",
    "SELECT seq FROM orders WHERE seq='x'",
    "SELECT * FROM orders WHERE note = 'q''x'",
    "SELECT \"q'\" FROM orders",
    "SELECT * FROM orders -- q'[x]'",
])
def test_standard_literals_and_names_spelled_with_q_still_read_on_oracle(sql):
    _passes("OracledbClient", sql)


# --------------------------------------------------------------- Redshift

@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE login = 'DOMAIN" + BS + "user'",
    "SELECT * FROM orders WHERE note = 'x" + BS + "' UNION SELECT * FROM secret --'",
])
def test_a_backslash_in_a_redshift_string_is_refused(sql):
    _refused("AwsRedshiftClient", sql, match="backslash")


def test_an_e_string_is_refused_on_redshift():
    _refused("AwsRedshiftClient", "SELECT E'x' AS a FROM orders", match="E'")


def test_a_redshift_read_comes_back_as_the_lexer_s_canonical_text():
    sql = "SELECT 'it''s' AS a /* note */ FROM orders -- end"
    assert ensure_read_only(_client("AwsRedshiftClient"), sql) == "SELECT 'it''s' AS a   FROM orders  "


def test_redshift_refuses_more_than_one_statement_as_the_lexer_reads_it():
    _refused("AwsRedshiftClient", "SELECT 1; SELECT 2", match="single")


# --------------------------------------------------------------- clients without a parser dialect

UNPARSED_FAMILIES = ["VerticaClient", "SybaseClient", "SapHanaClient", "PinotClient"]


@pytest.mark.parametrize("family", UNPARSED_FAMILIES)
@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET a = 1",
    "INSERT INTO orders VALUES (1)",
    "CREATE TABLE t (a INT)",
    "DROP TABLE orders",
    "SELECT 1; DELETE FROM orders",
    "SELECT * INTO t2 FROM orders",
    "SELECT * FROM orders /* /* */ '; DROP TABLE orders --' */",
])
def test_a_client_without_a_parser_dialect_refuses_writes_and_second_statements(family, sql):
    _refused(family, sql)


@pytest.mark.parametrize("family", UNPARSED_FAMILIES)
@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders WHERE status = 'delete me' LIMIT 10",
    "WITH t AS (SELECT * FROM orders) SELECT COUNT(*) FROM t",
    "(SELECT 1)",
    "VALUES (1)",
    "SELECT * FROM orders -- note",
])
def test_a_client_without_a_parser_dialect_lets_reads_through(family, sql):
    assert ensure_read_only(_client(family), sql) == sql


@pytest.mark.parametrize("family", UNPARSED_FAMILIES)
def test_a_subclass_of_a_client_without_a_parser_dialect_is_guarded(family):
    parent = type(_client(family))
    child = object.__new__(type("Custom" + parent.__name__, (parent,), {}))
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(child, "DELETE FROM orders")


def test_vertica_reads_a_backslash_escape_in_an_e_string():
    _refused("VerticaClient", "SELECT E'" + BS + "'' ; DELETE FROM orders --' AS a")


def test_pinot_reads_a_double_slash_as_a_comment():
    _refused("PinotClient", "SELECT 1 // '" + NL + "; DELETE FROM orders --'", match="//")


def test_db2_refuses_a_comment_inside_a_comment():
    _refused("Db2Client", "SELECT 1 FROM SYSIBM.SYSDUMMY1 /* /* */ ' */ ; DELETE FROM T --'")


NONPARSING_FAMILIES = UNPARSED_FAMILIES + ["Db2Client", "IbmiClient"]


@pytest.mark.parametrize("family", NONPARSING_FAMILIES)
@pytest.mark.parametrize("brk", [CR, chr(0x0B), chr(0x0C), chr(0x85), chr(0x2028)], ids=lambda c: f"U+{ord(c):04X}")
def test_a_line_break_other_than_a_newline_is_refused_without_a_parser_dialect(family, brk):
    # The checks end a -- comment at a newline only; an engine that ends
    # it at this break reads the second statement.
    _refused(family, "SELECT 1 FROM t -- x" + brk + "; DELETE FROM orders", match="line break")


@pytest.mark.parametrize("family", NONPARSING_FAMILIES)
def test_windows_line_ends_still_read_without_a_parser_dialect(family):
    sql = "SELECT id -- note" + CR + NL + "FROM orders"
    assert ensure_read_only(_client(family), sql) == sql


@pytest.mark.parametrize("family", NONPARSING_FAMILIES)
def test_a_backtick_is_code_to_an_engine_that_does_not_quote_names_with_it(family):
    # None of these engines quotes names with backticks (Pinot may), so the
    # text between two backticks is code to them.
    _refused(family, "SELECT 1 AS a, ` ; DROP TABLE orders --`")


VERTICA_DOLLAR_HIDDEN = [
    "SELECT $$'$$ AS a; DROP TABLE orders --'",
    "SELECT $x$'$x$ AS a; DROP TABLE orders --'",
    "SELECT 1 AS a, $$ " + BS + "' $$ AS b; DROP TABLE orders --'",
    # A number before the tag: a Postgres-style lexer reads the number and
    # then the dollar-quoted string.
    "SELECT 1$x$'$x$ AS a; DROP TABLE orders --'",
    "SELECT .5$x$'$x$ AS a; DROP TABLE orders --'",
    "SELECT 1e5$x$'$x$ AS a; DROP TABLE orders --'",
]


@pytest.mark.parametrize("sql", VERTICA_DOLLAR_HIDDEN)
def test_a_dollar_quoted_string_is_refused_on_vertica(sql):
    # Vertica reads $$...$$ as a string, so a quote inside it opens nothing
    # and the second statement after it runs; standard quoting reads a
    # string from that quote to the end.
    _refused("VerticaClient", sql, match="dollar")


@pytest.mark.parametrize("sql", [
    "SELECT a$b, price$ FROM orders WHERE note = '$$ off'",
    "SELECT 1 AS a FROM orders -- $$",
    "SELECT x1$a$, _b$c$d$, a$b$1$c$ FROM orders",
])
def test_names_holding_a_dollar_and_dollars_in_strings_still_read_on_vertica(sql):
    assert ensure_read_only(_client("VerticaClient"), sql) == sql


@pytest.mark.parametrize("sql", [
    "SELECT 1 // '" + NL + "; DELETE FROM orders --'",
    "SELECT 1 AS a FROM orders // note",
])
def test_a_double_slash_is_refused_on_sql_anywhere(sql):
    # SQL Anywhere reads // as a line comment, like --.
    _refused("SybaseClient", sql, match="//")


def test_sql_anywhere_may_read_a_percent_sign_as_a_line_comment():
    # With percent_as_comment on, % starts a line comment, so the quote
    # after it opens nothing.
    _refused("SybaseClient", "SELECT 1 % '" + NL + "; DELETE FROM orders --'")


def test_sql_anywhere_may_read_a_backtick_quoted_name():
    # Read as a name, the backticks hold the quote, and the second
    # statement after them runs.
    _refused("SybaseClient", "SELECT `'`, 1; DELETE FROM orders --'")


def test_sql_anywhere_may_read_a_backslash_escape_in_a_string():
    _refused("SybaseClient", "SELECT 'a" + BS + "'' ; DELETE FROM orders --' AS a")


@pytest.mark.parametrize("sql", [
    "SELECT a % 2, a / 2 FROM orders WHERE note = 'it''s 50%'",
    "SELECT [my col] FROM orders",
    "SELECT `my col`, `id` FROM orders",
])
def test_sql_anywhere_reads_still_pass(sql):
    assert ensure_read_only(_client("SybaseClient"), sql) == sql


# --------------------------------------------------------------- Timbr

def test_timbr_is_checked_as_spark_sql_and_scoped_to_its_active_concepts():
    timbr = _client("TimbrClient")
    assert dialect_for_client(timbr) == "spark"
    _refused("TimbrClient", "DELETE FROM dtimbr.customer")
    setattr(timbr, TABLE_SCOPE_ATTR, TableScope.from_names(active=["dtimbr.customer"], inactive=["dtimbr.payroll"]))
    enforce_table_scope(timbr, ensure_read_only(timbr, "SELECT name FROM `dtimbr`.`customer` LIMIT 10"))
    with pytest.raises(TableScopeViolation, match="PAYROLL"):
        enforce_table_scope(timbr, ensure_read_only(timbr, "SELECT * FROM dtimbr.payroll"))


def test_a_backslash_is_refused_on_timbr_whose_lexer_is_not_known():
    # Spark reads the backslash as an escape and one string up to the last
    # quote; a lexer without backslash escapes ends the string at the
    # backslash and reads the secret table.
    sql = "SELECT 'a" + BS + "' , x FROM secret --' FROM orders"
    _hidden_from_sqlglot("TimbrClient", sql)
    _refused("TimbrClient", sql, match="backslash")


@pytest.mark.parametrize("sql", [
    "SELECT `a" + BS + "` FROM orders",
    "SELECT * FROM orders WHERE note LIKE 'a" + BS + BS + "%'",
])
def test_timbr_refuses_a_backslash_anywhere(sql):
    _refused("TimbrClient", sql, match="backslash")


@pytest.mark.parametrize("sql", DIGIT_GLUED)
def test_a_number_glued_to_a_word_is_refused_on_timbr(sql):
    _hidden_from_sqlglot("TimbrClient", sql)
    _refused("TimbrClient", sql, match="starts with a digit")


def test_the_spark_clients_whose_lexer_is_known_keep_reading_a_backslash_in_a_string():
    for family in ("SparkConnectClient", "DatabricksSqlClient"):
        _passes(family, "SELECT 'a" + BS + "'b' AS a FROM orders")


# --------------------------------------------------------------- guard profiles and dialects

def test_a_guard_profile_refuses_an_engine_it_does_not_know():
    from app.ai.code_execution.client_profiles import NONPARSING_ENGINES, GuardProfile

    with pytest.raises(ValueError, match="vertika"):
        GuardProfile(family="X", kind="nonparsing_sql", dialect=None, engine="vertika")
    with pytest.raises(ValueError):
        GuardProfile(family="X", kind="nonparsing_sql", dialect=None)
    with pytest.raises(ValueError):
        GuardProfile(family="X", kind="sql", dialect="mysql", engine="db2")
    assert {e.profile.engine for e in _CLIENT_TABLE.values()
            if e.profile is not None and e.profile.kind == "nonparsing_sql"} == NONPARSING_ENGINES


def test_every_engine_rule_names_an_engine_the_profiles_know():
    from app.ai.code_execution import sql_guard
    from app.ai.code_execution.client_profiles import NONPARSING_ENGINES

    rules = [getattr(sql_guard, name) for name in dir(sql_guard) if name.startswith("_NONPARSING_")
             and name != "_NONPARSING_READ_VERBS" and isinstance(getattr(sql_guard, name), frozenset)]
    assert rules and all(rule <= NONPARSING_ENGINES for rule in rules)
    assert set(sql_guard._NONPARSING_WRITE_WORDS) <= NONPARSING_ENGINES


@pytest.mark.parametrize("dialect", [None, "", "nosuchdialect"])
def test_a_sql_guard_profile_needs_a_dialect_sqlglot_knows(dialect):
    from app.ai.code_execution.client_profiles import GuardProfile

    with pytest.raises(ValueError, match="dialect"):
        GuardProfile(family="X", kind="sql", dialect=dialect)


class _UnhandledClient:
    """A client whose guard profile has a kind the guard has no check for."""


def test_a_client_whose_profile_the_guard_cannot_check_is_refused(monkeypatch):
    from app.ai.code_execution.client_profiles import ClientEntry, GuardProfile

    key = (_UnhandledClient.__module__, _UnhandledClient.__qualname__)
    profile = GuardProfile(family="Unhandled", kind="graphql", dialect=None)
    monkeypatch.setitem(_CLIENT_TABLE, key, ClientEntry(family="Unhandled", profile=profile))
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(_UnhandledClient(), "DELETE FROM orders")


@pytest.mark.parametrize("dialect", [None, "", "nosuchdialect"])
def test_reading_text_for_a_missing_or_unknown_dialect_is_refused(dialect):
    from app.ai.code_execution.sql_guard import check_engine_reading

    with pytest.raises(ReadOnlyViolation, match="dialect"):
        check_engine_reading("SELECT 1", dialect)


# --------------------------------------------------------------- top-level VALUES

@pytest.mark.parametrize("family", ["PostgresqlClient", "DuckDBClient", "SnowflakeClient"])
def test_a_top_level_values_list_reads(family):
    _passes(family, "VALUES (1, 'a'), (2, 'b')")


@pytest.mark.parametrize("family", ["PostgresqlClient", "DuckDBClient"])
def test_a_values_list_with_a_write_inside_is_refused(family):
    _refused(family, "VALUES ((SELECT 1 FROM orders)), (1) ; DELETE FROM orders")


# --------------------------------------------------------------- table scope folds Postgres names like Postgres

K_SIGN = chr(0x212A)
A_UMLAUT, a_umlaut = chr(0xC4), chr(0xE4)


@pytest.mark.parametrize("sql", [
    # Unquoted names fold to lower case: this reads public.orders.
    "SELECT * FROM public.Orders",
    "SELECT * FROM PUBLIC.ORDERS",
    # Postgres folds only ASCII letters: this reads the table named with the
    # Kelvin sign, not "kx".
    "SELECT * FROM public." + K_SIGN + "x",
    "SELECT * FROM public." + A_UMLAUT + "bc",
])
def test_table_scope_compares_postgres_names_as_postgres_folds_them(sql):
    scope = TableScope.from_names(
        active=["public.Orders", "public.kx", "public." + a_umlaut + "bc", a_umlaut + "bc"],
        inactive=["public.orders", "public." + K_SIGN + "x", "public." + A_UMLAUT + "bc"],
    )
    with pytest.raises(TableScopeViolation):
        check_table_scope(ensure_read_only(_client("PostgresqlClient"), sql), "postgres", scope)


@pytest.mark.parametrize("sql", [
    'SELECT * FROM public."Orders"',
    "SELECT * FROM public.KX",
    "SELECT * FROM public." + a_umlaut + "bc",
    'SELECT * FROM "public"."' + a_umlaut + 'bc"',
])
def test_table_scope_lets_postgres_names_through_that_fold_to_an_active_table(sql):
    scope = TableScope.from_names(
        active=["public.Orders", "public.kx", "public." + a_umlaut + "bc"],
        inactive=["public.orders", "public." + K_SIGN + "x", "public." + A_UMLAUT + "bc"],
    )
    check_table_scope(ensure_read_only(_client("PostgresqlClient"), sql), "postgres", scope)


CTE_SCOPE = TableScope.from_names(active=["public.orders"], inactive=["public." + a_umlaut + "bc", "public.x"])


def test_a_postgres_cte_covers_only_the_names_that_fold_to_its_own():
    # The CTE is named with an upper-case letter outside ASCII, which
    # Postgres keeps; the lower-case name is the inactive table.
    sql = "WITH " + A_UMLAUT + "bc AS (SELECT 1) SELECT * FROM " + a_umlaut + "bc"
    with pytest.raises(TableScopeViolation):
        check_table_scope(ensure_read_only(_client("PostgresqlClient"), sql), "postgres", CTE_SCOPE)


@pytest.mark.parametrize("sql", [
    "WITH " + A_UMLAUT + "bc AS (SELECT 1) SELECT * FROM " + A_UMLAUT + "bc",
    'WITH "x" AS (SELECT 1) SELECT * FROM X',
    'WITH X AS (SELECT 1) SELECT * FROM "x"',
])
def test_a_postgres_cte_still_covers_the_names_that_fold_to_its_own(sql):
    # The names unlisted (a CTE may not take a disabled table's name); read
    # as tables they would still be refused, as none is enabled.
    unlisted = TableScope.from_names(active=["public.orders"], inactive=[])
    check_table_scope(ensure_read_only(_client("PostgresqlClient"), sql), "postgres", unlisted)
    with pytest.raises(TableScopeViolation, match="may not take its name"):
        check_table_scope(ensure_read_only(_client("PostgresqlClient"), sql), "postgres", CTE_SCOPE)


def test_other_dialects_keep_comparing_names_case_insensitively():
    scope = TableScope.from_names(active=["sales.Orders"], inactive=[])
    check_table_scope('SELECT * FROM "SALES"."ORDERS"', "snowflake", scope)
    check_table_scope("SELECT * FROM SALES.orders", "tsql", scope)
