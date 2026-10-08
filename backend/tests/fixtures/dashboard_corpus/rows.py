"""Write corpus rows with SQLAlchemy Core against the schema the database has.

Why not the ORM models: the corpus stands for data customers already have,
written by older releases. It is inserted at `MAIN_BASE_REVISION`, before
this branch's migrations run, so the rows must take the *database's* shape
at that revision -- not the shape of the models on this branch, which gain
columns the base schema does not have. Reflecting the live tables keeps the
seed correct in every phase without rewriting it.

Why not the API (tests/AGENTS.md rule 5): no route creates Steps or
Visualizations (only the agent does), and the old shapes the migration has
to meet -- artver01 rows, published widget dashboards, legacy cron values --
cannot be produced by today's code at all.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import MetaData, Table, create_engine
from sqlalchemy import null as sa_null
from sqlalchemy import types as sa_types
from sqlalchemy.engine import Engine

from app.models.base import Base


def sync_test_url() -> str:
    from app.settings.config import settings

    url = os.environ.get("TEST_DATABASE_URL") or settings.TEST_DATABASE_URL
    return url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")


def new_id() -> str:
    return str(uuid.uuid4())


class Rows:
    """Inserts rows into reflected tables, filling what the ORM would fill.

    Columns the caller leaves out get the model's Python-side default when
    the model still declares that column; a NOT NULL column with neither a
    value, a server default nor a model default is an error -- the corpus
    must say what it means rather than rely on an accident.
    """

    def __init__(self, engine: Engine | None = None):
        self.engine = engine or create_engine(sync_test_url())
        self._meta = MetaData()
        self._tables: dict[str, Table] = {}
        # Timestamps the caller leaves out come from this clock. The corpus
        # seeder points it at its fixed clock so no row depends on the date
        # the test runs; until then (people created through the API) it is
        # the wall clock.
        self.clock: Callable[[], datetime] = datetime.utcnow
        # Called with (table, id) after every insert; the seeder uses it to
        # give every corpus row a stable name for the snapshot.
        self.on_insert: Callable[[str, str], None] | None = None

    def close(self) -> None:
        self.engine.dispose()

    def table(self, name: str) -> Table:
        if name not in self._tables:
            self._tables[name] = Table(name, self._meta, autoload_with=self.engine)
        return self._tables[name]

    def _model_default(self, table: str, column: str) -> tuple[bool, Any]:
        model_table = Base.metadata.tables.get(table)
        if model_table is None or column not in model_table.c:
            return False, None
        default = model_table.c[column].default
        if default is None:
            return False, None
        if default.is_callable:
            return True, default.arg(None)
        if default.is_scalar:
            return True, default.arg
        return False, None

    def values_for(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        tbl = self.table(table)
        row = dict(values)
        unknown = set(row) - set(tbl.c.keys())
        if unknown:
            raise KeyError(f"{table} has no column(s) {sorted(unknown)} at this revision")
        now = self.clock()
        if "id" in tbl.c and "id" not in row and tbl.c["id"].type.python_type is str:
            row["id"] = new_id()
        for stamp in ("created_at", "updated_at"):
            if stamp in tbl.c and stamp not in row:
                row[stamp] = now
        for name, value in list(row.items()):
            # None on a JSON column means SQL NULL (as the ORM's none_as_null
            # columns and raw purges write), never the JSON literal null.
            if value is None and isinstance(tbl.c[name].type, sa_types.JSON):
                row[name] = sa_null()
        for col in tbl.columns:
            if col.name in row or col.server_default is not None:
                continue
            found, value = self._model_default(table, col.name)
            if found:
                row[col.name] = value
            elif not col.nullable and not col.primary_key:
                raise ValueError(f"{table}.{col.name} is NOT NULL and the corpus gave no value")
        return row

    def insert(self, table: str, **values: Any) -> str | None:
        row = self.values_for(table, values)
        with self.engine.begin() as conn:
            conn.execute(self.table(table).insert().values(**row))
        if self.on_insert and row.get("id"):
            self.on_insert(table, str(row["id"]))
        return row.get("id")

    def update(self, table: str, row_id: str, **values: Any) -> None:
        tbl = self.table(table)
        with self.engine.begin() as conn:
            conn.execute(tbl.update().where(tbl.c.id == row_id).values(**values))
