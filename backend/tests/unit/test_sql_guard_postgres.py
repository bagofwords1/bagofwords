"""Postgres queries are checked as the server reads them, and the checked text is sent.

Contract: for a Postgres connection the read-only guard reads the query with
the Postgres lexer, allows exactly one statement, checks the lexer's
canonical text and returns that text; table scope then checks the same text,
so both see the tables and statements the server would run. When sqlglot
cannot parse the text (or fails in any way), the guard falls back to an
allowlist: one statement, a read verb first, and no write word anywhere
outside literals; table scope refuses.

Shapes are described generically; each one was read one way by the old
checks and another way by Postgres.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.ai.code_execution import sql_parser
from app.ai.code_execution.code_execution import StreamingCodeExecutor, TableAccessError, UnsafeSQLError
from app.ai.code_execution.sql_guard import ReadOnlyViolation, check_sql, check_unparsed, ensure_read_only
from app.ai.code_execution.table_scope import TABLE_SCOPE_ATTR, TableScope, TableScopeViolation, check_table_scope
from app.data_sources.clients.postgresql_client import PostgresqlClient

BS = chr(92)
NL = chr(10)

SCOPE = TableScope.from_names(active=["public.orders"], inactive=["public.secret"])
# An operator sqlglot 27 cannot parse (Postgres reads it as prefix '@' on -1).
UNPARSABLE = "@ -1 = 1"


def _pg():
    return object.__new__(PostgresqlClient)


def _checked(sql: str) -> str:
    return ensure_read_only(_pg(), sql)


def _scoped(sql: str) -> None:
    """Both checks as the wrapper runs them: scope on what the guard returned."""
    check_table_scope(_checked(sql), "postgres", SCOPE)


# --------------------------------------------------------------- token boundaries

# An E-string whose escaped quote the plain reading does not see: the plain
# reading finds one long string, Postgres finds a short one and more SQL.
E_STRING_STATEMENTS = [
    "SELECT E'" + BS + "''; COMMIT; INSERT INTO orders VALUES (1); --'",
    "SELECT E'" + BS + "'' AS a; DELETE FROM orders; --'",
    "SELECT e'x" + BS + "'';NOTIFY c;--'",
]
E_STRING_READS = [
    "SELECT E'" + BS + "'' AS a, (SELECT s FROM secret) AS b --'",
    "SELECT E'" + BS + "'' AS a FROM orders, secret --'",
    "SELECT * FROM orders WHERE note = E'" + BS + "'' UNION SELECT * FROM secret --'",
]


@pytest.mark.parametrize("sql", E_STRING_STATEMENTS)
def test_an_e_string_hiding_a_second_statement_is_refused(sql):
    with pytest.raises(ReadOnlyViolation, match="single read-only statement"):
        _checked(sql)
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, "postgres")


@pytest.mark.parametrize("sql", E_STRING_READS)
def test_an_e_string_hiding_a_read_of_an_inactive_table_is_refused_by_table_scope(sql):
    checked = _checked(sql)  # a read: the guard lets it through, as Postgres reads it
    assert "secret" in checked and "E'" not in checked
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(checked, "postgres", SCOPE)


def test_table_scope_on_the_checked_text_sees_the_tables_postgres_reads():
    sql = "SELECT o.id, $$x$$ AS c FROM orders o /* note */ -- " + NL + "JOIN secret s ON true"
    checked = _checked(sql)
    assert checked == "SELECT o.id, 'x' AS c FROM orders o    " + NL + "JOIN secret s ON true"
    with pytest.raises(TableScopeViolation, match="SECRET"):
        check_table_scope(checked, "postgres", SCOPE)


@pytest.mark.parametrize("sql", [
    "SELECT 'a'" + NL + "'b' FROM orders",
    "SELECT U&'d0061' FROM orders",
    "SELECT 1 {# x #} FROM orders",
    "SELECT E'" + BS + "xff' FROM orders",
    "SELECT 'unterminated FROM orders",
])
def test_the_lexer_refusals_reach_the_guard(sql):
    with pytest.raises(ReadOnlyViolation):
        _checked(sql)


# --------------------------------------------------------------- the unparsed path

@pytest.mark.parametrize("sql", [
    # An operator sqlglot cannot parse, then a transaction command and more.
    f"SELECT 1 WHERE {UNPARSABLE}; COMMIT; SELECT set_config('a', 'b', false)",
    f"SELECT 1 WHERE {UNPARSABLE}; NOTIFY c; COMMIT",
    # Commands that do not parse and are not reads.
    "LOCK TABLE orders",
    "LOCK orders IN ACCESS EXCLUSIVE MODE",
    "START TRANSACTION",
    "BEGIN",
    "COMMIT",
    "SECURITY LABEL ON TABLE orders IS 'x'",
    "IMPORT FOREIGN SCHEMA s FROM SERVER x INTO y",
    "REASSIGN OWNED BY a TO b",
    "UNLISTEN c",
    "LISTEN c",
    "DISCARD ALL",
    "RESET ALL",
    "CHECKPOINT",
    "PREPARE p AS SELECT 1",
    "DO $$BEGIN NULL; END$$",
    # select-into after an operator that makes sqlglot give up.
    f"SELECT id INTO orders_copy FROM orders WHERE {UNPARSABLE}",
    f"SELECT id FROM orders WHERE {UNPARSABLE} INTO orders_copy",
])
def test_unparsed_or_non_read_commands_are_refused(sql):
    with pytest.raises(ReadOnlyViolation):
        _checked(sql)


@pytest.mark.parametrize("sql", [
    f"SELECT id FROM orders WHERE {UNPARSABLE}",
    "SELECT id FROM orders WHERE doc @? '$.a'",
    f"WITH x AS (SELECT id FROM orders WHERE {UNPARSABLE}) SELECT * FROM x",
    f"(SELECT id FROM orders WHERE {UNPARSABLE})",
    f"SELECT id FROM orders WHERE {UNPARSABLE} AND note = 'please delete; then commit'",
    f'SELECT "into", "set" FROM orders WHERE {UNPARSABLE}',
])
def test_an_unparsable_read_still_runs_unscoped_and_table_scope_refuses_it(sql):
    checked = _checked(sql)
    assert checked == sql
    with pytest.raises(TableScopeViolation):
        check_table_scope(checked, "postgres", SCOPE)


@pytest.mark.parametrize("sql", [
    "INSERT INTO orders (id) VALUES (9)",
    "SELECT 1; SELECT 2",
    "EXPLAIN ANALYZE DELETE FROM orders",
])
def test_controls_stay_refused(sql):
    with pytest.raises(ReadOnlyViolation):
        _checked(sql)


@pytest.mark.parametrize("sql, sent", [
    ("SELECT * FROM orders WHERE owner = 'DOMAIN" + BS + "user'",
     "SELECT * FROM orders WHERE owner = 'DOMAIN" + BS + "user'"),
    ("SELECT * FROM orders WHERE owner = E'DOMAIN" + BS + BS + "user'",
     "SELECT * FROM orders WHERE owner = 'DOMAIN" + BS + "user'"),
    ("SELECT * FROM orders WHERE owner = $$DOMAIN" + BS + "user$$",
     "SELECT * FROM orders WHERE owner = 'DOMAIN" + BS + "user'"),
    ("SELECT date_part('year', d) FROM orders", "SELECT date_part('year', d) FROM orders"),
    ("SELECT * FROM orders; -- sample", "SELECT * FROM orders;  "),
])
def test_identity_values_and_ordinary_literals_pass_and_are_sent_by_value(sql, sent):
    assert _checked(sql) == sent
    check_table_scope(sent, "postgres", SCOPE)


# --------------------------------------------------------------- sqlglot failing in any way

def test_any_parser_exception_falls_back_in_the_guard_and_fails_closed_in_table_scope(monkeypatch):
    def broken(sql, dialect):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(sql_parser, "parse_guard_sql", broken)
    assert _checked("SELECT id FROM orders") == "SELECT id FROM orders"
    with pytest.raises(ReadOnlyViolation):
        _checked("LOCK TABLE orders")
    check_sql("SELECT id FROM orders", "mysql")
    with pytest.raises(ReadOnlyViolation):
        check_sql("SELECT id INTO OUTFILE '/tmp/x' FROM orders", "mysql")
    with pytest.raises(TableScopeViolation):
        check_table_scope("SELECT id FROM orders", "postgres", SCOPE)


# --------------------------------------------------------------- check_unparsed

@pytest.mark.parametrize("code_only", [
    "SELECT 1", "with x as (select 1) select * from x", "VALUES (1)", "SHOW search_path",
    "EXPLAIN SELECT 1", "  ((SELECT 1))", "SELECT '' AS note;", 'SELECT "begin", "do" FROM t',
    "SELECT start_date, settings, into_x, x_into FROM t", "SELECT a$into FROM t",
])
def test_check_unparsed_allows_one_read(code_only):
    check_unparsed(code_only, read_verbs=frozenset({"SELECT", "WITH", "VALUES", "SHOW", "EXPLAIN"}))


@pytest.mark.parametrize("code_only", [
    "", ";", "SELECT 1; SELECT 2", "TABLE orders", "LOCK TABLE t", "CALL p()", "SELECT1",
    "SELECT 1 INTO t", "SELECT 1into t", "SELECT 1 FROM t FOR UPDATE; COMMIT", "SELECT 1 WHERE x = 1 -- c; RESET ALL",
    "SELECT * FROM t WHERE a = 1 AND START", "select refresh(1)", "WITH x AS (DELETE FROM t) SELECT 1",
])
def test_check_unparsed_refuses_anything_else(code_only):
    with pytest.raises(ReadOnlyViolation):
        check_unparsed(code_only, read_verbs=frozenset({"SELECT", "WITH", "VALUES", "SHOW", "EXPLAIN"}))


def test_check_unparsed_takes_extra_write_words():
    import re

    words = re.compile(r"\bQCMDEXC\b", re.IGNORECASE)
    check_unparsed("SELECT 1", read_verbs=frozenset({"SELECT"}), extra_write_words=words)
    with pytest.raises(ReadOnlyViolation, match="QCMDEXC"):
        check_unparsed("SELECT qcmdexc('x')", read_verbs=frozenset({"SELECT"}), extra_write_words=words)


# --------------------------------------------------------------- through the wrapper

class RecordingPostgresqlClient(PostgresqlClient):
    """The real Postgres client type, recording what reaches `execute_query`
    (the driver boundary)."""

    def execute_query(self, sql):
        self.sent.append(sql)
        return pd.DataFrame([{"a": 1}])


def _recording(scoped: bool):
    client = object.__new__(RecordingPostgresqlClient)
    client.sent = []
    if scoped:
        setattr(client, TABLE_SCOPE_ATTR, SCOPE)
    return client


def _run(client, sql: str):
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients['pg'].execute_query({sql!r})\n"
    )
    return StreamingCodeExecutor().execute_code(code=code, ds_clients={"pg": client}, excel_files=[])


@pytest.fixture(params=["inprocess", "subprocess"])
def mode(request, monkeypatch):
    import sys

    if request.param == "subprocess" and not sys.platform.startswith("linux"):
        pytest.skip("sandbox child needs Linux")
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    if request.param == "inprocess":
        # In-process mode is for local development only; CI runs with
        # ENVIRONMENT=production, so the test names the environment itself.
        monkeypatch.setenv("ENVIRONMENT", "development")
    return request.param


def test_the_client_receives_the_canonical_text_the_checks_read(mode):
    client = _recording(scoped=True)
    _run(client, "SELECT id FROM orders /* note */ WHERE owner = E'DOMAIN" + BS + BS + "user' -- end")
    assert client.sent == ["SELECT id FROM orders   WHERE owner = 'DOMAIN" + BS + "user'  "]


@pytest.mark.parametrize("sql, error", [
    (E_STRING_READS[0], TableAccessError),
    (E_STRING_STATEMENTS[2], UnsafeSQLError),  # no write word for the code scan to catch first
])
def test_a_hidden_read_or_statement_never_reaches_the_client(mode, sql, error):
    client = _recording(scoped=True)
    with pytest.raises(error):
        _run(client, sql)
    assert client.sent == []
