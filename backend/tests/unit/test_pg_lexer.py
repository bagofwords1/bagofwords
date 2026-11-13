"""The Postgres lexer reads query text the way the server does.

Contract: `lex_postgres` finds literal, quoted-name, comment and statement
boundaries exactly where Postgres (standard_conforming_strings on) finds
them, and gives back one canonical text: every comment replaced by a space
(its line breaks kept), every string literal written as a standard '...'
literal with the same value, everything else as written. That one text is
what the guard checks and what is sent. Forms that are rare and read in
more than one way (string continuation, U& strings, NUL, unterminated text,
byte escapes >= 0x80, malformed bit/hex strings, `{#`) are refused rather
than modelled.

The engine section runs the original and the canonical text on a real
Postgres 15 and compares what comes back.
"""

from __future__ import annotations

import re

import pytest

from app.ai.code_execution.pg_lexer import PgLexResult, lex_postgres
from app.ai.code_execution.sql_guard import ReadOnlyViolation

BS = chr(92)
NL = chr(10)
CR = chr(13)
TAB = chr(9)
NUL = chr(0)


def _text(sql: str) -> str:
    return lex_postgres(sql).text


# --------------------------------------------------------------- canonical text

CANONICAL = [
    # E-strings: every escape decoded, written back as a standard literal.
    ("SELECT E'it" + BS + "'s' AS v", "SELECT 'it''s' AS v"),
    ("SELECT e'a''b' AS v", "SELECT 'a''b' AS v"),
    ("SELECT E'a" + BS + BS + "b' AS v", "SELECT 'a" + BS + "b' AS v"),
    ("SELECT E'" + BS + "n" + BS + "t" + BS + "r" + BS + "b" + BS + "f' AS v",
     "SELECT '" + NL + TAB + CR + chr(8) + chr(12) + "' AS v"),
    ("SELECT E'" + BS + "101" + BS + "x41" + BS + "x4" + BS + "u0041" + BS + "U00000041' AS v",
     "SELECT 'AA" + chr(4) + "AA' AS v"),
    ("SELECT E'" + BS + "8" + BS + "q" + BS + "x' AS v", "SELECT '8qx' AS v"),
    ("SELECT E'" + BS + "u05e9' AS v", "SELECT '" + chr(0x5E9) + "' AS v"),
    # Dollar quotes: the body as it is, quotes doubled.
    ("SELECT $$it's " + BS + " x$$ AS v", "SELECT 'it''s " + BS + " x' AS v"),
    ("SELECT $tag$ $x$ $$ $ta $tag$ AS v", "SELECT ' $x$ $$ $ta ' AS v"),
    ("SELECT $a$x$b$a$ AS v", "SELECT 'x$b' AS v"),
    ("SELECT $$$$ AS v", "SELECT '' AS v"),
    # `$` inside a name is part of the name, not a quote.
    ("SELECT a$x$, 1 FROM t WHERE b = $x$y$x$", "SELECT a$x$, 1 FROM t WHERE b = 'y'"),
    ("SELECT $1, $2 FROM t", "SELECT $1, $2 FROM t"),
    # Comments: one space, line breaks kept; `--` ends at LF or CR.
    ("SELECT 1 -- note" + NL + "FROM t", "SELECT 1  " + NL + "FROM t"),
    ("SELECT 1 --c" + CR + "FROM secret", "SELECT 1  " + CR + "FROM secret"),
    ("SELECT /* a /* b */ c" + NL + " */ 1", "SELECT  " + NL + " 1"),
    ("SELECT 1/**/+/*x*/2", "SELECT 1 + 2"),
    ("SELECT 1 -/**/-1", "SELECT 1 - -1"),
    ("SELECT 1 +--x" + NL + "2", "SELECT 1 + " + NL + "2"),
    # Kept as written.
    ("SELECT N'x', n'y', B'0101', b'', X'1F', x'' FROM t", "SELECT N'x', n'y', B'0101', b'', X'1F', x'' FROM t"),
    ('SELECT "a""b", "e\'x\'", "--" FROM t', 'SELECT "a""b", "e\'x\'", "--" FROM t'),
    ("SELECT date'2020-01-01', xe'x', 'it''s'", "SELECT date'2020-01-01', xe'x', 'it''s'"),
    ("SELECT * FROM t WHERE owner = 'DOMAIN" + BS + "user'", "SELECT * FROM t WHERE owner = 'DOMAIN" + BS + "user'"),
    ("SELECT date_part('year', d) FROM t", "SELECT date_part('year', d) FROM t"),
    ("SELECT 'a' 'b'", "SELECT 'a' 'b'"),
    ("SELECT 1.5e3, .5, 1e-3 FROM t", "SELECT 1.5e3, .5, 1e-3 FROM t"),
]


@pytest.mark.parametrize("sql, text", CANONICAL)
def test_the_canonical_text_writes_every_literal_as_a_standard_one_and_drops_comments(sql, text):
    assert _text(sql) == text


def test_code_only_empties_every_literal_body_and_keeps_names():
    result = lex_postgres("SELECT 'a', E'b', $$c$$, N'd', B'1', X'f', \"q'x\" FROM t -- c")
    assert result == PgLexResult(
        text="SELECT 'a', 'b', 'c', N'd', B'1', X'f', \"q'x\" FROM t  ",
        code_only="SELECT '', '', '', N'', B'', X'', \"q'x\" FROM t  ",
        statements=1,
        has_backslash_literal=False,
    )


@pytest.mark.parametrize("sql, count", [
    ("SELECT 1", 1),
    ("SELECT 1;", 1),
    (";SELECT 1;;", 1),
    ("SELECT 1; SELECT 2", 2),
    ("SELECT ';'", 1),
    ("SELECT 1 -- ;", 1),
    ("SELECT 1 /* ; */", 1),
    ("SELECT $$;$$", 1),
    ("SELECT E'" + BS + "';'", 1),
    ('SELECT "a;b"', 1),
    ("SELECT 1; /* c */ ; -- d", 1),
    ("SELECT E'" + BS + "''; COMMIT; SELECT 1 --'", 3),
    (";;", 0),
    ("-- only a comment", 0),
])
def test_statements_are_counted_between_top_level_semicolons(sql, count):
    assert lex_postgres(sql).statements == count


@pytest.mark.parametrize("sql, expected", [
    ("SELECT 'a" + BS + "b'", True),
    ("SELECT E'" + BS + BS + "'", True),
    ("SELECT $$" + BS + "$$", True),
    ("SELECT N'" + BS + "'", True),
    ("SELECT E'" + BS + "n'", False),
    ("SELECT 'a', \"" + BS + "\"", False),
    ("SELECT 1 -- " + BS, False),
])
def test_a_backslash_is_reported_only_when_a_literal_value_holds_one(sql, expected):
    assert lex_postgres(sql).has_backslash_literal is expected


# --------------------------------------------------------------- properties

SAMPLES = [sql for sql, _ in CANONICAL] + [
    "SELECT 1 -- a" + CR + NL + "/* b" + CR + "c */ FROM t",
    "WITH x AS (SELECT E'" + BS + "'' AS a) SELECT * FROM x; -- end",
    "SELECT $q$" + NL + "line" + NL + "$q$, '" + NL + "' FROM t",
]


@pytest.mark.parametrize("sql", SAMPLES)
def test_the_canonical_text_is_a_fixed_point_with_no_comment_e_string_or_dollar_quote(sql):
    first = lex_postgres(sql)
    again = lex_postgres(first.text)
    assert again.text == first.text
    assert again.code_only == first.code_only
    assert again.statements == first.statements
    assert again.has_backslash_literal == first.has_backslash_literal
    names_removed = first.code_only
    while '"' in names_removed:  # quoted names may hold anything
        start = names_removed.index('"')
        end = names_removed.index('"', start + 1)
        names_removed = names_removed[:start] + names_removed[end + 1:]
    assert "--" not in names_removed and "/*" not in names_removed
    assert "$$" not in names_removed and "$q$" not in names_removed and "$tag$" not in names_removed
    assert not re.search(r"(?<![A-Za-z0-9_$])[eE]''", names_removed)  # no E-string prefix left
    # Line breaks are kept (an E-string's \n or \r escape becomes a real one
    # in the literal, so those samples are skipped here).
    if BS + "n" not in sql and BS + "r" not in sql:
        for mark in (NL, CR):
            assert first.text.count(mark) == sql.count(mark)


# --------------------------------------------------------------- refusals

REFUSED = {
    "continuation": [
        "SELECT 'a'" + NL + "'b'",
        "SELECT E'a'" + NL + "'" + BS + "''",
        "SELECT 'a' -- c" + NL + "'b'",
        "SELECT 'a'" + CR + "'b'",
        "SELECT 'a'  " + NL + TAB + " 'b'",
        "SELECT 'a'" + NL + "-- c" + NL + "'b'",
        "SELECT $$a$$" + NL + "'b'",
        "SELECT 'a'" + NL + "E'b'",
        "SELECT 'a'" + NL + "$$b$$",
        "SELECT B'1'" + NL + "'0'",
        "SELECT N'a'" + NL + "'b'",
        "SELECT 'a'/*" + NL + "*/'b'",
        "SELECT 'a'" + chr(11) + NL + "'b'",
    ],
    "unicode escape string": [
        "SELECT U&'x'", "SELECT u&'x'", 'SELECT U&"x"', "SELECT 1 FROM t WHERE a = u&'d0061'",
    ],
    "nul": [
        "SELECT 1" + NUL, "SELECT '" + NUL + "'", "SELECT 1 -- " + NUL, 'SELECT "' + NUL + '"',
        "SELECT E'" + BS + "0'", "SELECT E'" + BS + "000'", "SELECT E'" + BS + "x0'", "SELECT E'" + BS + "u0000'",
        "SELECT E'" + BS + "400'",
    ],
    "unterminated": [
        "SELECT 'a", "SELECT E'a" + BS + "'", "SELECT E'a" + BS, "SELECT $$a", "SELECT $x$a$y$", 'SELECT "a',
        "SELECT /* a", "SELECT /* /* */ 1", "SELECT B'01", "SELECT N'x",
    ],
    "byte escape": [
        "SELECT E'" + BS + "200'", "SELECT E'" + BS + "377'", "SELECT E'" + BS + "777'",
        "SELECT E'" + BS + "x80'", "SELECT E'" + BS + "xff'", "SELECT E'" + BS + "xC3" + BS + "xA9'",
    ],
    "unicode escape value": [
        "SELECT E'" + BS + "u12'", "SELECT E'" + BS + "u12g4'", "SELECT E'" + BS + "U0001F60'",
        "SELECT E'" + BS + "uD800'", "SELECT E'" + BS + "uD83D" + BS + "uDE00'", "SELECT E'" + BS + "U00110000'",
    ],
    "template comment": ["SELECT 1 {# x #} FROM t", "{# x #} SELECT 1", "SELECT 1 {#"],
    "bit or hex body": ["SELECT B'012'", "SELECT b'1 0'", "SELECT X'GG'", "SELECT x'0x1'"],
    "glued to a number": [
        "SELECT 1e'x'", "SELECT 1E'x'", "SELECT 2x'ff'", "SELECT 1.5n'a'", "SELECT 3b'1'", "SELECT 1u&'x'",
        "SELECT $1e'x'",
    ],
}


@pytest.mark.parametrize("sql", [sql for forms in REFUSED.values() for sql in forms])
def test_rare_or_ambiguous_forms_are_refused(sql):
    with pytest.raises(ReadOnlyViolation):
        lex_postgres(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 'a';" + NL + "SELECT 'b'",  # a ';' between them: two statements, not one string
    "SELECT 'a', " + NL + "'b'",
    "SELECT 'a' AS x" + NL + ", 'b'",
    "SELECT \"a\"" + NL + "'b'",
    "SELECT B'', X'', N''",
    "SELECT xe'x', ab'c', u'x', u &'x'",
    "SELECT 1 e'x'",
    "SELECT 1/**/e'x'",
    "SELECT { 'a' }",
])
def test_near_misses_are_not_refused(sql):
    lex_postgres(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 'a" + BS + "b'",
    "SELECT $$a" + BS + "b$$",
    "SELECT E'x'",
    "SELECT e'" + BS + "n'",
])
def test_redshift_mode_refuses_backslashes_in_literals_and_e_strings(sql):
    lex_postgres(sql)  # fine on Postgres
    with pytest.raises(ReadOnlyViolation):
        lex_postgres(sql, mode="redshift")


def test_redshift_mode_keeps_plain_literals():
    assert lex_postgres("SELECT 'it''s', $$a$$ -- c", mode="redshift").text == "SELECT 'it''s', 'a'  "


def test_an_unknown_mode_is_an_error():
    with pytest.raises(ValueError):
        lex_postgres("SELECT 1", mode="mysql")


def test_the_lexer_runs_in_linear_time():
    import time

    def took(n):
        sql = ("SELECT E'" + BS + "'' AS a, $$x$$ /* /* c */ */ -- d" + NL) * n
        start = time.perf_counter()
        lex_postgres(sql)
        return time.perf_counter() - start

    took(200)
    small, large = min(took(2_000) for _ in range(3)), min(took(20_000) for _ in range(3))
    assert large < small * 30  # 10x the text, far below quadratic growth


# --------------------------------------------------------------- engine (Postgres 15)

def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def pg():
    """Run one statement on a throwaway Postgres 15; return (columns, rows)."""
    if not _docker_available():
        pytest.skip("Docker daemon not reachable")
    import psycopg2
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15") as server:
        con = psycopg2.connect(host=server.get_container_host_ip(), port=server.get_exposed_port(5432),
                               user=server.username, password=server.password, dbname=server.dbname)
        con.autocommit = True
        cur = con.cursor()
        cur.execute("CREATE TABLE t (a int, b text)")
        cur.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
        cur.execute("SHOW standard_conforming_strings")
        assert cur.fetchone()[0] == "on"

        def run(sql):
            cur.execute(sql)  # no parameters: psycopg2 sends the text as it is
            return [d.name for d in cur.description], cur.fetchall()

        yield run
        con.close()


ENGINE_SAMPLES = [
    "SELECT E'it" + BS + "'s' AS v",
    "SELECT E'a" + BS + BS + "b" + BS + "n" + BS + "t" + BS + "r" + BS + "b" + BS + "f" + BS + "8" + BS + "q' AS v",
    "SELECT E'" + BS + "101" + BS + "x41" + BS + "x4" + BS + "u0041" + BS + "U00000041" + BS + "u05e9" + BS + "7' AS v",
    "SELECT e'a''b' AS v, E'" + BS + "'' AS w",
    "SELECT $$it's " + BS + " x$$ AS v, $tag$ $x$ $$ $ta $tag$ AS w, $a$x$b$a$ AS z",
    "SELECT 1 AS a$x$, $x$y$x$ AS v",
    "SELECT 1 AS v -- note" + NL + ", 2 AS w",
    "SELECT 1 AS v --c" + CR + ", 2 AS w",
    "SELECT 1 AS v --c" + CR + NL + ", 2 AS w",
    "SELECT /* a /* b */ c" + NL + " */ 1 AS v",
    "SELECT 1/**/+/*x*/2 AS v, 1 -/**/-1 AS w, 1 +--x" + NL + "2 AS z",
    "SELECT N'x' AS v, B'0101' AS w, X'1F' AS z",
    'SELECT 1 AS "a""b", 2 AS "e\'x\'", 3 AS "--"',
    "SELECT date'2020-01-01' AS v, 'it''s' AS w, 'DOMAIN" + BS + "user' AS z",
    "SELECT a, b FROM t WHERE b = E'" + BS + "x79' -- trailing",
    "SELECT $q$" + NL + "line" + CR + NL + "$q$ AS v, '" + NL + "' AS w",
    "SELECT 'a' AS v;",
]


@pytest.mark.parametrize("sql", ENGINE_SAMPLES)
def test_postgres_returns_the_same_columns_and_values_for_the_canonical_text(pg, sql):
    result = lex_postgres(sql)
    assert result.statements == 1
    assert pg(result.text) == pg(sql)


def test_postgres_ends_a_line_comment_at_a_carriage_return_as_the_lexer_does(pg):
    sql = "SELECT 1 AS v --c" + CR + ", 2 AS w"
    assert pg(sql)[0] == ["v", "w"]
    assert ", 2 AS w" in lex_postgres(sql).code_only


def test_postgres_keeps_the_escape_mode_across_a_continued_string_so_the_lexer_refuses_it(pg):
    # Continued after a line break, the second piece is read with the first
    # piece's escapes: a plain-looking '\'' is one quote, not a quote and a
    # backslash. This is why continuation is refused, not modelled.
    sql = "SELECT E'a'" + NL + "'" + BS + "'' AS v"
    assert pg(sql) == (["v"], [("a'",)])
    with pytest.raises(ReadOnlyViolation):
        lex_postgres(sql)


def test_postgres_reads_an_e_string_quote_where_a_plain_reading_does_not(pg):
    # The plain reading ends nowhere before the trailing quote; Postgres ends
    # the string after the escaped quote, and the rest is code.
    sql = "SELECT E'" + BS + "'' AS a, 2 AS b --'"
    assert pg(sql) == (["a", "b"], [("'", 2)])
    assert lex_postgres(sql).code_only == "SELECT '' AS a, 2 AS b  "
