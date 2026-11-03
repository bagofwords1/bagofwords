"""Valid SQLite comparisons remain readable without hiding referenced tables."""

import sqlite3

import pytest

from app.ai.code_execution.sql_guard import ReadOnlyViolation, check_sql
from app.ai.code_execution.table_scope import TableScope, TableScopeViolation, check_table_scope


@pytest.fixture
def ledger():
    # This is the external SQLite source, not application database state.
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE ledger (a, b)")
    db.executemany("INSERT INTO ledger VALUES (?, ?)", [
        (1, 1), (1, 2), (None, 1), (1, None), (None, None),
    ])
    yield db
    db.close()


COMPARISONS = [
    "l.a IS l.b",
    "l.a IS NOT l.b",
    "l.a IS abs(l.b)",
    "l.a IS NOT coalesce(l.b, 0)",
    "l.a IS l.b + 1",
    "l.a IS NOT -l.b",
    "l.a IS (SELECT max(b) FROM ledger)",
    "l.a IS NOT (SELECT min(b) FROM ledger)",
    "l.a IS NULL",
    "l.a IS NOT NULL",
    "l.a IS TRUE",
    "l.a IS NOT FALSE",
    "l.a IS DISTINCT FROM l.b",
    "l.a IS NOT DISTINCT FROM l.b",
    "(l.a = l.b) IS NOT 1",
    "l.a IS /* IS NOT hidden */ l.b",
    "l.a IS NOT 'IS NOT hidden'",
]


@pytest.mark.parametrize("predicate", COMPARISONS)
def test_sqlite_comparisons_pass_guards_and_execute_unchanged(ledger, predicate):
    sql = f"SELECT {predicate} AS matches FROM ledger l"
    expected = ledger.execute(sql).fetchall()
    check_sql(sql, "sqlite")
    check_table_scope(sql, "sqlite", TableScope.from_names(["ledger"], ["hidden"]))
    assert ledger.execute(sql).fetchall() == expected
    assert len(expected) == 5


@pytest.mark.parametrize("sql", [
    "SELECT * FROM ledger l JOIN ledger r ON l.a IS r.b",
    "WITH x AS (SELECT * FROM ledger) SELECT * FROM x WHERE a IS NOT b",
    "SELECT CASE WHEN a IS NOT abs(b) THEN 1 ELSE 0 END FROM ledger",
])
def test_sqlite_comparison_contexts_pass_guards(ledger, sql):
    expected = ledger.execute(sql).fetchall()
    check_sql(sql, "sqlite")
    check_table_scope(sql, "sqlite", TableScope.from_names(["ledger"], []))
    assert ledger.execute(sql).fetchall() == expected


@pytest.mark.parametrize("operator", ["IS", "IS NOT", "IS DISTINCT FROM", "IS NOT DISTINCT FROM"])
@pytest.mark.parametrize("operand", [
    "(SELECT secret FROM hidden)",
    "coalesce((SELECT secret FROM hidden), 0)",
    "(WITH x AS (SELECT secret FROM hidden) SELECT secret FROM x)",
    "(SELECT secret FROM hidden WHERE EXISTS (WITH hidden AS (SELECT 1) SELECT * FROM hidden))",
])
def test_comparison_operands_cannot_hide_disabled_tables(operator, operand):
    sql = f"SELECT a {operator} {operand} FROM ledger"
    with pytest.raises(TableScopeViolation) as exc:
        check_table_scope(sql, "sqlite", TableScope.from_names(["ledger"], ["hidden"]))
    # The parser must expose the nested table, not merely fail to parse.
    assert "HIDDEN" in str(exc.value)


@pytest.mark.parametrize("sql", [
    "SELECT a IS NOT FROM ledger",
    "SELECT a IS (SELECT FROM hidden) FROM ledger",
    "SELECT a IS coalesce( FROM ledger",
])
def test_malformed_sqlite_comparisons_still_fail_closed(sql):
    with pytest.raises(TableScopeViolation):
        check_table_scope(sql, "sqlite", TableScope.from_names(["ledger"], []))


@pytest.mark.parametrize("sql", [
    "DELETE FROM ledger WHERE a IS b",
    "UPDATE ledger SET a=0 WHERE a IS NOT b",
    "SELECT a IS b FROM ledger; DELETE FROM ledger",
    "SELECT a IS (SELECT b INTO stolen FROM ledger) FROM ledger",
])
def test_sqlite_comparisons_do_not_allow_writes(sql):
    with pytest.raises(ReadOnlyViolation):
        check_sql(sql, "sqlite")


@pytest.mark.parametrize("operator,expected", [
    ("IS", [1, 0, 0, 0, 1]),
    ("IS NOT", [0, 1, 1, 1, 0]),
])
def test_generated_code_executes_sqlite_comparisons_through_both_guards(tmp_path, operator, expected):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor
    from app.ai.code_execution.table_scope import TABLE_SCOPE_ATTR
    from app.data_sources.clients.sqlite_client import SqliteClient

    path = tmp_path / "source.db"
    # Seed a real external source so the production client/executor path runs.
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ledger (id INTEGER, a INTEGER, b INTEGER)")
        db.executemany("INSERT INTO ledger VALUES (?, ?, ?)", [
            (1, 1, 1), (2, 1, 2), (3, None, 1), (4, 1, None), (5, None, None),
        ])
    client = SqliteClient(database=str(path))
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(["ledger"], []))
    sql = f"SELECT a {operator} b AS matches FROM ledger ORDER BY id"
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients['source'].execute_query({sql!r})\n"
    )
    frame, _, queries = StreamingCodeExecutor().execute_code(
        code=code, ds_clients={"source": client}, excel_files=[],
    )
    assert frame["matches"].tolist() == expected
    assert queries == [sql]
