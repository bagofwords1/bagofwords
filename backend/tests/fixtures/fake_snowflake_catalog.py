"""A Snowflake account with several databases, faked at the driver boundary.

What catalog discovery needs from Snowflake is each database's own
`<DB>.INFORMATION_SCHEMA` (columns, and schemata to probe access) plus
`SHOW SEMANTIC VIEWS`. This fake answers exactly those from an in-memory
catalog, and records every statement so a test can see which database each
read went to. It replaces only the engine the client builds: SnowflakeClient's
own discovery, naming, schema filtering and connection checks run unchanged.

It does not execute data queries; the sandbox loop (a real Snowflake emulator)
covers cross-database joins returning rows.

Usage:

    account = FakeSnowflakeAccount({"DB_A": {"SALES": {"ORDERS": ["ID", "AMOUNT"]}}})
    account.install(monkeypatch)
"""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace
from typing import Any

import sqlalchemy

_COLUMNS_FROM = re.compile(r"FROM\s+(\S+)\.INFORMATION_SCHEMA\.COLUMNS", re.I)
_SCHEMATA_FROM = re.compile(r"FROM\s+(\S+)\.INFORMATION_SCHEMA\.SCHEMATA", re.I)


def _db_key(name: str) -> str:
    name = name.strip()
    return name[1:-1] if name.startswith('"') and name.endswith('"') else name.upper()


class FakeSnowflakeAccount:
    def __init__(self, databases: dict[str, dict[str, dict[str, list[str]]]]):
        # {database: {schema: {table: [column, ...]}}}, copied so a test that
        # revokes a database does not change the catalog other tests share.
        self.databases = copy.deepcopy(databases)
        self.statements: list[str] = []

    def install(self, monkeypatch) -> None:
        from app.data_sources.clients import snowflake_client

        shim = SimpleNamespace(
            create_engine=lambda *a, **kw: _FakeEngine(self),
            event=sqlalchemy.event,
            engine=sqlalchemy.engine,
        )
        monkeypatch.setattr(snowflake_client, "sqlalchemy", shim)

    def reads_of(self, database: str) -> list[str]:
        """Catalog statements that read `database`'s INFORMATION_SCHEMA."""
        return [s for s in self.statements if re.search(rf"\b{database}\.INFORMATION_SCHEMA\b", s, re.I)]

    # -- answering --------------------------------------------------------
    def _columns(self, db: str, sql: str, params: dict) -> list[tuple]:
        catalog = self.databases.get(_db_key(db))
        if catalog is None:
            raise RuntimeError(f"Database '{db}' does not exist or not authorized.")
        wanted = {v for k, v in (params or {}).items() if re.fullmatch(r"s\d+", k)}
        enriched = "COMMENT" in sql.upper()
        rows = []
        for schema in sorted(catalog):
            if wanted and schema not in wanted:
                continue
            for table in sorted(catalog[schema]):
                for column in catalog[schema][table]:
                    base = (schema, table, column, "NUMBER")
                    rows.append(base + (None, None) if enriched else base)
        return rows

    def answer(self, sql: str, params: dict | None) -> list[tuple]:
        self.statements.append(sql)
        if m := _COLUMNS_FROM.search(sql):
            return self._columns(m.group(1), sql, params or {})
        if m := _SCHEMATA_FROM.search(sql):
            if _db_key(m.group(1)) not in self.databases:
                raise RuntimeError(f"Database '{m.group(1)}' does not exist or not authorized.")
            return [(1,)]
        if re.match(r"\s*SHOW\s+SEMANTIC\s+VIEWS", sql, re.I):
            return []
        if re.match(r"\s*SELECT\s+1\s*$", sql, re.I):
            return [(1,)]
        raise NotImplementedError(f"fake Snowflake does not answer: {sql[:120]}")


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, account: FakeSnowflakeAccount):
        self._account = account
        self.info: dict[str, Any] = {}

    def execute(self, statement, parameters=None):
        return _FakeResult(self._account.answer(str(statement), parameters))

    def exec_driver_sql(self, statement, parameters=None):
        # ALTER SESSION SET QUERY_TAG: accepted, nothing to return.
        self._account.statements.append(str(statement))
        return _FakeResult([])

    def rollback(self):
        pass

    def close(self):
        pass


class _FakeEngine:
    def __init__(self, account: FakeSnowflakeAccount):
        self._account = account

    def connect(self):
        return _FakeConnection(self._account)
