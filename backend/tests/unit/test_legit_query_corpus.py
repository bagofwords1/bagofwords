"""Ordinary queries keep passing every check.

Contract: every query in `LEGIT_QUERIES` (the shapes agents, saved steps and
custom tables send: date parts, casts, quoted names, comments, dollar
quotes, backslashes in literals, CTEs, window functions) passes the
read-only guard for its dialect, and table scope accepts the text the guard
returned when every table it reads is active. A query scoped to the
viewer's identity, rendered by the connection's own renderer (values with a
quote or a backslash included), passes both checks and still counts as
filtered on that identity.

A failure here is an over-refusal: a legitimate query the checks no longer
let through. Over-refusals known today are listed with their reason in
`KNOWN_OVER_REFUSALS` and expected to fail until their rule is fixed.
"""

from __future__ import annotations

import importlib

import pytest

from app.ai.code_execution.client_profiles import _CLIENT_TABLE, family_name
from app.ai.code_execution.identity_bind import CapturedQuery
from app.ai.code_execution.query_params import render_sql_with_params, renderer_for, verify_identity_binds_in_queries
from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
from app.ai.code_execution.table_scope import TableScope, TableScopeViolation, check_table_scope, dialect_for_client
from app.schemas.param_schema import ParamSpec
from tests.unit.sql_corpus.legit_queries import (
    CORPUS_TABLES,
    IDENTITY_QUERIES,
    KNOWN_OVER_REFUSALS,
    LEGIT_QUERIES,
    NONPARSING_IDENTITY_TEMPLATES,
    NONPARSING_QUERIES,
)

BS = chr(92)


# Clients whose guard also reads their configuration, configured so every
# corpus table is theirs: a DuckDB connection to a database file reads its
# tables by plain name. No file is opened.
CONFIGURED = {"DuckDBClient": {"database": "/data/corpus.duckdb"}}


def _sql_clients() -> dict:
    """Every client the guard checks as parsed SQL, from its own registry,
    by family. No connection is opened: the checks read the client's type
    (and, for `CONFIGURED`, its configuration) only."""
    clients = {}
    for (module, name), entry in _CLIENT_TABLE.items():
        if entry.profile is not None and entry.profile.kind == "sql":
            client = object.__new__(getattr(importlib.import_module(module), name))
            if entry.family in CONFIGURED:
                client.__init__(**CONFIGURED[entry.family])
            clients[entry.family] = client
    return clients


def _unparsed_clients() -> dict:
    """Every client the guard checks without a parser, by family."""
    return {entry.family: object.__new__(getattr(importlib.import_module(module), name))
            for (module, name), entry in _CLIENT_TABLE.items()
            if entry.profile is not None and entry.profile.kind == "nonparsing_sql"}


CLIENTS = _sql_clients()
UNPARSED_CLIENTS = _unparsed_clients()
# Each client's dialect as the guard and table scope read it.
DIALECTS = {family: dialect_for_client(client) for family, client in CLIENTS.items()}

SCOPE = TableScope.from_names(active=CORPUS_TABLES, inactive=[])
SPEC = [ParamSpec.model_validate({"name": "owner", "type": "string", "source": "identity"})]


# Timbr: its renderer refuses a backslash in a value, and writes a quote
# doubled, which Spark SQL (how the checks read Timbr) reads as two strings
# side by side, so the identity check refuses a value holding a quote.
# Both fail closed; such values are left out here.
UNRENDERED_VALUES = {"TimbrClient": (BS, "'")}


def _per_client(entries) -> list:
    """Each entry (dialect first) once for every client of its dialect."""
    return [pytest.param(family, *entry, id=f"{family}-{i}")
            for family, dialect in DIALECTS.items()
            for i, entry in enumerate(entries) if entry[0] == dialect
            and not (entries is IDENTITY_QUERIES and any(c in entry[-1] for c in UNRENDERED_VALUES.get(family, ())))]


def _over_refusals() -> list:
    return [pytest.param(*p.values, marks=pytest.mark.xfail(
        strict=True, raises=(ReadOnlyViolation, TableScopeViolation), reason=f"known over-refusal: {p.values[-1]}"),
        id=p.id) for p in _per_client(KNOWN_OVER_REFUSALS)]


def test_the_corpus_covers_every_dialect_and_the_shapes_that_must_keep_working():
    dialects = {dialect for dialect, _sql in LEGIT_QUERIES}
    assert dialects == set(DIALECTS.values()), sorted(dialects ^ set(DIALECTS.values()))
    for dialect in dialects:
        assert sum(d == dialect for d, _sql in LEGIT_QUERIES) >= 3, dialect
    postgres = [sql for dialect, sql in LEGIT_QUERIES if dialect == "postgres"]
    assert any("date_part('year'" in sql for sql in postgres)
    assert any(BS in sql for sql in postgres), "a backslash in a standard literal"
    assert any("E'" in sql for sql in postgres) and any("$$" in sql for sql in postgres)
    assert any("--" in sql for sql in postgres) and any("/*" in sql for sql in postgres)
    assert any('"' in sql for sql in postgres), "a quoted name"
    assert len(postgres) >= 40
    values = {value for _dialect, _template, value in IDENTITY_QUERIES}
    assert "DOMAIN" + BS + "user" in values and any("'" in value for value in values)
    assert any("date_part('year'" in template for _d, template, _v in IDENTITY_QUERIES)
    assert {dialect for dialect, _t, _v in IDENTITY_QUERIES} >= {"postgres", "mysql", "tsql", "snowflake"}


@pytest.mark.parametrize("family,dialect,sql", _per_client(LEGIT_QUERIES))
def test_a_legitimate_query_passes_the_guard_and_table_scope(family, dialect, sql):
    checked = ensure_read_only(CLIENTS[family], sql)
    check_table_scope(checked, dialect, SCOPE)


def _denied_names(message: str) -> list[str]:
    """The names a "Table access denied" refusal lists."""
    listed = message.split("Table access denied: ", 1)[1]
    listed = listed.split(" is not enabled", 1)[0].split(" are not enabled", 1)[0]
    return listed.split(", ")


@pytest.mark.parametrize("dialect", sorted(set(DIALECTS.values())))
def test_with_each_table_disabled_in_turn_the_corpus_is_refused_only_where_it_reads_that_table(dialect):
    """With one corpus table disabled (and every other one enabled), a
    legitimate query is refused only because it reads a table whose name
    ends like the disabled one, or because one of its WITH entries takes
    the disabled table's name. Nothing else changes with the scope: an
    alias (`count() AS events`) may share a disabled table's name."""
    from sqlglot import exp

    from app.ai.code_execution.sql_parser import parse_guard_sql

    client = CLIENTS[next(f for f, d in DIALECTS.items() if d == dialect)]
    refused = {"table": 0, "entry": 0}
    for sql in [s for d, s in LEGIT_QUERIES if d == dialect]:
        checked = ensure_read_only(client, sql)
        entries = {cte.alias_or_name.casefold() for s in parse_guard_sql(checked, dialect) if s is not None
                   for cte in s.find_all(exp.CTE)}
        for table in CORPUS_TABLES:
            scope = TableScope.from_names(active=[t for t in CORPUS_TABLES if t != table], inactive=[table])
            parts = table.upper().split(".")
            try:
                check_table_scope(checked, dialect, scope)
            except TableScopeViolation as e:
                message = str(e)
                if message.startswith("Table access denied: "):
                    shown = _denied_names(message)
                    assert all(parts[-len(n.split(".")):] == n.split(".") for n in shown), (sql, table, message)
                    refused["table"] += 1
                else:
                    assert "a WITH entry may not take its name" in message, (sql, table, message)
                    assert table.split(".")[-1].casefold() in entries, (sql, table, message)
                    refused["entry"] += 1
    # Every dialect's corpus reads `orders`; ClickHouse's also has an entry
    # named after it.
    assert refused["table"] > 0
    if dialect == "clickhouse":
        assert refused["entry"] > 0


@pytest.mark.parametrize("family,dialect,sql,reason", _over_refusals())
def test_a_known_over_refusal_is_still_refused_until_its_rule_is_fixed(family, dialect, sql, reason):
    """Strict: once the checks let one of these through, this fails, and
    the query moves to LEGIT_QUERIES. Only a refusal counts as the expected
    failure."""
    test_a_legitimate_query_passes_the_guard_and_table_scope(family, dialect, sql)


def test_the_unparsed_corpus_covers_every_client_checked_without_a_parser():
    assert {"Db2Client", "IbmiClient"} <= set(UNPARSED_CLIENTS)
    assert len(NONPARSING_QUERIES) >= 15 and any(BS in sql for sql in NONPARSING_QUERIES)


@pytest.mark.parametrize("family", sorted(UNPARSED_CLIENTS))
@pytest.mark.parametrize("sql", NONPARSING_QUERIES)
def test_a_legitimate_query_passes_the_guard_of_a_client_checked_without_a_parser(family, sql):
    assert ensure_read_only(UNPARSED_CLIENTS[family], sql) == sql


@pytest.mark.parametrize("family", sorted(UNPARSED_CLIENTS))
@pytest.mark.parametrize("template", NONPARSING_IDENTITY_TEMPLATES)
@pytest.mark.parametrize("value", ["sarah@example.com", "DOMAIN" + BS + "user", "it's"])
def test_an_identity_scoped_query_passes_the_guard_of_a_client_checked_without_a_parser(family, template, value):
    client = UNPARSED_CLIENTS[family]
    rendered = render_sql_with_params(template, {"owner": value}, renderer=renderer_for(client))
    checked = ensure_read_only(client, rendered)
    capture = CapturedQuery(checked, family_name(client))
    assert verify_identity_binds_in_queries([capture], {"owner": value}, SPEC) is None


@pytest.mark.parametrize("family,dialect,template,value", _per_client(IDENTITY_QUERIES))
def test_an_identity_scoped_query_passes_every_check_and_binds_the_identity(family, dialect, template, value):
    client = CLIENTS[family]
    rendered = render_sql_with_params(template, {"owner": value}, renderer=renderer_for(client))
    checked = ensure_read_only(client, rendered)
    check_table_scope(checked, dialect, SCOPE)
    capture = CapturedQuery(checked, family_name(client))
    assert verify_identity_binds_in_queries([capture], {"owner": value}, SPEC) is None
