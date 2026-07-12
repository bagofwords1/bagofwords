"""Chunk-by-chunk spill writers: consume DataFrame chunks or Arrow batches into
budget-enforced Parquet files, plus the cell/column normalizers shared by every
streaming source and the cooperative-cancellation hook."""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import uuid
from contextlib import suppress
from decimal import Decimal
from pathlib import Path

import pandas as pd

from .config import StreamConfig
from .errors import QueryAbandonedError, ResultTooLargeError
from .frame import (
    _EMPTY_ROW_METADATA_KEY,
    _EMPTY_ROW_METADATA_VALUE,
    _EMPTY_ROW_SENTINEL_PREFIX,
)

logger = logging.getLogger(__name__)


# --- cooperative cancellation ------------------------------------------------
# The code-exec wrapper runs each query on an abandonable daemon thread; when
# it times out, nothing used to stop the underlying stream from running to its
# full row/byte budget against the source. The wrapper registers its
# abandonment Event for the thread; the chunk consumers poll it between chunks
# and abort (with the normal partial-file cleanup) once it fires.
_thread_cancel = threading.local()


def set_cancel_event(event) -> None:
    """Register a threading.Event for the CURRENT thread's lazy streams."""
    _thread_cancel.event = event


def _cancelled() -> bool:
    e = getattr(_thread_cancel, "event", None)
    return bool(e is not None and e.is_set())


def arrow_safe_cell(v):
    """Coerce one row-dict value for the columnar (Parquet) spill. Shared by
    every row-dict lazy override (Salesforce, NetSuite, Spark, Mongo).

    dict/list → JSON string (schemaless nesting can't be a stable Arrow type);
    scalars pyarrow types natively — str/int/float/bool/datetime/date/time/
    bytes/Decimal — pass through unchanged so timestamps stay timestamps and
    exact financial values stay exact; anything else —
    driver-specific scalars like bson.Timestamp/Regex/Code — becomes str,
    because pa.Table.from_pandas raises ArrowInvalid on unknown Python types
    and would kill the whole stream. Eager paths are unaffected (pandas keeps
    raw objects)."""
    import json

    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if v is None or isinstance(v, (str, int, float, bool, bytes, _dt.datetime, _dt.date, _dt.time)):
        return v
    if isinstance(v, Decimal):
        return v
    return str(v)


def _close_quietly(it) -> None:
    """Explicitly close a generator so any `with connect_cm()` block inside it
    releases its connection *now*, not whenever refcount GC gets around to the
    suspended frame after an exception unwinds."""
    close = getattr(it, "close", None)
    if close is not None:
        with suppress(Exception):
            close()


def _widen_null_columns(table):
    """Keep unknown all-NULL fields as Arrow null.

    Guessing float64 corrupted later booleans into 1.0. The consumer already
    rolls a new part when a later concrete type cannot cast to the first part;
    DuckDB's union-by-name then promotes null + concrete to the concrete type.
    """
    return table


def _cast_chunk_to_schema(table, schema):
    """Reconcile a later chunk's inferred schema with the writer's. Chunks are
    typed independently, so a nullable numeric column that happens to be
    all-NULL (or all-int) in one 50k-row chunk infers a different Arrow dtype
    and pq.ParquetWriter.write_table would abort the whole stream. SAFE casts
    only: a lossy fallback (cast(safe=False)) would silently floor a float
    chunk into an int64-locked file schema (3.7 → 3) — different numbers than
    the eager path. On failure raise a clear error naming the offending
    column; the chunk consumer rolls a new part file and the read side's
    union_by_name promotes to the common type (int64 + double → double)."""
    if table.schema.equals(schema, check_metadata=False):
        return table
    try:
        return table.cast(schema)
    except Exception as exc:
        if table.schema.names != schema.names:
            detail = (
                f"chunk columns {table.schema.names} do not match "
                f"file columns {schema.names}"
            )
        else:
            detail = "schemas differ"
            for field in schema:
                col = table.column(field.name)
                try:
                    col.cast(field.type)
                except Exception:
                    detail = (
                        f"column '{field.name}' is {col.type} in this chunk "
                        f"but {field.type} in the file"
                    )
                    break
        raise ValueError(
            f"Streamed result chunks have irreconcilable schemas: {detail}. "
            "Cast the column to one type in the query (e.g. CAST(col AS DOUBLE))."
        ) from exc


def _dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename duplicate column names (id, id → id, id_1): pyarrow refuses to
    convert a frame with duplicates, which would kill every streamed lazy query
    whose SELECT list repeats a name (e.g. `SELECT *` over a join) — the eager
    path returns those fine. The rename matches DuckDB's own dedup convention.
    Chunks of one stream share a fixed column list, so renames are stable."""
    cols = [str(c) for c in df.columns]
    if len(set(cols)) == len(cols):
        return df
    seen: dict = {}
    renamed = []
    for c in cols:
        n = seen.get(c, 0)
        seen[c] = n + 1
        renamed.append(c if n == 0 else f"{c}_{n}")
    df = df.copy(deep=False)
    df.columns = renamed
    return df


def _jsonify_nested_cells(df: pd.DataFrame) -> pd.DataFrame:
    """JSON-encode dict/list cells so the Parquet write can't fail on nested
    values (pyarrow raises ArrowInvalid on dict cells — e.g. ADX dynamic
    columns, Mongo sub-documents). Clients with bespoke streaming overrides do
    this themselves; this covers the generic materialize-then-spill default.
    Returns the input unchanged when there is nothing to encode."""
    import json

    out = None
    for col in df.columns:
        series = df[col]
        if series.dtype != object:
            continue
        if not series.map(lambda v: isinstance(v, (dict, list))).any():
            continue
        if out is None:
            out = df.copy(deep=False)
        out[col] = series.map(
            lambda v: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
        )
    return df if out is None else out


def _consume_chunks_to_parquet(chunks, path: Path, config: StreamConfig, columns=None) -> list:
    """Write an iterable of DataFrame chunks to Parquet, enforcing the row/byte
    cap and aborting (with cleanup) if exceeded. Shared by the DBAPI streamers
    (see streamers.py). `columns`, when known up front, keeps the real schema in
    the Parquet even if the iterable yields no chunks at all.

    Returns the list of Parquet files written — usually one. A ParquetWriter's
    schema is frozen at the first chunk, but schemaless sources (e.g. Mongo)
    can grow or lose top-level columns between chunks; when the column *set*
    changes we roll a new part file and the reader unions parts by name
    (missing columns become NULL, matching eager pandas behavior)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: pq.ParquetWriter | None = None
    schema = None
    paths = [path]
    rows = 0
    byte_estimate = 0
    empty_row_sentinel = f"{_EMPTY_ROW_SENTINEL_PREFIX}{uuid.uuid4().hex}"

    def start_writer(table):
        nonlocal writer, schema
        table = _widen_null_columns(table)
        schema = table.schema
        writer = pq.ParquetWriter(str(paths[-1]), schema)
        config.storage.restrict_file(paths[-1])
        return table

    def roll_part(table):
        nonlocal writer
        writer.close()
        writer = None
        paths.append(path.with_name(f"{path.stem}_part{len(paths)}.parquet"))
        return start_writer(table)

    try:
        config.check_capacity(full=True)
        for chunk in chunks:
            if _cancelled():
                raise QueryAbandonedError()
            config.check_capacity()
            chunk = _jsonify_nested_cells(_dedupe_columns(chunk))
            rows += len(chunk)
            byte_estimate += int(chunk.memory_usage(deep=True).sum())
            if rows > config.max_rows or byte_estimate > config.max_bytes:
                raise ResultTooLargeError(
                    rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
                )
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if table.num_columns == 0:
                # Parquet cannot encode N rows with zero fields. Preserve those
                # real rows with an internal marker; LazyFrame hides it from
                # public columns/materialization after union-by-name.
                field = pa.field(
                    empty_row_sentinel,
                    pa.bool_(),
                    metadata={_EMPTY_ROW_METADATA_KEY: _EMPTY_ROW_METADATA_VALUE},
                )
                table = pa.Table.from_arrays(
                    [pa.array([True] * len(chunk), type=pa.bool_())],
                    schema=pa.schema([field]),
                )
            if writer is None:
                table = start_writer(table)
            elif set(table.schema.names) != set(schema.names):
                table = roll_part(table)
            else:
                if table.schema.names != schema.names:
                    table = table.select(schema.names)  # same columns, drifted order
                try:
                    table = _cast_chunk_to_schema(table, schema)
                except ValueError:
                    # Irreconcilable types — e.g. a sparse text column whose
                    # first 50k rows were all NULL locked the file schema to
                    # double, and now real strings arrive. Roll a new part;
                    # the read side's union_by_name promotes to a common type
                    # (double + varchar → varchar), matching eager pandas.
                    table = roll_part(table)
            writer.write_table(table)
            config.check_capacity(full=True)
        if writer is None:
            # Empty result with no chunk yielded: write a 0-row Parquet that
            # still carries the real column names (when known) so downstream
            # `.sql("SELECT col ...")` keeps working.
            empty = pd.DataFrame(columns=list(columns)) if columns else pd.DataFrame()
            table = _widen_null_columns(pa.Table.from_pandas(empty, preserve_index=False))
            pq.write_table(table, str(path))
            config.storage.restrict_file(path)
            config.check_capacity(full=True)
    except BaseException:
        if writer is not None:
            with suppress(Exception):
                writer.close()
            writer = None
        config.storage.release(paths)  # files + private q_ dir
        raise
    finally:
        if writer is not None:
            writer.close()
        _close_quietly(chunks)  # release the source connection promptly
    try:
        # Parquet footer bytes are appended by writer.close(), after the
        # per-batch checks above. Enforce the aggregate cap once more against
        # the completed file(s).
        config.check_capacity(full=True)
    except BaseException:
        config.storage.release(paths)
        raise
    return paths


def _consume_arrow_to_parquet(arrow_iter, path: Path, config: StreamConfig) -> Path:
    """Write an iterable of pyarrow Tables/RecordBatches to one Parquet file,
    enforcing the cap. For clients with native Arrow streams (BigQuery, ClickHouse)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    writer: pq.ParquetWriter | None = None
    schema = None
    rows = 0
    byte_estimate = 0
    try:
        config.check_capacity(full=True)
        for obj in arrow_iter:
            if _cancelled():
                raise QueryAbandonedError()
            config.check_capacity()
            table = pa.Table.from_batches([obj]) if isinstance(obj, pa.RecordBatch) else obj
            if table.num_rows == 0 and writer is not None:
                continue
            rows += table.num_rows
            byte_estimate += table.nbytes
            if rows > config.max_rows or byte_estimate > config.max_bytes:
                raise ResultTooLargeError(
                    rows=rows, byte_estimate=byte_estimate, limit_desc=config.limit_desc()
                )
            if writer is None:
                table = _widen_null_columns(table)
                schema = table.schema
                writer = pq.ParquetWriter(str(path), schema)
                config.storage.restrict_file(path)
            else:
                table = _cast_chunk_to_schema(table, schema)
            writer.write_table(table)
            config.check_capacity(full=True)
        if writer is None:
            pd.DataFrame().to_parquet(path, index=False)
            config.storage.restrict_file(path)
            config.check_capacity(full=True)
    except BaseException:
        if writer is not None:
            with suppress(Exception):
                writer.close()
            writer = None
        config.storage.release([path])  # file + private q_ dir
        raise
    finally:
        if writer is not None:
            writer.close()
        _close_quietly(arrow_iter)  # release the source stream promptly
    try:
        config.check_capacity(full=True)  # includes footer bytes from close()
    except BaseException:
        config.storage.release([path])
        raise
    return path
