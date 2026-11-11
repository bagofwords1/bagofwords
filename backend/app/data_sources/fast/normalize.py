"""One shape for every source's rows before they reach the artifact.

Sources hand over Arrow batches, but what is *in* them varies with the driver:
unnamed and duplicate columns, values Arrow cannot type (UUIDs, a JSON column
that is an object in one row and a list in the next), decimals wider than
DuckDB can store. Each of those used to be handled — or not — inside whichever
source first met it. Doing it once here means a relation looks the same
whatever connector produced it, which is also the contract anything built on
top of the artifacts (a transform over several of them) is written against.

Type drift *between* batches is not handled here: a batch cannot know what
came before it. That is reconciled against the stored table by the loader.
"""

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal

logger = logging.getLogger(__name__)

# DuckDB's widest DECIMAL is 38 digits. Arrow infers decimal256 past that and
# the mismatch surfaces as an opaque failure at register() time.
DUCKDB_MAX_DECIMAL_PRECISION = 38

# Columns the loader adds to every row. A source column may not use the prefix.
RESERVED_PREFIX = "_bow_"


def clean_names(names: Sequence) -> list[str]:
    """Column names a DuckDB table can actually hold, in the same order.

    A result set is allowed things a table is not: an expression with no name
    (`SELECT COUNT(*)` on SQL Server), the same name twice (`a.id, b.id`), two
    names differing only in case. Building a table straight from those either
    fails or — worse — keeps one column and silently drops the other.
    """
    out: list[str] = []
    seen = set()
    for i, raw in enumerate(names):
        name = str(raw).strip() if raw is not None else ""
        if not name:
            name = f"column_{i + 1}"
        if name.lower().startswith(RESERVED_PREFIX):
            name = f"src{name}"
        candidate, n = name, 1
        while candidate.lower() in seen:
            candidate = f"{name}_{n}"
            n += 1
        seen.add(candidate.lower())
        out.append(candidate)
    return out


def _jsonable(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=_jsonable, ensure_ascii=False)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def column_array(values: list, declared=None, name: str = ""):
    """One column of Python values as an Arrow array that will always build.

    Objects (a JSON column) become JSON text: their shape differs row to row,
    so a typed struct inferred from one batch would not accept the next. Any
    column Arrow still cannot type — mixed kinds, driver-specific objects — is
    stored as text rather than failing the whole extraction over one column.

    ``declared`` is the column's `sources.Declared` decimal type, if the
    driver reported one: numbers are built by `sources._array`, which gives
    every batch that declared type and keeps integer/decimal mixes exact.
    """
    import pyarrow as pa

    probe = next((v for v in values if v is not None), None)
    if isinstance(probe, dict) or (
        isinstance(probe, (list, tuple)) and any(isinstance(x, (dict, list, tuple)) for x in probe)
    ):
        return pa.array([_as_text(v) for v in values], type=pa.string())
    if isinstance(probe, uuid.UUID):
        return pa.array([None if v is None else str(v) for v in values], type=pa.string())
    if isinstance(probe, memoryview):
        values = [None if v is None else bytes(v) for v in values]
    try:
        from app.data_sources.fast.sources import _array

        arr = _array(name, values, declared)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, OverflowError, TypeError, ValueError):
        return pa.array([_as_text(v) for v in values], type=pa.string())
    if pa.types.is_decimal(arr.type) and arr.type.precision > DUCKDB_MAX_DECIMAL_PRECISION:
        # No representable decimal form in DuckDB; exactness is traded for
        # being able to store the value at all.
        return arr.cast(pa.float64())
    return arr


def rows_to_arrow(names: Sequence, rows: Sequence[Sequence], decimals=None):
    """Driver rows (tuples) as an Arrow table with clean, unique column names.

    Built by position, never through a dict keyed on the name: two columns
    with the same name are both kept. ``decimals`` (see
    `sources.declared_decimals`) is aligned with ``names`` by position.
    """
    import pyarrow as pa

    clean = clean_names(names)
    declared = (list(decimals or ()) + [None] * len(clean))[:len(clean)]
    arrays = [column_array([r[i] for r in rows], declared[i], clean[i])
              for i in range(len(clean))]
    return pa.Table.from_arrays(arrays, names=clean)


def normalize_table(table):
    """An Arrow batch from any source, made safe to store.

    Sources that already speak Arrow keep their types; only what a DuckDB
    table cannot hold is changed.
    """
    import pyarrow as pa

    names = clean_names(table.schema.names)
    if names != list(table.schema.names):
        table = table.rename_columns(names)
    for i, field in enumerate(table.schema):
        if pa.types.is_decimal(field.type) and field.type.precision > DUCKDB_MAX_DECIMAL_PRECISION:
            table = table.set_column(i, pa.field(field.name, pa.float64()),
                                     table.column(i).cast(pa.float64()))
            logger.info("extraction.decimal_widened", extra={"column": field.name})
    return table
