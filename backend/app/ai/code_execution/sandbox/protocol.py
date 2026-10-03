"""Wire format between the trusted parent and the untrusted child.

Framing: ``>II`` (header length, payload length) + JSON header + raw payload.

Trust direction matters for the payload encoding:

* parent → child payloads are **pickle**. The parent is trusted, the child is
  the one being sandboxed; unpickling data from the trusted side is fine.
* child → parent payloads are **never pickle**. Unpickling would hand the
  child arbitrary code execution in the API process, which is the exact
  thing the sandbox exists to prevent. DataFrames cross as Arrow IPC bytes,
  everything else as JSON in the header.

Both sides import this module, so it must stay light (stdlib + pandas/pyarrow).
"""
from __future__ import annotations

import json
import numbers
import struct
from typing import Any, BinaryIO, Dict, Optional, Tuple

import pandas as pd

_FRAME = struct.Struct(">II")
# Hard caps on a single frame. The header is JSON the parent parses eagerly
# (stdout, error text, rpc arguments), so it gets a tight cap; the payload is
# Arrow/pickle bytes that are only decoded when expected.
MAX_HEADER_BYTES = 32 * 1024 * 1024
MAX_FRAME_BYTES = 2 * 1024 * 1024 * 1024

# Sentinel used by the child's client proxy to signal a keyword-only call.
NO_QUERY = "__bow_no_query__"


class ProtocolError(RuntimeError):
    pass


def write_message(stream: BinaryIO, header: Dict[str, Any], payload: bytes = b"") -> None:
    body = json.dumps(header, default=str, ensure_ascii=False).encode("utf-8")
    stream.write(_FRAME.pack(len(body), len(payload)))
    stream.write(body)
    if payload:
        stream.write(payload)
    stream.flush()


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("sandbox pipe closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> Tuple[Dict[str, Any], bytes]:
    raw = _read_exact(stream, _FRAME.size)
    hlen, plen = _FRAME.unpack(raw)
    if hlen > MAX_HEADER_BYTES or plen > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame too large: header={hlen} payload={plen}")
    header = json.loads(_read_exact(stream, hlen).decode("utf-8"))
    if not isinstance(header, dict):
        raise ProtocolError("frame header is not an object")
    payload = _read_exact(stream, plen) if plen else b""
    return header, payload


# ---------------------------------------------------------------------------
# DataFrame transport (child → parent): Arrow IPC, with a lossy fallback for
# columns Arrow cannot type (mixed objects, nested python objects, ...).
# ---------------------------------------------------------------------------

def _stringify_column(series: pd.Series) -> pd.Series:
    def conv(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v if isinstance(v, str) else str(v)

    return series.astype(object).map(conv)


_INT64_MIN, _INT64_MAX = -(1 << 63), (1 << 63) - 1
_UINT64_MAX = (1 << 64) - 1


def _is_null_scalar(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        res = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return res is True or (isinstance(res, bool) and res)


def _as_nullable_integers(series: pd.Series) -> pd.Series | None:
    """Re-type an object column of Python ints as a nullable integer column.

    Connectors that return counters wider than int64 (Brocade port
    statistics, for one) build object-dtype frames so pandas does not turn
    the values into lossy floats. Arrow's type inference overflows on such a
    column before it ever considers uint64, which used to send it down the
    text fallback and hand the parent strings. A nullable UInt64/Int64
    extension column crosses Arrow losslessly and pandas reconstructs the
    same dtype on the other side. Returns None when the column is not
    integers (bools excluded) that fit one of those two types.
    """
    values = series.tolist()
    ints = []
    for v in values:
        if _is_null_scalar(v):
            continue
        if isinstance(v, bool) or not isinstance(v, numbers.Integral):
            return None
        ints.append(int(v))
    if not ints:
        return None
    lo, hi = min(ints), max(ints)
    if lo >= 0 and hi <= _UINT64_MAX:
        dtype = "UInt64"
    elif lo >= _INT64_MIN and hi <= _INT64_MAX:
        dtype = "Int64"
    else:
        return None
    data = [None if _is_null_scalar(v) else int(v) for v in values]
    return pd.Series(pd.array(data, dtype=dtype), index=series.index)


def dataframe_to_arrow(df: pd.DataFrame) -> Tuple[bytes, Dict[str, Any]]:
    """Serialize `df` to Arrow IPC stream bytes plus a small meta dict.

    Tries a faithful conversion first. Columns Arrow rejects are converted
    to strings (None preserved) and listed in ``meta["stringified"]`` so the
    parent can log the loss. Column labels are made unique strings for the
    wire; the originals travel in ``meta["columns"]`` and are restored.
    """
    import pyarrow as pa

    # Positions of object-dtype columns. Arrow types them (bool, int, str),
    # which is right on the wire but would hand the parent numpy scalars
    # where the child's code (or a connector) deliberately kept Python
    # objects; the parent restores object dtype so the frame reads the same
    # as it did when the code ran in-process.
    meta: Dict[str, Any] = {
        "stringified": [],
        "columns": None,
        "object_columns": [i for i, dt in enumerate(df.dtypes) if pd.api.types.is_object_dtype(dt)],
    }
    try:
        table = pa.Table.from_pandas(df, preserve_index=None)
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return sink.getvalue().to_pybytes(), meta
    except Exception:
        pass

    # Fallback: unique string column names, per-column conversion.
    original_columns = [c for c in df.columns]
    wire = pd.DataFrame(index=df.index)
    for i, col in enumerate(original_columns):
        name = f"c{i}"
        series = df.iloc[:, i]
        try:
            pa.array(series)
            wire[name] = series
            continue
        except Exception:
            pass
        retyped = _as_nullable_integers(series) if series.dtype == object else None
        if retyped is not None:
            wire[name] = retyped
            continue
        wire[name] = _stringify_column(series)
        meta["stringified"].append(str(col))
    meta["columns"] = [c if isinstance(c, (str, int, float, bool)) or c is None else str(c) for c in original_columns]
    try:
        table = pa.Table.from_pandas(wire, preserve_index=None)
    except Exception:
        # Index itself untypeable — drop it.
        table = pa.Table.from_pandas(wire.reset_index(drop=True), preserve_index=False)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes(), meta


def arrow_to_dataframe(payload: bytes, meta: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    import pyarrow as pa

    with pa.ipc.open_stream(pa.BufferReader(payload)) as reader:
        table = reader.read_all()
    df = table.to_pandas()
    meta = meta or {}
    columns = meta.get("columns")
    if columns is not None and len(columns) == len(df.columns):
        df.columns = columns
    for i in meta.get("object_columns") or []:
        if not (isinstance(i, int) and 0 <= i < len(df.columns)):
            continue
        restored = df.iloc[:, i].astype(object)
        restored[restored.isna()] = None
        df.isetitem(i, restored)
    return df


def json_safe(value: Any) -> Any:
    """Round-trip `value` through JSON (default=str) so it is wire-safe."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))
