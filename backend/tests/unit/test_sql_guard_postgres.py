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
from app.ai.code_execution.sql_guard import (
    DENIED_FUNCTIONS,
    ReadOnlyViolation,
    check_sql,
    check_unparsed,
    ensure_read_only,
)
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


# A name holding a character >= 0x80 that Python calls a space: one name to
# Postgres, a name then a dollar string to sqlglot, which then sees no table
# and no INTO.
NAME_SPACE_SHAPES = [
    "SELECT 1 as{s}$x$, code FROM secret as{s}$x$",
    "SELECT 1 as{s}$x$ INTO orders_copy FROM orders as{s}$x$",
    "SELECT 1 as{s}$x$, (SELECT code FROM secret LIMIT 1) as{s}$x$ FROM orders",
]


@pytest.mark.parametrize("space", [chr(0x85), chr(0xA0), chr(0x2028), chr(0x3000)], ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("shape", NAME_SPACE_SHAPES)
def test_a_name_split_by_a_non_ascii_space_is_refused_by_the_guard_and_never_scoped(shape, space):
    sql = shape.format(s=space)
    with pytest.raises(ReadOnlyViolation):
        _checked(sql)
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, "postgres")
    with pytest.raises(ReadOnlyViolation):
        _scoped(sql)


# A name that starts with a character Python calls a digit (beyond ASCII):
# one name to Postgres, a number then a dollar string to sqlglot.
NAME_DIGIT_SHAPES = [
    "SELECT 1 BETWEEN {d}$x$.id AND 2 AS v, code FROM secret AS {d}$x$",
    "SELECT 1 BETWEEN {d}$x$.id AND 2 AS v INTO orders_copy FROM orders AS {d}$x$",
    "SELECT id FROM orders WHERE id BETWEEN {d}$x$ AND (SELECT max(code) FROM secret) AND {d}$x$",
]


@pytest.mark.parametrize("digit", [chr(0xB2), chr(0x661), chr(0x966), chr(0xFF11)], ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("shape", NAME_DIGIT_SHAPES)
def test_a_name_that_starts_with_a_non_ascii_digit_is_refused_by_the_guard_and_never_scoped(shape, digit):
    sql = shape.format(d=digit)
    with pytest.raises(ReadOnlyViolation):
        _checked(sql)
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, "postgres")
    with pytest.raises(ReadOnlyViolation):
        _scoped(sql)


def test_a_non_ascii_digit_inside_a_name_still_passes():
    sql = "SELECT id" + chr(0xB2) + " FROM orders"
    assert _checked(sql) == sql


def test_a_non_ascii_space_in_a_literal_or_quoted_name_still_passes():
    nbsp = chr(0xA0)
    sql = "SELECT id AS \"order" + nbsp + "id\" FROM orders WHERE note = 'a" + nbsp + "b'"
    assert _checked(sql) == sql
    check_table_scope(sql, "postgres", SCOPE)


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
    # A write word inside a name that holds digits is part of that name.
    "SELECT t1do, col2set, x9into, a1b2lock FROM t",
    "SELECT 0x1F, 0XFF, 0b101, 0o17, 1_000, 0x, 0b2 FROM t",
])
def test_check_unparsed_allows_one_read(code_only):
    check_unparsed(code_only, read_verbs=frozenset({"SELECT", "WITH", "VALUES", "SHOW", "EXPLAIN"}))


@pytest.mark.parametrize("code_only", [
    "", ";", "SELECT 1; SELECT 2", "TABLE orders", "LOCK TABLE t", "CALL p()", "SELECT1",
    "SELECT 1 INTO t", "SELECT 1into t", "SELECT 1 FROM t FOR UPDATE; COMMIT", "SELECT 1 WHERE x = 1 -- c; RESET ALL",
    "SELECT * FROM t WHERE a = 1 AND START", "select refresh(1)", "WITH x AS (DELETE FROM t) SELECT 1",
    # A number then a write word: two tokens to older servers.
    "SELECT 1do", "SELECT 2.5set", "SELECT 1e5into t", "SELECT $1do",
    # Hex, octal and binary numbers and digit separators end where the
    # engine ends them; a word right after one is a word.
    "SELECT 0x1into t", "SELECT 0XaFset", "SELECT 0b1do", "SELECT 0o7into t", "SELECT 1_000into t",
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


# --------------------------------------------------------------- the TABLE shorthand

# The TABLE shorthand reads a whole table; it is refused wherever it stands.
TABLE_SHORTHAND = [
    "TABLE secret",
    "table secret",
    "WITH x AS (TABLE secret) SELECT * FROM x",
    "SELECT 1 AS v WHERE 'k1' = ANY (TABLE secret)",
    "SELECT (TABLE secret LIMIT 1) AS v",
    "SELECT 'x' AS code UNION ALL TABLE secret",
    "EXPLAIN TABLE secret",
    f"SELECT 1 WHERE {UNPARSABLE} UNION ALL TABLE secret",
]


@pytest.mark.parametrize("sql", TABLE_SHORTHAND)
def test_the_table_shorthand_is_refused(sql):
    with pytest.raises(ReadOnlyViolation, match="TABLE"):
        _checked(sql)
    with pytest.raises(ReadOnlyViolation, match="TABLE"):
        check_sql(sql, "postgres")


@pytest.mark.parametrize("sql", [
    'SELECT "table" FROM orders',
    "SELECT table_name FROM information_schema.tables",
    "SELECT 'TABLE secret' AS note, $$table$$ AS kind FROM orders -- TABLE secret",
])
def test_the_word_table_as_a_quoted_name_part_of_a_name_or_in_a_literal_still_passes(sql):
    _checked(sql)


# --------------------------------------------------------------- function deny list

# Functions that run SQL held in a string, reach another connection or the
# server's files, or change the session or the server outside the read
# (replication slots and origins, logical decoding, statistics, WAL, backup,
# recovery, logs, index maintenance, server internals, large objects).
DENIED = [
    "ts_stat", "ts_rewrite",
    "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
    "table_to_xml", "table_to_xml_and_xmlschema", "table_to_xmlschema",
    "cursor_to_xml", "cursor_to_xmlschema",
    "dblink", "dblink_exec", "dblink_connect", "dblink_send_query",
    "pg_advisory_lock", "pg_advisory_lock_shared", "pg_try_advisory_lock", "pg_try_advisory_lock_shared",
    "pg_advisory_unlock", "pg_advisory_unlock_shared", "pg_advisory_unlock_all",
    "set_config", "setval", "nextval", "pg_notify",
    "pg_cancel_backend", "pg_terminate_backend", "pg_reload_conf",
    "pg_create_physical_replication_slot", "pg_create_logical_replication_slot",
    "pg_copy_physical_replication_slot", "pg_copy_logical_replication_slot",
    "pg_drop_replication_slot", "pg_replication_slot_advance",
    "pg_logical_emit_message", "pg_logical_slot_get_changes", "pg_logical_slot_peek_binary_changes",
    "pg_replication_origin_create", "pg_replication_origin_session_setup", "pg_replication_origin_advance",
    "pg_stat_reset", "pg_stat_reset_shared", "pg_stat_reset_single_table_counters", "pg_stat_statements_reset",
    "pg_switch_wal", "pg_switch_xlog", "pg_create_restore_point",
    "pg_backup_start", "pg_backup_stop", "pg_start_backup", "pg_stop_backup",
    "pg_promote", "pg_wal_replay_pause", "pg_xlog_replay_resume",
    "pg_rotate_logfile", "pg_rotate_logfile_old", "pg_log_backend_memory_contexts", "pg_import_system_collations",
    "brin_summarize_range", "brin_summarize_new_values", "brin_desummarize_range", "gin_clean_pending_list",
    "binary_upgrade_set_next_pg_type_oid", "pg_nextoid", "pg_stop_making_pinned_objects",
    "pg_extension_config_dump",
    "pg_file_write", "pg_file_rename", "pg_file_unlink", "pg_file_sync", "pg_logdir_ls",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_waldir", "pg_stat_file",
    "lo_import", "lo_export", "lo_unlink", "lo_put", "lo_get", "lo_from_bytea", "lo_create", "lo_open",
    "loread", "lowrite",
]
# A denied name is refused wherever it stands in the code, also as a field,
# a column or an alias.
SPELLINGS = {
    "plain": "SELECT {n}('a') FROM orders",
    "upper case": "SELECT {N}('a') FROM orders",
    "schema-qualified": "SELECT pg_catalog.{n}('a') FROM orders",
    "quoted": 'SELECT "{n}"(\'a\') FROM orders',
    "quoted and qualified": 'SELECT "pg_catalog" . "{n}" (\'a\') FROM orders',
    "as a row source": "SELECT * FROM {n}('a') AS f",
    "with a comment before the call": "SELECT {n} /* c */ ('a') FROM orders",
    "in text sqlglot cannot parse": "SELECT {n}('a') FROM orders WHERE " + UNPARSABLE,
    "as a field of its argument": "SELECT ('a'::text).{n} FROM orders",
    "as a quoted field of its argument": "SELECT ('a'::text).\"{n}\" FROM orders",
    "as a field, with a comment before it": "SELECT (1) /* c */ . {n} FROM orders",
    "as a field of a table alias": "SELECT o.{n} FROM orders o",
    "as a field in text sqlglot cannot parse": "SELECT (1).{n} FROM orders WHERE " + UNPARSABLE,
    "as a bare name": "SELECT {n} FROM orders",
    "as a quoted alias": 'SELECT 1 AS "{n}" FROM orders',
}
REDSHIFT_SPELLINGS = ["plain", "upper case", "schema-qualified", "quoted", "as a row source",
                      "as a field of its argument", "as a quoted field of its argument", "as a field of a table alias",
                      "as a bare name"]


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("name", DENIED)
def test_each_denied_function_is_refused_in_every_spelling(name, spelling):
    sql = SPELLINGS[spelling].format(n=name, N=name.upper())
    with pytest.raises(ReadOnlyViolation, match="(?i)" + name):
        _checked(sql)


@pytest.mark.parametrize("spelling", REDSHIFT_SPELLINGS)
@pytest.mark.parametrize("name", DENIED)
def test_each_denied_function_is_refused_on_the_parsed_tree_for_redshift(name, spelling):
    sql = SPELLINGS[spelling].format(n=name, N=name.upper())
    with pytest.raises(ReadOnlyViolation, match="(?i)" + name):
        check_sql(sql, "redshift")


@pytest.mark.parametrize("sql", [
    'SELECT "pg_terminate_backend"(1) FROM orders WHERE ' + UNPARSABLE,
    'SELECT "pg_catalog"."NEXTVAL"(1) FROM orders WHERE ' + UNPARSABLE,
    'SELECT (1)."pg_advisory_lock" FROM orders WHERE ' + UNPARSABLE,
])
def test_a_quoted_denied_name_in_redshift_text_sqlglot_cannot_parse_is_refused(sql):
    with pytest.raises(ReadOnlyViolation, match="(?i)pg_terminate_backend|nextval|pg_advisory_lock"):
        check_sql(sql, "redshift")


def test_a_quoted_write_word_in_redshift_text_sqlglot_cannot_parse_still_passes():
    check_sql('SELECT "delete", "a""set" FROM orders WHERE ' + UNPARSABLE, "redshift")


def test_a_function_that_runs_sql_in_a_string_is_refused_on_an_unscoped_client():
    sql = "SELECT word FROM ts_stat('SELECT to_tsvector(code) FROM secret')"
    with pytest.raises(ReadOnlyViolation, match="TS_STAT"):
        _checked(sql)


@pytest.mark.parametrize("sql", [
    "SELECT pg_advisory_xact_lock(1), pg_advisory_xact_lock_shared(2), pg_try_advisory_xact_lock(3) FROM orders",
    "SELECT current_setting('search_path'), currval('s') FROM orders",
    "SELECT my_set_config(1), set_configx(1), nextval_of(1), o.lo_price, lo_bound FROM orders o",
    "SELECT 'nextval(1)' AS a, $$set_config(1)$$ AS b FROM orders -- setval(1)",
    "SELECT pg_sleep(0), setseed(0.5), random(), clock_timestamp() FROM orders",
    "SELECT * FROM pg_catalog.pg_file_settings",
    "SELECT * FROM pg_catalog.pg_replication_origin_status",
    "SELECT slot_name FROM pg_replication_slots",
])
def test_transaction_scoped_locks_reads_and_lookalike_names_still_pass(sql):
    _checked(sql)


# Reads an agent writes every day, each with a function or clause near a
# denied one.
LEGITIMATE_READS = [
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    "SELECT pg_size_pretty(pg_total_relation_size('orders')) AS size",
    "SELECT obj_description('orders'::regclass, 'pg_class') AS note",
    "SELECT pg_advisory_xact_lock(7), pg_try_advisory_xact_lock(8)",
    "SELECT format('%s-%s', id, id) AS v FROM orders",
    "SELECT xpath('/a/text()', '<a>x</a>'::xml) AS v",
    "SELECT row_to_json(o) AS v FROM orders o",
    "SELECT date_part('year', now()) AS v",
    "SELECT id FROM orders TABLESAMPLE SYSTEM (100)",
    "SELECT v FROM XMLTABLE('/r/i' PASSING ('<r><i>1</i></r>'::xml) COLUMNS v int PATH '.') AS x",
]


@pytest.mark.parametrize("sql", LEGITIMATE_READS + ["SELECT currval('s1') AS v", "SELECT lastval() AS v"])
def test_legitimate_reads_still_pass(sql):
    assert _checked(sql) == sql


# --------------------------------------------------------------- EXPLAIN

@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYZE SELECT 1 AS a INTO made",
    "explain analyse select id into made from orders",
    "EXPLAIN ANALYZE VERBOSE WITH x AS (SELECT 1 AS a) SELECT * INTO made FROM x",
    "EXPLAIN (ANALYZE, BUFFERS) SELECT set_config('application_name', 'x', false)",
    "EXPLAIN ANALYZE SELECT nextval('s')",
    "EXPLAIN(ANALYZE)SELECT pg_advisory_lock(1)",
    "EXPLAIN ANALYZE EXECUTE p",
    "EXPLAIN ANALYZE DECLARE c CURSOR FOR SELECT 1",
    "EXPLAIN (ANALYZE) CREATE TABLE made AS SELECT 1",
    "EXPLAIN ANALYZE MERGE INTO orders o USING orders s ON false WHEN NOT MATCHED THEN DO NOTHING",
    "EXPLAIN ANALYZE SELECT 1 AS a INTO made WHERE " + UNPARSABLE,
])
def test_explain_checks_the_statement_it_explains(sql):
    with pytest.raises(ReadOnlyViolation):
        _checked(sql)


@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT 1",
    "EXPLAIN SELECT id FROM orders",
    "EXPLAIN ANALYZE SELECT id FROM orders",
    "EXPLAIN ANALYZE VERBOSE SELECT 1",
    "EXPLAIN VERBOSE SELECT 1",
    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT id FROM orders",
    "EXPLAIN (FORMAT 'yaml') SELECT 1",
    "EXPLAIN (SELECT 1)",
    "explain (costs off) values (1)",
    "EXPLAIN WITH x AS (SELECT id FROM orders) SELECT * FROM x",
    'EXPLAIN SELECT "start", "set" FROM orders',
])
def test_explain_of_a_read_still_passes(sql):
    _checked(sql)


def test_an_explain_refused_for_a_name_that_is_also_an_sql_word_says_to_quote_it():
    with pytest.raises(ReadOnlyViolation, match="START.*double quotes"):
        _checked("EXPLAIN SELECT start FROM orders")


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
    (NAME_SPACE_SHAPES[0].format(s=chr(0xA0)), UnsafeSQLError),
    (NAME_DIGIT_SHAPES[0].format(d=chr(0xB2)), UnsafeSQLError),
])
def test_a_hidden_read_or_statement_never_reaches_the_client(mode, sql, error):
    client = _recording(scoped=True)
    with pytest.raises(error):
        _run(client, sql)
    assert client.sent == []


# --------------------------------------------------------------- engine (Postgres 15)

def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


READER = "bow_reader"
_CONNECT_KEYS = ("host", "port", "user", "password", "dbname")


@pytest.fixture(scope="module")
def server():
    """A throwaway Postgres 15 (logical WAL, so slots can decode) holding a
    table the agent must not read, sequences, replication slots, a large
    object, adminpack and a role that may only SELECT; yields its connection
    settings."""
    if not _docker_available():
        pytest.skip("Docker daemon not reachable")
    import psycopg2
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:15").with_command("postgres -c wal_level=logical") as container:
        settings = {"host": container.get_container_host_ip(), "port": int(container.get_exposed_port(5432)),
                    "user": container.username, "password": container.password, "dbname": container.dbname}
        con = psycopg2.connect(**settings)
        con.autocommit = True
        with con.cursor() as cur:
            for sql in ["CREATE TABLE secret (code text)", "INSERT INTO secret VALUES ('k1')",
                        "CREATE TABLE orders (id int)", "INSERT INTO orders VALUES (1)",
                        "CREATE TABLE quiet (id int)", "CREATE TABLE changes (id int)",
                        "CREATE EXTENSION dblink", "CREATE EXTENSION adminpack",
                        "SELECT lo_from_bytea(4242, 'x')",
                        "SELECT pg_create_physical_replication_slot('bow_slot')",
                        "SELECT pg_create_physical_replication_slot('bow_slot2')",
                        "SELECT pg_create_physical_replication_slot('bow_kept')",
                        "SELECT pg_create_logical_replication_slot('bow_cdc', 'test_decoding')",
                        "SELECT pg_create_logical_replication_slot('bow_messages', 'test_decoding')",
                        "INSERT INTO changes VALUES (1)",
                        f"CREATE ROLE {READER} LOGIN PASSWORD '{READER}'",
                        f"GRANT SELECT ON orders TO {READER}",
                        ] + [f"CREATE SEQUENCE s{i}" for i in range(1, 10)]:
                cur.execute(sql)
        con.close()
        # dblink connects from inside the server, to itself.
        settings["dsn"] = f"host=localhost dbname={container.dbname} user={container.username} password={container.password}"
        yield settings


@pytest.fixture
def pg(server):
    """Run one statement on its own connection, as it is (no parameters, so
    psycopg2 sends the text unchanged), then `then` on the same connection;
    return the rows of each."""
    import psycopg2

    def run(sql, then=None):
        con = psycopg2.connect(**{k: server[k] for k in _CONNECT_KEYS})
        con.autocommit = True
        try:
            with con.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() if cur.description else None
                if then is None:
                    return rows, None
                cur.execute(then)
                return rows, cur.fetchall()
        finally:
            con.close()

    return run


LOCKS_HELD = "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
# (statement, a statement run after it on the same connection, what that
# shows): each statement reads the table or has an effect outside the read.
# None: the statement ran, and its effect is not visible to a query.
ENGINE_SHAPES = [
    ("TABLE secret", None, None),
    ("WITH x AS (TABLE secret) SELECT * FROM x", None, None),
    ("SELECT 1 AS v WHERE 'k1' = ANY (TABLE secret)", None, None),
    ("SELECT (TABLE secret LIMIT 1) AS v", None, None),
    ("SELECT 'x' AS code UNION ALL TABLE secret", None, None),
    ("SELECT word FROM ts_stat('SELECT to_tsvector(''simple'', code) FROM secret')", None, None),
    ("SELECT ts_rewrite('a'::tsquery, 'SELECT ''a''::tsquery, to_tsquery(''simple'', code) FROM secret') AS v",
     None, None),
    ("SELECT query_to_xml('SELECT code FROM secret', true, false, '') AS v", None, None),
    ("SELECT table_to_xml('secret', true, false, '') AS v", None, None),
    ("SELECT * FROM dblink('{dsn}', 'SELECT code FROM secret') AS r(code text)", None, None),
    ("SELECT dblink_exec('{dsn}', 'CREATE TABLE made_by_link (a int)')",
     "SELECT to_regclass('made_by_link')::text", [("made_by_link",)]),
    ("SELECT pg_advisory_lock(41)", LOCKS_HELD, [(1,)]),
    ("SELECT pg_try_advisory_lock(42)", LOCKS_HELD, [(1,)]),
    ("SELECT pg_advisory_lock_shared(43)", LOCKS_HELD, [(1,)]),
    ("SELECT pg_advisory_lock(44), pg_advisory_unlock(44)", LOCKS_HELD, [(0,)]),
    ("SELECT set_config('search_path', 'elsewhere', false)", "SHOW search_path", [("elsewhere",)]),
    ('SELECT "pg_catalog"."set_config"(\'search_path\', \'elsewhere\', false)', "SHOW search_path",
     [("elsewhere",)]),
    ("SELECT setval('s1', 100)", "SELECT last_value FROM s1", [(100,)]),
    ("SELECT nextval('s2')", "SELECT is_called FROM s2", [(True,)]),
    ("SELECT pg_catalog.nextval('s3')", "SELECT is_called FROM s3", [(True,)]),
    ("SELECT pg_notify('c', 'x')", None, None),
    ("SELECT pg_cancel_backend(0)", None, None),
    ("SELECT pg_terminate_backend(0)", None, None),
    ("SELECT pg_reload_conf()", None, None),
    ("EXPLAIN ANALYZE SELECT 1 AS a INTO made_by_explain",
     "SELECT to_regclass('made_by_explain')::text", [("made_by_explain",)]),
    ("EXPLAIN ANALYZE SELECT nextval('s4')", "SELECT is_called FROM s4", [(True,)]),
    ("EXPLAIN (ANALYZE, BUFFERS) SELECT set_config('search_path', 'elsewhere', false)", "SHOW search_path",
     [("elsewhere",)]),
    # Effects a rollback does not undo: replication slots, decoded changes,
    # statistics, server files.
    ("SELECT pg_drop_replication_slot('bow_slot')",
     "SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'bow_slot'", [(0,)]),
    ("SELECT pg_create_physical_replication_slot('bow_made')",
     "SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'bow_made'", [(1,)]),
    ("SELECT count(*) FROM pg_logical_slot_get_changes('bow_cdc', NULL, NULL)",
     "SELECT count(*) FROM pg_logical_slot_peek_changes('bow_cdc', NULL, NULL)", [(0,)]),
    ("SELECT pg_stat_reset()",
     "SELECT stats_reset IS NOT NULL FROM pg_stat_database WHERE datname = current_database()", [(True,)]),
    ("SELECT pg_catalog.pg_file_write('/tmp/bow_admin_write', 'w', false)",
     "SELECT pg_read_file('/tmp/bow_admin_write')", [("w",)]),
    ("SELECT lo_export(4242, '/tmp/bow_lo_export')", "SELECT pg_read_file('/tmp/bow_lo_export')", [("x",)]),
    # The same functions written as a field.
    ("SELECT (('s7'::regclass)).nextval", "SELECT is_called FROM s7", [(True,)]),
    ("SELECT (45::bigint).pg_advisory_lock", LOCKS_HELD, [(1,)]),
    ("SELECT (0).pg_cancel_backend", None, None),
    ("SELECT ('SELECT to_tsvector(''simple'', code) FROM public.secret'::text).ts_stat", None, None),
    ("SELECT ('bow_slot2'::name).pg_drop_replication_slot",
     "SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'bow_slot2'", [(0,)]),
]


@pytest.mark.parametrize("sql, then, shows", ENGINE_SHAPES, ids=[s[0][:60] for s in ENGINE_SHAPES])
def test_postgres_runs_each_refused_shape_and_the_guard_refuses_it(pg, server, sql, then, shows):
    sql = sql.replace("{dsn}", server["dsn"])
    rows, after = pg(sql, then)
    if then is None:
        assert rows is not None
        if "secret" in sql:
            assert "k1" in str(rows) or rows == [(1,)]  # the table's rows, or a test on them
    else:
        assert after == shows
    with pytest.raises(ReadOnlyViolation):
        _checked(sql)


def test_postgres_reads_an_inactive_table_through_a_field_call_and_table_scope_refuses_it(pg):
    sql = "SELECT ('SELECT to_tsvector(''simple'', code) FROM public.secret'::text).ts_stat AS v FROM orders"
    assert "k1" in str(pg(sql)[0])
    with pytest.raises(TableScopeViolation, match="TS_STAT"):
        check_table_scope(sql, "postgres", SCOPE)


def test_a_select_only_role_writes_a_message_that_survives_rollback_and_the_guard_refuses_it(pg, server):
    import psycopg2

    sql = "SELECT pg_logical_emit_message(false, 'bow_reader_note', 'x')"
    reader = psycopg2.connect(**{**{k: server[k] for k in _CONNECT_KEYS}, "user": READER, "password": READER})
    try:
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders")
            cur.execute(sql)
        reader.rollback()
    finally:
        reader.close()
    # Any commit flushes the WAL the message went to; the slot then decodes it.
    pg("CREATE TABLE flushes_the_wal (id int)")
    peek = "SELECT count(*) FROM pg_logical_slot_peek_changes('bow_messages', NULL, NULL) WHERE data LIKE '%bow_reader_note%'"
    assert pg(peek)[0] == [(1,)]
    with pytest.raises(ReadOnlyViolation, match="PG_LOGICAL_EMIT_MESSAGE"):
        _checked(sql)


def test_postgres_runs_the_reads_the_guard_lets_through(pg):
    for sql in ["EXPLAIN SELECT 1", "EXPLAIN (SELECT 1)", "SELECT pg_advisory_xact_lock(9)",
                'SELECT "table" FROM (SELECT 1 AS "table") t'] + LEGITIMATE_READS:
        assert _checked(sql) == sql
        assert pg(sql)[0]


# Volatile functions in pg_catalog a read may call from SQL: not type I/O,
# trigger, handler or support functions (they take or return an internal
# type, so SQL cannot call them).
VOLATILE_CATALOG_FUNCTIONS = """
SELECT DISTINCT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'pg_catalog' AND p.provolatile = 'v' AND p.prokind = 'f'
  AND p.prorettype NOT IN ('trigger'::regtype, 'event_trigger'::regtype, 'internal'::regtype,
                           'language_handler'::regtype, 'fdw_handler'::regtype, 'index_am_handler'::regtype,
                           'table_am_handler'::regtype, 'tsm_handler'::regtype, 'cstring'::regtype)
  AND NOT ('internal'::regtype = ANY (p.proargtypes::regtype[]))
  AND NOT ('cstring'::regtype = ANY (p.proargtypes::regtype[]))
"""
# Reviewed: volatile, and still fine in a read.
REVIEWED_ALLOWED = {
    # Clocks, random values and the seed (session state of no consequence).
    "clock_timestamp", "timeofday", "random", "setseed", "gen_random_uuid",
    # Sequence values this session already drew; no sequence moves.
    "currval", "lastval", "pg_sequence_last_value",
    # Advisory locks that end with the transaction.
    "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared",
    "pg_try_advisory_xact_lock", "pg_try_advisory_xact_lock_shared",
    # Sleeping holds the connection; the query timeout bounds it.
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    # A snapshot others may import until this transaction ends.
    "pg_export_snapshot",
    # This session's statistics snapshot and flush timing.
    "pg_stat_clear_snapshot", "pg_stat_force_next_flush",
    # Sizes, locks, activity, WAL positions, recovery and control state.
    "pg_database_size", "pg_indexes_size", "pg_relation_size", "pg_table_size", "pg_tablespace_size",
    "pg_total_relation_size", "pg_blocking_pids", "pg_safe_snapshot_blocking_pids", "pg_lock_status",
    "pg_isolation_test_session_is_blocked", "pg_get_multixact_members", "pg_prepared_xact",
    "pg_notification_queue_usage", "pg_current_wal_lsn", "pg_current_wal_insert_lsn",
    "pg_current_wal_flush_lsn", "pg_last_wal_receive_lsn", "pg_last_wal_replay_lsn",
    "pg_last_xact_replay_timestamp", "pg_is_in_recovery", "pg_is_wal_replay_paused",
    "pg_get_wal_replay_pause_state", "pg_get_wal_resource_managers", "pg_stat_get_recovery_prefetch",
    "pg_control_checkpoint", "pg_control_init", "pg_control_recovery", "pg_control_system",
    "pg_last_committed_xact", "pg_xact_commit_timestamp", "pg_xact_commit_timestamp_origin",
    "pg_xact_status", "txid_status", "pg_jit_available", "pg_get_backend_memory_contexts",
    "pg_get_shmem_allocations", "pg_show_replication_origin_status", "pg_stat_have_stats",
    "pg_stat_get_xact_blocks_fetched", "pg_stat_get_xact_blocks_hit", "pg_stat_get_xact_function_calls",
    "pg_stat_get_xact_function_self_time", "pg_stat_get_xact_function_total_time",
    "pg_stat_get_xact_numscans", "pg_stat_get_xact_tuples_deleted", "pg_stat_get_xact_tuples_fetched",
    "pg_stat_get_xact_tuples_hot_updated", "pg_stat_get_xact_tuples_inserted",
    "pg_stat_get_xact_tuples_returned", "pg_stat_get_xact_tuples_updated",
    # Server settings and access rules, as the server holds them (superuser
    # or pg_read_all_settings only).
    "pg_hba_file_rules", "pg_ident_file_mappings", "pg_show_all_file_settings", "pg_current_logfile",
    # The query text, catalog validators and lookups, collation versions,
    # partitions, row lookups by position.
    "current_query", "amvalidate", "plpgsql_validator", "pg_collation_actual_version",
    "pg_database_collation_actual_version", "pg_partition_ancestors", "pg_partition_tree", "currtid2",
}


def test_every_volatile_catalog_function_is_denied_or_reviewed(pg):
    denied = DENIED_FUNCTIONS["postgres"]
    names = {row[0] for row in pg(VOLATILE_CATALOG_FUNCTIONS)[0]}
    assert len(names) > 150  # the listing works
    assert sorted(n for n in names if not denied.fullmatch(n) and n not in REVIEWED_ALLOWED) == []
    assert sorted(n for n in REVIEWED_ALLOWED if denied.fullmatch(n)) == []
    assert sorted(REVIEWED_ALLOWED - names) == []  # no stale entry


class LivePostgresqlClient(PostgresqlClient):
    """The real Postgres client on the test server, recording what reaches
    `execute_query` before passing it on."""

    def execute_query(self, sql):
        self.sent.append(sql)
        return super().execute_query(sql)


def _live(server):
    client = LivePostgresqlClient(host=server["host"], port=server["port"], database=server["dbname"],
                                  user=server["user"], password=server["password"])
    client.sent = []
    return client


@pytest.mark.parametrize("sql, then, unchanged", [
    ("SELECT setval('s5', 100)", "SELECT last_value, is_called FROM s5", [(1, False)]),
    ("EXPLAIN ANALYZE SELECT nextval('s6')", "SELECT last_value, is_called FROM s6", [(1, False)]),
    ("EXPLAIN ANALYZE SELECT 1 AS a INTO made_through_the_wrapper",
     "SELECT to_regclass('made_through_the_wrapper')::text", [(None,)]),
    ("SELECT dblink_exec('{dsn}', 'ANALYZE quiet')",
     "SELECT last_analyze IS NOT NULL FROM pg_stat_user_tables WHERE relname = 'quiet'", [(False,)]),
    ("WITH x AS (TABLE secret) SELECT * FROM x", "SELECT 1", [(1,)]),
    ("SELECT (('s8'::regclass)).nextval", "SELECT last_value, is_called FROM s8", [(1, False)]),
    ("SELECT pg_catalog.pg_file_write('/tmp/bow_never_written', 'w', false)",
     "SELECT (pg_stat_file('/tmp/bow_never_written', true)).size", [(None,)]),
    ("SELECT ('bow_kept'::name).pg_drop_replication_slot",
     "SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'bow_kept'", [(1,)]),
])
def test_a_refused_shape_never_reaches_the_server(mode, pg, server, sql, then, unchanged):
    client = _live(server)
    with pytest.raises(UnsafeSQLError):
        _run(client, sql.replace("{dsn}", server["dsn"]))
    assert client.sent == []
    assert pg(then)[0] == unchanged


def test_a_read_through_the_wrapper_reaches_the_server(mode, server):
    client = _live(server)
    result = _run(client, "EXPLAIN SELECT id FROM orders")
    assert client.sent == ["EXPLAIN SELECT id FROM orders"]
    assert "Seq Scan on orders" in str(result)
