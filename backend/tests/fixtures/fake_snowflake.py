"""A stand-in for a Snowflake account at the driver boundary.

What the query-tag tests need from Snowflake is small and observable: each
session keeps its own `QUERY_TAG` (set by `ALTER SESSION`), every statement
gets a query id on the cursor (`sfqid`), and every statement lands in a query
history together with the tag its session had at that moment. That is what a
warehouse admin reads in `ACCOUNT_USAGE.QUERY_HISTORY`.

This fake provides exactly that, and runs the statements themselves on SQLite
so ordinary SELECTs return real rows. It replaces the driver only: the
SnowflakeClient code, the SQLAlchemy pool and pandas all run unchanged.

Usage:

    server = FakeSnowflakeServer(tmp_path / "wh.db")
    server.install(monkeypatch)          # SnowflakeClient engines now hit it
    client = SnowflakeClient(account="a", warehouse="w", database="d",
                             user="u", password="p")
    client.execute_query("SELECT 1")
    server.history[-1]["query_tag"]       # the tag Snowflake would record
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from types import ModuleType, SimpleNamespace
from typing import Any

import sqlalchemy
from sqlalchemy.pool import QueuePool

_ALTER_TAG = re.compile(r"^\s*ALTER\s+SESSION\s+SET\s+QUERY_TAG\s*=\s*'(.*)'\s*;?\s*$", re.I | re.S)
_UNSET_TAG = re.compile(r"^\s*ALTER\s+SESSION\s+UNSET\s+QUERY_TAG\s*;?\s*$", re.I)


def _unquote(literal: str) -> str:
    # Snowflake string literals: '' is a quote, backslash escapes.
    return literal.replace("''", "'").replace("\\\\", "\\")


class FakeSnowflakeServer:
    def __init__(self, db_path, pool_size: int = 1):
        self.db_path = str(db_path)
        self.pool_size = pool_size
        self.history: list[dict[str, Any]] = []
        self.sessions_opened = 0
        self._lock = threading.Lock()

    # -- wiring ---------------------------------------------------------
    def engine(self):
        """A pooled engine whose DBAPI connections are fake Snowflake sessions.

        The fake is handed to SQLAlchemy as the DBAPI *module* (not a
        `creator=`), so connect-time hooks run as with the real driver and the
        connect arguments, `session_parameters` included, reach `connect()`.
        """
        return sqlalchemy.create_engine(
            f"sqlite:///{self.db_path}",
            module=self._dbapi(),
            poolclass=QueuePool,
            pool_size=self.pool_size,
            max_overflow=0,
        )

    def _dbapi(self) -> ModuleType:
        module = ModuleType("fake_snowflake_dbapi")
        for name in dir(sqlite3):
            if not name.startswith("__"):
                setattr(module, name, getattr(sqlite3, name))
        module.connect = self._connect
        return module

    def install(self, monkeypatch) -> None:
        """Make SnowflakeClient build its engine against this server.

        Only the engine factory is swapped; SnowflakeClient still builds its
        URL and connect args, and still instruments the engine itself.
        """
        from app.data_sources.clients import snowflake_client

        shim = SimpleNamespace(
            create_engine=lambda *a, **kw: self.engine(),
            event=sqlalchemy.event,
            engine=sqlalchemy.engine,
        )
        monkeypatch.setattr(snowflake_client, "sqlalchemy", shim)

    def _connect(self, *args, session_parameters: dict[str, Any] | None = None, **kwargs):
        # Like snowflake.connector.connect(session_parameters=...): the new
        # session starts with these parameters already set.
        with self._lock:
            self.sessions_opened += 1
            session_id = self.sessions_opened
        conn = _FakeConnection(self, sqlite3.connect(self.db_path, check_same_thread=False), session_id)
        conn.query_tag = str((session_parameters or {}).get("QUERY_TAG") or "")
        return conn

    def _record(self, session: _FakeConnection, statement: str, error: str | None = None) -> str:
        qid = str(uuid.uuid4())
        with self._lock:
            self.history.append({
                "query_id": qid,
                "query_text": statement,
                "query_tag": session.query_tag,
                "session_id": session.session_id,
                "error": error,
            })
        return qid

    # -- reading history like an admin would ----------------------------
    def rows(self, *, include_session_statements: bool = False) -> list[dict[str, Any]]:
        """History rows; by default without the ALTER SESSION bookkeeping."""
        if include_session_statements:
            return list(self.history)
        return [r for r in self.history if not r["query_text"].lstrip().upper().startswith("ALTER SESSION")]


class _FakeConnection:
    def __init__(self, server: FakeSnowflakeServer, raw: sqlite3.Connection, session_id: int):
        self._server = server
        self._raw = raw
        self.session_id = session_id
        self.query_tag = ""

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self, self._raw.cursor())

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _FakeCursor:
    def __init__(self, conn: _FakeConnection, raw: sqlite3.Cursor):
        self._conn = conn
        self._raw = raw
        self._session_statement = False
        self.sfqid: str | None = None

    @property
    def description(self):
        return None if self._session_statement else self._raw.description

    def execute(self, statement, parameters=()):
        server = self._conn._server
        set_match = _ALTER_TAG.match(statement)
        if set_match or _UNSET_TAG.match(statement):
            # Recorded with the tag the session had when it ran, like Snowflake.
            self.sfqid = server._record(self._conn, statement)
            self._conn.query_tag = _unquote(set_match.group(1)) if set_match else ""
            self._session_statement = True
            return self
        self._session_statement = False
        try:
            self._raw.execute(statement, parameters)
        except Exception as e:
            self.sfqid = server._record(self._conn, statement, error=str(e))
            # snowflake.connector's ProgrammingError carries the id as `sfqid`.
            e.sfqid = self.sfqid
            raise
        self.sfqid = server._record(self._conn, statement)
        return self

    def __getattr__(self, name):
        return getattr(self._raw, name)
