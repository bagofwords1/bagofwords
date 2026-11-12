"""Writing a custom table's artifact: whole, in chunks, or as an increment.

Three shapes of work, one writer:

* a **load** streams source batches into a fresh file. Done in one go it is a
  full refresh; done one window at a time into the same file it is a first
  load that can stop and resume between windows.
* an **increment** copies the previous artifact into a fresh file, stages the
  rows of one window beside it, and folds them in (merge by key, or replace
  the tail).
* **finalize** measures the result, runs the checks that decide whether it may
  be published, and gives the file its final name.

A served file is never touched. Every shape ends in a new file under a new
name, so a failure at any point leaves the previous artifact serving and a
reader that cached it by path stays correct.
"""

import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.data_sources.fast import artifacts, incremental
from app.data_sources.fast.extractor import ExtractionAborted, PublishCheckFailed
from app.data_sources.fast.extractor import ExtractResult as LoadOutcome
from app.data_sources.fast.normalize import normalize_table

logger = logging.getLogger(__name__)

RUN_ID = "_bow_run_id"
LOADED_AT = "_bow_loaded_at"
# Every row records which run wrote it and when. Hidden from agents (the
# serving view leaves them out) and from the stored column list.
HIDDEN_COLUMNS = (RUN_ID, LOADED_AT)

STAGING = "bow__staging"
# Which windows of a load in progress are already in the part file, written in
# the same transaction as their rows.
PROGRESS = "bow__progress"
# Tables the loader keeps for itself inside an artifact. A custom table may
# not use the prefix, and none of them survives into a published file.
INTERNAL_PREFIX = "bow__"

# Below this many rows a falling count is noise, not a signal.
ROW_DROP_GUARD_MIN_ROWS = 20



# --------------------------------------------------------------------------
# Numeric widening between batches, sized by the values (from #1685)
# --------------------------------------------------------------------------
# DuckDB's own union of two numeric types is type-based: BIGINT meeting
# DECIMAL(5,2) gives DECIMAL(21,2), and past 38 digits it caps the precision
# and cuts the fraction (DECIMAL(20,20) meeting DECIMAL(24,1) became
# DECIMAL(38,15), silently rounding the rows already stored). Where an integer
# or decimal meets a decimal, the column is sized by the digits its values
# actually have, and past 38 it becomes DOUBLE, with a warning.

def _arrow_numeric(duck_type: str):
    """The Arrow type for a DuckDB integer/decimal/double column name, else None."""
    import pyarrow as pa

    t = duck_type.upper()
    if t.startswith("DECIMAL(") and t.endswith(")"):
        p, sc = (int(x) for x in t[len("DECIMAL("):-1].split(","))
        return pa.decimal128(p, sc)
    return {
        "TINYINT": pa.int8(), "SMALLINT": pa.int16(), "INTEGER": pa.int32(),
        "BIGINT": pa.int64(), "UTINYINT": pa.uint8(), "USMALLINT": pa.uint16(),
        "UINTEGER": pa.uint32(), "UBIGINT": pa.uint64(), "DOUBLE": pa.float64(),
    }.get(t)


def _integer_digits(t) -> int:
    """Decimal digits that hold every value of integer type `t` (19 for int64)."""
    import pyarrow as pa

    top = 2 ** t.bit_width - 1 if pa.types.is_unsigned_integer(t) else 2 ** (t.bit_width - 1)
    return len(str(top))


def _value_digits(col) -> int:
    """Decimal digits of the largest magnitude in integer column `col`; 0 if all NULL."""
    import pyarrow.compute as pc

    if col.null_count == len(col):
        return 0
    bounds = pc.min_max(col)
    return max(len(str(abs(bounds[k].as_py()))) for k in ("min", "max"))


def _exact_decimal(digits: int, scale: int):
    """decimal(digits + scale, scale), or float64 when DuckDB has no such decimal."""
    import pyarrow as pa

    from app.data_sources.fast.normalize import DUCKDB_MAX_DECIMAL_PRECISION

    if digits + scale > DUCKDB_MAX_DECIMAL_PRECISION:
        return pa.float64()
    return pa.decimal128(digits + scale, scale)


def _wider(held, incoming, held_digits: int | None = None, incoming_digits: int | None = None):
    """The column type that holds both `held` and `incoming` values, if a change is needed.

    Where one side is an integer column, `held_digits` / `incoming_digits` are
    the digits its values actually have (see `_value_digits`). Sizing it by its
    type instead — 19 digits for any int64 — would ask for DECIMAL(39,20) to
    add the integer 1 to a DECIMAL(21,20) that already holds it, and past 38
    that is float64: exact rows made approximate for nothing.

    Returns None when the held type already holds `incoming`, or for a pair
    this does not decide (the caller falls back to DuckDB's own union).
    """
    import pyarrow as pa

    if held == incoming:
        return None
    if pa.types.is_decimal(held) and pa.types.is_decimal(incoming):
        scale = max(held.scale, incoming.scale)
        digits = max(held.precision - held.scale, incoming.precision - incoming.scale)
        if digits + scale == held.precision and scale == held.scale:
            return None
        return _exact_decimal(digits, scale)
    if pa.types.is_integer(held) and pa.types.is_decimal(incoming):
        held_digits = _integer_digits(held) if held_digits is None else held_digits
        return _exact_decimal(max(held_digits, incoming.precision - incoming.scale),
                              incoming.scale)
    if pa.types.is_decimal(held) and pa.types.is_integer(incoming):
        incoming_digits = (_integer_digits(incoming) if incoming_digits is None
                           else incoming_digits)
        digits = max(held.precision - held.scale, incoming_digits)
        if digits == held.precision - held.scale:
            return None
        return _exact_decimal(digits, held.scale)
    if pa.types.is_decimal(held) and pa.types.is_floating(incoming):
        return pa.float64()
    return None


def _duck_type(t) -> str:
    import pyarrow as pa

    if pa.types.is_decimal(t):
        return f"DECIMAL({t.precision},{t.scale})"
    return "DOUBLE"


class SchemaChanged(Exception):
    """The rows of an increment no longer fit the stored table."""


@dataclass
class Limits:
    """The ceilings one unit of work must stay under."""

    max_rows: int
    max_bytes: int
    deadline: float                                   # time.monotonic() value
    should_abort: Callable[[], bool] | None = None
    max_seconds: int = 0

    def check(self) -> None:
        if self.should_abort is not None and self.should_abort():
            raise ExtractionAborted(
                "Refresh was cancelled before it completed; "
                "the previous data is still being served."
            )
        if time.monotonic() > self.deadline:
            raise ExtractionAborted(
                f"Refresh exceeded the {self.max_seconds}s limit and was aborted; "
                f"the previous data is still being served."
            )


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _is_nested(type_name: str) -> bool:
    upper = (type_name or "").upper()
    return upper.startswith(("STRUCT", "MAP", "UNION")) or "STRUCT(" in upper or upper.endswith("[]")


class ArtifactWriter:
    """A writable encrypted DuckDB file holding one relation."""

    def __init__(self, path, key: str, relation: str, run_id: str, loaded_at: datetime | None = None):
        self.path = Path(path)
        self.key = key
        self.relation = relation
        self.run_id = run_id
        self.loaded_at = (loaded_at or datetime.utcnow()).replace(tzinfo=None)
        self.con = None
        # Columns typed from a batch that held no value: any type may replace
        # the placeholder until one does.
        self._placeholders: set = set()
        self._scanned: set = set()
        self._last_schema = {}

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "ArtifactWriter":
        self.con = artifacts.connect_encrypted(self.path, self.key)
        return self

    def __exit__(self, *exc) -> None:
        if self.con is not None:
            try:
                self.con.close()
            except Exception:
                pass
            self.con = None

    def size(self) -> int:
        return artifacts.artifact_size(str(self.path)) + artifacts.artifact_size(str(self.path) + ".wal")

    # -- catalog -----------------------------------------------------------

    def exists(self, table: str | None = None) -> bool:
        row = self.con.execute(
            "SELECT 1 FROM duckdb_tables() WHERE database_name = 'artifact' AND table_name = ?",
            [table or self.relation],
        ).fetchone()
        return row is not None

    def describe(self, table: str | None = None) -> list:
        """[(name, duckdb type)] of a table, in order."""
        rows = self.con.execute(f"DESCRIBE {_q(table or self.relation)}").fetchall()
        return [(r[0], r[1]) for r in rows]

    def data_columns(self, table: str | None = None) -> list:
        return [name for name, _t in self.describe(table) if name not in HIDDEN_COLUMNS]

    def columns(self) -> list:
        """The stored column list, in the shape the row has always kept:
        Arrow type names, hidden columns left out."""
        cols = ", ".join(_q(c) for c in self.data_columns()) or "*"
        schema = self.con.execute(
            f"SELECT {cols} FROM {_q(self.relation)} LIMIT 0"
        ).fetch_arrow_table().schema
        return [{"name": f.name, "dtype": str(f.type)} for f in schema]

    def count(self, table: str | None = None) -> int:
        return int(self.con.execute(f"SELECT count(*) FROM {_q(table or self.relation)}").fetchone()[0])

    # -- seeding from the previous artifact --------------------------------

    def seed_from(self, previous_path: str, previous_key: str) -> None:
        """Start this file as a copy of the artifact currently being served."""
        from app.services import storage

        # A scheduled run can land on a pod that has never served the file.
        storage.ensure_local(previous_path, fresh=False)
        self.con.execute(
            f"ATTACH '{previous_path}' AS bow_prev (ENCRYPTION_KEY '{previous_key}', READ_ONLY)"
        )
        try:
            self.con.execute("COPY FROM DATABASE bow_prev TO artifact")
        finally:
            self.con.execute("DETACH bow_prev")
        self.con.execute("USE artifact")
        self.adopt_relation()
        self._ensure_hidden_columns()

    def adopt_relation(self) -> None:
        """Give the stored table this relation's name if it had another.

        A custom table can be renamed after its artifact was written. The
        rows are the same rows; only the name the file knows them by is old.
        """
        if self.exists():
            return
        others = [r[0] for r in self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'artifact'"
        ).fetchall() if not r[0].startswith(INTERNAL_PREFIX)]
        if len(others) == 1:
            self.con.execute(f"ALTER TABLE {_q(others[0])} RENAME TO {_q(self.relation)}")

    def window_rows(self, window: int) -> int | None:
        """Rows a window already committed to this file, or None if it has not."""
        if not self.exists(PROGRESS):
            return None
        row = self.con.execute(f"SELECT n FROM {PROGRESS} WHERE w = ?", [window]).fetchone()
        return int(row[0]) if row else None

    def drop_internal(self) -> None:
        for name in (STAGING, PROGRESS):
            self.con.execute(f"DROP TABLE IF EXISTS {_q(name)}")

    def _ensure_hidden_columns(self) -> None:
        """Artifacts written before run lineage existed gain the columns, empty."""
        have = {name for name, _t in self.describe()}
        if RUN_ID not in have:
            self.con.execute(f"ALTER TABLE {_q(self.relation)} ADD COLUMN {RUN_ID} VARCHAR")
        if LOADED_AT not in have:
            self.con.execute(f"ALTER TABLE {_q(self.relation)} ADD COLUMN {LOADED_AT} TIMESTAMP")

    # -- loading -----------------------------------------------------------

    def _hidden_literals(self) -> tuple:
        stamp = self.loaded_at.strftime("%Y-%m-%d %H:%M:%S.%f")
        run = self.run_id.replace("'", "''")
        return f"'{run}'", f"TIMESTAMP '{stamp}'"

    def _hidden_select(self) -> str:
        run_lit, at_lit = self._hidden_literals()
        return f"{run_lit} AS {RUN_ID}, {at_lit} AS {LOADED_AT}"

    def _find_placeholders(self, table: str) -> set:
        """INTEGER columns holding no value at all: typed from an empty batch."""
        candidates = [n for n, t in self.describe(table) if t == "INTEGER" and n not in HIDDEN_COLUMNS]
        if not candidates:
            return set()
        counts = self.con.execute(
            "SELECT " + ", ".join(f"count({_q(c)})" for c in candidates) + f" FROM {_q(table)}"
        ).fetchone()
        return {c.lower() for c, n in zip(candidates, counts) if not n}

    def _unified(self, current: str, incoming: str) -> str:
        """The narrowest type both `current` and `incoming` values fit in."""
        try:
            row = self.con.execute(
                f"DESCRIBE SELECT CAST(NULL AS {current}) AS x UNION ALL SELECT CAST(NULL AS {incoming})"
            ).fetchone()
            return row[1]
        except Exception:
            return "VARCHAR"

    def _reconcile(self, table: str, batch, hidden: bool, strict_columns: bool) -> str:
        """Make `table` able to hold `batch`; returns the SELECT list to insert.

        A batch's types come from the values that happen to be in it, so two
        batches of one query can disagree: a column that was all NULL, a
        decimal that had fewer digits. The table is widened to hold both
        rather than failing the load — or, far worse, coercing silently.
        """
        import pyarrow as pa

        signature = (table, str(batch.schema))
        cached = self._last_schema.get(table)
        if cached and cached[0] == signature:
            return cached[1]

        if table not in self._scanned:
            self._placeholders |= {f"{table}.{c}" for c in self._find_placeholders(table)}
            self._scanned.add(table)

        current = {name.lower(): (name, dtype) for name, dtype in self.describe(table)}
        incoming = self.con.execute("DESCRIBE SELECT * FROM bow_batch").fetchall()
        null_fields = {f.name.lower() for f in batch.schema if pa.types.is_null(f.type)}
        batch_fields = {f.name.lower(): (batch.column(i), f) for i, f in enumerate(batch.schema)}
        value_dependent = False

        exprs = []
        for name, itype in [(r[0], r[1]) for r in incoming]:
            low = name.lower()
            col = _q(name)
            if low not in current:
                if strict_columns:
                    raise SchemaChanged(f"column '{name}' is new")
                new_type = "INTEGER" if low in null_fields else itype
                self.con.execute(f"ALTER TABLE {_q(table)} ADD COLUMN {col} {new_type}")
                if low in null_fields:
                    self._placeholders.add(f"{table}.{low}")
                exprs.append(col)
                continue
            _cname, ctype = current[low]
            marker = f"{table}.{low}"
            if low in null_fields:
                exprs.append(col)
                continue
            if marker in self._placeholders:
                self._placeholders.discard(marker)
                if ctype != itype:
                    self.con.execute(f"ALTER TABLE {_q(table)} ALTER {col} TYPE {itype}")
                exprs.append(col)
                continue
            if ctype == itype:
                exprs.append(col)
                continue
            if _is_nested(ctype) or _is_nested(itype):
                # Objects whose shape changed between batches: keep them all,
                # as JSON text, instead of forcing one shape on the other.
                if _is_nested(ctype):
                    self.con.execute(
                        f"ALTER TABLE {_q(table)} ALTER {col} TYPE VARCHAR "
                        f"USING CAST(to_json({col}) AS VARCHAR)"
                    )
                elif ctype != "VARCHAR":
                    self.con.execute(f"ALTER TABLE {_q(table)} ALTER {col} TYPE VARCHAR")
                exprs.append(f"CAST(to_json({col}) AS VARCHAR) AS {col}" if _is_nested(itype) else col)
                continue
            if self._widen_numeric(table, col, ctype, batch_fields.get(low)):
                # Sized from this batch's values: the next batch with the
                # same schema can carry larger ones, so it is checked again.
                value_dependent = True
                exprs.append(col)
                continue
            unified = self._unified(ctype, itype)
            if unified != ctype:
                self.con.execute(f"ALTER TABLE {_q(table)} ALTER {col} TYPE {unified}")
            exprs.append(col)

        select = ", ".join(exprs)
        if hidden:
            select = f"{select}, {self._hidden_select()}" if select else self._hidden_select()
        if not value_dependent:
            self._last_schema[table] = (signature, select)
        return select

    def _widen_numeric(self, table: str, col: str, ctype: str, field) -> bool:
        """Widen `col` for an integer/decimal/float pair where a decimal is
        involved, sized by the values (see `_wider`). False when the pair is
        not one this decides; the caller then uses DuckDB's own union."""
        import pyarrow as pa

        if field is None:
            return False
        held, incoming = _arrow_numeric(ctype), field[1].type
        if held is None or not (pa.types.is_decimal(held) or pa.types.is_decimal(incoming)):
            return False
        if not (pa.types.is_integer(incoming) or pa.types.is_decimal(incoming)
                or pa.types.is_floating(incoming)):
            return False
        held_digits = incoming_digits = None
        if pa.types.is_integer(held) and pa.types.is_decimal(incoming):
            lo, hi = self.con.execute(f"SELECT min({col}), max({col}) FROM {_q(table)}").fetchone()
            held_digits = max((len(str(abs(v))) for v in (lo, hi) if v is not None), default=0)
        if pa.types.is_integer(incoming):
            incoming_digits = _value_digits(field[0])
        target = _wider(held, incoming, held_digits, incoming_digits)
        if target is None:
            return pa.types.is_decimal(held) and not pa.types.is_floating(incoming)
        self.con.execute(f"ALTER TABLE {_q(table)} ALTER {col} TYPE {_duck_type(target)}")
        if target == pa.float64():
            # The exact values already written are converted too. Rare (it
            # needs more than 38 significant digits, or a float among exact
            # numbers), and worth knowing about when it happens.
            logger.warning("extraction.column_became_approximate",
                           extra={"column": col.strip('"'), "from_type": ctype})
        else:
            logger.info("extraction.column_widened",
                        extra={"column": col.strip('"'), "type": str(target)})
        return True

    def load(self, batches: Iterable, limits: Limits, *, table: str | None = None,
             hidden: bool = True, strict_columns: bool = False, rows_before: int = 0,
             window: int | None = None) -> int:
        """Append every batch to `table` inside one transaction.

        All or nothing: an abort part-way leaves the file exactly as it was,
        which is what lets a chunked load resume from the last finished window.
        `window` records, in that same transaction, that this window of a load
        is in the file — so a crash between the commit and the caller noting
        it can never load the window a second time.
        """
        table = table or self.relation
        read = 0
        self.con.execute("BEGIN")
        try:
            try:
                for raw in batches:
                    limits.check()
                    batch = normalize_table(raw)
                    read += batch.num_rows
                    if rows_before + read > limits.max_rows:
                        raise ExtractionAborted(
                            f"Query returned more than the {limits.max_rows:,}-row limit "
                            f"for a custom table and was aborted."
                        )
                    self.con.register("bow_batch", batch)
                    try:
                        if not self.exists(table):
                            hidden_sql = f", {self._hidden_select()}" if hidden else ""
                            self.con.execute(
                                f"CREATE TABLE {_q(table)} AS SELECT *{hidden_sql} FROM bow_batch"
                            )
                            import pyarrow as pa

                            self._placeholders |= {
                                f"{table}.{f.name.lower()}" for f in batch.schema if pa.types.is_null(f.type)
                            }
                            self._scanned.add(table)
                        elif batch.num_rows:
                            select = self._reconcile(table, batch, hidden, strict_columns)
                            self.con.execute(
                                f"INSERT INTO {_q(table)} BY NAME SELECT {select} FROM bow_batch"
                            )
                    finally:
                        self.con.unregister("bow_batch")
                    if self.size() > limits.max_bytes:
                        raise ExtractionAborted(
                            f"Artifact exceeded the "
                            f"{limits.max_bytes / (1024**3):.1f} GB limit and was aborted."
                        )
            finally:
                # Closing the generator runs the source's cleanup — cancelling
                # a still-running (still-billing) job where the source has one.
                close = getattr(batches, "close", None)
                if close is not None:
                    close()
            if window is not None:
                self.con.execute(f"CREATE TABLE IF NOT EXISTS {PROGRESS} (w INTEGER, n BIGINT)")
                self.con.execute(f"INSERT INTO {PROGRESS} VALUES (?, ?)", [window, read])
            self.con.execute("COMMIT")
        except BaseException:
            try:
                self.con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return read

    # -- folding an increment in -------------------------------------------

    def _require(self, names: Iterable[str], what: str, table: str | None = None) -> list:
        have = {c.lower(): c for c in self.data_columns(table)}
        resolved = []
        for name in names:
            actual = have.get(str(name).lower())
            if actual is None:
                raise incremental.IncrementalConfigError(
                    f"The {what} '{name}' is not a column of the query's result. "
                    f"Available columns: {', '.join(sorted(have.values())) or 'none'}."
                )
            resolved.append(actual)
        return resolved

    def collapse_duplicates(self, keys: Iterable[str], cursor: str, table: str | None = None) -> int:
        """Keep one row per key — the one with the highest cursor."""
        table = table or self.relation
        keys = self._require(keys, "key column", table)
        (cursor,) = self._require([cursor], "cursor column", table)
        order = f"{_q(cursor)} DESC NULLS LAST"
        if table == self.relation:
            order += f", {LOADED_AT} DESC NULLS LAST"
        partition = ", ".join(_q(k) for k in keys)
        before = self.count(table)
        self.con.execute(
            f"DELETE FROM {_q(table)} WHERE rowid IN ("
            f"SELECT rowid FROM (SELECT rowid, row_number() OVER ("
            f"PARTITION BY {partition} ORDER BY {order}, rowid DESC) AS bow_rn FROM {_q(table)}) "
            f"WHERE bow_rn > 1)"
        )
        return before - self.count(table)

    def align_to_staging(self) -> None:
        """Widen the stored table to hold the staged rows, or refuse.

        The stored rows and the new ones must describe the same columns. Rows
        already stored cannot be given a value for a column only the new rows
        have, and new rows cannot be given one for a column the source no
        longer returns — either way the copy would stop matching the source:
        that is a full refresh, not an increment.
        """
        target = {n.lower(): (n, t) for n, t in self.describe(self.relation)}
        staged = self.describe(STAGING)
        staged_names = {n.lower() for n, _t in staged}
        for low, (name, _t) in target.items():
            if name not in HIDDEN_COLUMNS and low not in staged_names:
                raise SchemaChanged(f"column '{name}' is gone")
        empty_target = self._find_placeholders(self.relation)
        # A staged column with no value in it has no type of its own (it was
        # typed as a placeholder); it must not retype the stored column.
        empty_staged = self._find_placeholders(STAGING)
        for name, stype in staged:
            low = name.lower()
            if low not in target:
                raise SchemaChanged(f"column '{name}' is new")
            _n, ttype = target[low]
            if ttype == stype or low in empty_staged:
                continue
            if _is_nested(ttype) or _is_nested(stype):
                raise SchemaChanged(f"column '{name}' changed shape")
            unified = stype if low in empty_target else self._unified(ttype, stype)
            if unified != ttype:
                self.con.execute(f"ALTER TABLE {_q(self.relation)} ALTER {_q(name)} TYPE {unified}")

    def merge(self, keys: Iterable[str], cursor: str) -> tuple:
        """Upsert the staged rows by key. Returns (inserted, updated, collapsed).

        A staged row identical to the stored one is left alone, so re-reading
        the lookback window does not rewrite rows that did not change and
        each row keeps the id of the run that last really changed it.
        """
        keys = self._require(keys, "key column", STAGING)
        collapsed = self.collapse_duplicates(keys, cursor, STAGING)
        self.align_to_staging()
        cols = self.data_columns(STAGING)
        on = " AND ".join(f"t.{_q(k)} IS NOT DISTINCT FROM s.{_q(k)}" for k in keys)
        others = [c for c in cols if c not in keys]
        run_lit, at_lit = self._hidden_literals()

        before = self.count()
        if others:
            left = ", ".join(f"t.{_q(c)}" for c in others)
            right = ", ".join(f"s.{_q(c)}" for c in others)
            sets = ", ".join(f"{_q(c)} = s.{_q(c)}" for c in others)
            changed = int(self.con.execute(
                f"SELECT count(*) FROM {_q(self.relation)} t JOIN {_q(STAGING)} s ON {on} "
                f"WHERE ({left}) IS DISTINCT FROM ({right})"
            ).fetchone()[0])
            self.con.execute(
                f"UPDATE {_q(self.relation)} AS t SET {sets}, {RUN_ID} = {run_lit}, {LOADED_AT} = {at_lit} "
                f"FROM {_q(STAGING)} s WHERE {on} AND ({left}) IS DISTINCT FROM ({right})"
            )
        else:
            changed = 0
        names = ", ".join(_q(c) for c in cols)
        self.con.execute(
            f"INSERT INTO {_q(self.relation)} ({names}, {RUN_ID}, {LOADED_AT}) "
            f"SELECT {', '.join('s.' + _q(c) for c in cols)}, {run_lit}, {at_lit} "
            f"FROM {_q(STAGING)} s WHERE NOT EXISTS ("
            f"SELECT 1 FROM {_q(self.relation)} t WHERE {on})"
        )
        return self.count() - before, changed, collapsed

    def replace_tail(self, cursor: str, lo: Any, replace_nulls: bool) -> tuple:
        """Swap every stored row at/after `lo` for the staged rows.

        Without a key there is nothing to match rows on, so the window is
        replaced whole. That is what makes re-reading it harmless: the same
        window read twice gives the same table, never duplicates.
        Returns (inserted, deleted).
        """
        (cursor,) = self._require([cursor], "cursor column", STAGING)
        self.align_to_staging()
        col = _q(cursor)
        staged_nulls = int(self.con.execute(
            f"SELECT count(*) FROM {_q(STAGING)} WHERE {col} IS NULL"
        ).fetchone()[0])
        before = self.count()
        if lo is not None:
            predicate, params = f"{col} >= ?", [lo]
            if replace_nulls or staged_nulls:
                predicate = f"({predicate}) OR {col} IS NULL"
            self.con.execute(f"DELETE FROM {_q(self.relation)} WHERE {predicate}", params)
        else:
            self.con.execute(f"DELETE FROM {_q(self.relation)}")
        deleted = before - self.count()
        cols = self.data_columns(STAGING)
        names = ", ".join(_q(c) for c in cols)
        run_lit, at_lit = self._hidden_literals()
        self.con.execute(
            f"INSERT INTO {_q(self.relation)} ({names}, {RUN_ID}, {LOADED_AT}) "
            f"SELECT {names}, {run_lit}, {at_lit} FROM {_q(STAGING)}"
        )
        return self.count(STAGING), deleted

    # -- measuring ---------------------------------------------------------

    def cursor_stats(self, cursor: str) -> tuple:
        """(max cursor value, rows with no cursor value)."""
        (cursor,) = self._require([cursor], "cursor column")
        row = self.con.execute(
            f"SELECT max({_q(cursor)}), count(*) - count({_q(cursor)}) FROM {_q(self.relation)}"
        ).fetchone()
        return row[0], int(row[1] or 0)


def schema_fingerprint(columns: list) -> str:
    import hashlib
    import json

    payload = json.dumps([[c.get("name"), c.get("dtype")] for c in columns or []])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def new_part(connection_id: str) -> tuple:
    """(path, key) of a fresh, not-yet-published artifact file."""
    final = artifacts.new_artifact_path(connection_id)
    return final.with_suffix(".tmp"), artifacts.new_artifact_key()


def load_window(source, sql: str, part_path, key: str, relation: str, run_id: str,
                limits: Limits, *, batch_rows: int, rows_before: int = 0,
                window: int | None = None) -> int:
    """Append the rows of one query to the relation in a part file.

    With `window` given, a window this file already holds is not loaded again.
    """
    with ArtifactWriter(part_path, key, relation, run_id) as writer:
        writer.adopt_relation()
        if window is not None:
            done = writer.window_rows(window)
            if done is not None:
                return done
        return writer.load(source.stream_batches(sql, batch_rows), limits, rows_before=rows_before,
                           window=window)


def _check_row_drop(config: incremental.RefreshConfig, previous_rows: int | None, rows: int) -> None:
    pct = config.row_drop_guard_pct
    if not pct or not previous_rows or previous_rows < ROW_DROP_GUARD_MIN_ROWS:
        return
    if rows < previous_rows * (1 - pct / 100):
        drop = 100 * (previous_rows - rows) / previous_rows
        raise PublishCheckFailed(
            f"The refreshed table has {rows:,} rows, down {drop:.0f}% from {previous_rows:,}. "
            f"That is more than the {pct}% this table allows, so the previous data is "
            f"still being served. If the drop is expected, raise or turn off the "
            f"row-drop guard and refresh again."
        )


def finalize(part_path, key: str, relation: str, config: incremental.RefreshConfig, *,
             previous_rows: int | None = None, outcome: LoadOutcome | None = None,
             run_id: str = "", collapse: bool = True) -> LoadOutcome:
    """Measure a finished part file, check it, and give it its final name."""
    part_path = Path(part_path)
    outcome = outcome or LoadOutcome()
    with ArtifactWriter(part_path, key, relation, run_id) as writer:
        writer.adopt_relation()
        if not writer.exists():
            raise ExtractionAborted("The query returned no result to store.")
        if config.incremental:
            if config.mode == incremental.MODE_MERGE and collapse:
                # Windows of a chunked load are disjoint, but a row that moved
                # between them while the load ran is in two of them.
                collapsed = writer.collapse_duplicates(config.unique_key, config.cursor_column)
                outcome.collapsed_duplicates += collapsed
            outcome.max_cursor, outcome.null_cursor_rows = writer.cursor_stats(config.cursor_column)
            kind = incremental.kind_of(outcome.max_cursor)
            if outcome.max_cursor is not None and kind is None:
                raise incremental.IncrementalConfigError(
                    f"Column '{config.cursor_column}' cannot be used as a cursor: it must be "
                    f"a date, timestamp, number or sortable text."
                )
        writer.drop_internal()
        outcome.row_count = writer.count()
        outcome.columns = writer.columns()
        writer.con.execute("CHECKPOINT")
    _check_row_drop(config, previous_rows, outcome.row_count)

    if outcome.collapsed_duplicates:
        outcome.notes.append(
            f"{outcome.collapsed_duplicates:,} rows shared a key with a newer row and "
            f"were collapsed into it. If that is unexpected, the key is not unique."
        )
    if outcome.null_cursor_rows:
        outcome.notes.append(
            f"{outcome.null_cursor_rows:,} rows have no '{config.cursor_column}' value."
        )

    final_path = part_path.with_suffix(".db")
    part_path.replace(final_path)
    artifacts.delete_artifact(str(part_path) + ".wal")
    from app.services import storage

    try:
        # Object storage: every pod serves this extract, not just this one.
        storage.persist(final_path)
    except BaseException:
        # Renamed but never published: nothing will ever reference it.
        artifacts.delete_artifact(str(final_path))
        raise
    outcome.artifact_path = str(final_path)
    outcome.artifact_key = key
    outcome.artifact_bytes = artifacts.artifact_size(str(final_path))
    outcome.schema_fingerprint = schema_fingerprint(outcome.columns)
    return outcome


def apply_increment(source, sql: str, previous_path: str, previous_key: str, relation: str,
                    connection_id: str, run_id: str, config: incremental.RefreshConfig,
                    window: incremental.Window, limits: Limits, *, batch_rows: int,
                    previous_rows: int | None = None, wrapped: bool = False,
                    filter_locally: bool = False) -> LoadOutcome:
    """Copy the served artifact forward and fold one window of rows into it.

    `filter_locally`: the source could not restrict the query to the window,
    so `sql` returned everything it has; the window is taken from that here.
    """
    part_path, key = new_part(connection_id)
    outcome = LoadOutcome()
    try:
        with ArtifactWriter(part_path, key, relation, run_id) as writer:
            writer.seed_from(previous_path, previous_key)
            base_rows = writer.count()
            outcome.rows_read = writer.load(
                source.stream_batches(sql, batch_rows), limits,
                table=STAGING, hidden=False, rows_before=0,
            )
            if not writer.exists(STAGING):
                raise SchemaChanged("the query returned no result shape")
            if filter_locally and window.lo is not None:
                (cursor,) = writer._require([config.cursor_column], "cursor column", STAGING)
                # Rows with no cursor value are kept: they are in no window.
                writer.con.execute(f"DELETE FROM {_q(STAGING)} WHERE {_q(cursor)} < ?", [window.lo])
            writer.con.execute("BEGIN")
            try:
                if config.mode == incremental.MODE_MERGE:
                    inserted, updated, collapsed = writer.merge(config.unique_key, config.cursor_column)
                    outcome.rows_inserted, outcome.rows_updated = inserted, updated
                    outcome.collapsed_duplicates = collapsed
                else:
                    inserted, deleted = writer.replace_tail(
                        config.cursor_column, window.lo, replace_nulls=wrapped,
                    )
                    outcome.rows_inserted, outcome.rows_deleted = inserted, deleted
                writer.con.execute("COMMIT")
            except BaseException:
                try:
                    writer.con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            if base_rows + outcome.rows_inserted - outcome.rows_deleted > limits.max_rows:
                raise ExtractionAborted(
                    f"The table would exceed the {limits.max_rows:,}-row limit "
                    f"for a custom table; the refresh was aborted."
                )
        return finalize(part_path, key, relation, config, previous_rows=previous_rows,
                        outcome=outcome, run_id=run_id, collapse=False)
    except BaseException:
        discard_part(part_path)
        raise


def discard_part(part_path) -> None:
    """Remove an unpublished file and its sidecar."""
    if not part_path:
        return
    artifacts.delete_artifact(str(part_path))


def new_run_id() -> str:
    return str(uuid.uuid4())
