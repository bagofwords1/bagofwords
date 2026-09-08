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


def dataframe_to_arrow(df: pd.DataFrame) -> Tuple[bytes, Dict[str, Any]]:
    """Serialize `df` to Arrow IPC stream bytes plus a small meta dict.

    Tries a faithful conversion first. Columns Arrow rejects are converted
    to strings (None preserved) and listed in ``meta["stringified"]`` so the
    parent can log the loss. Column labels are made unique strings for the
    wire; the originals travel in ``meta["columns"]`` and are restored.
    """
    import pyarrow as pa

    meta: Dict[str, Any] = {"stringified": [], "columns": None}
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
        except Exception:
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
    columns = (meta or {}).get("columns")
    if columns is not None and len(columns) == len(df.columns):
        df.columns = columns
    return df


def json_safe(value: Any) -> Any:
    """Round-trip `value` through JSON (default=str) so it is wire-safe."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))
