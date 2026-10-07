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

import datetime
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


class ResultTooLargeError(ProtocolError):
    """A returned DataFrame exceeds what the parent agreed to decode."""


def encode_frame_header(header: Dict[str, Any], payload: bytes = b"") -> bytes:
    """Frame prefix plus JSON header; the payload follows it on the wire."""
    body = json.dumps(header, default=str, ensure_ascii=False).encode("utf-8")
    return _FRAME.pack(len(body), len(payload)) + body


def write_message(stream: BinaryIO, header: Dict[str, Any], payload: bytes = b"") -> None:
    stream.write(encode_frame_header(header, payload))
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


def _decodes(table) -> bool:
    """Whether the parent will be able to turn `table` back into pandas.

    Some frames convert to Arrow but not back: a categorical of Intervals
    (`pd.cut` / `pd.qcut` buckets, or an index built from one) writes pandas
    metadata that `to_pandas` cannot read. That failure depends on the
    schema, not the data, so decoding the first row is enough to catch it.
    """
    try:
        table.slice(0, 1).to_pandas()
        return True
    except Exception:
        return False


def _ipc_bytes(table) -> bytes:
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


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
        if _decodes(table):
            return _ipc_bytes(table), meta
    except Exception:
        pass

    # Fallback: unique string column names, per-column conversion.
    original_columns = [c for c in df.columns]
    wire = pd.DataFrame(index=df.index)
    for i, col in enumerate(original_columns):
        name = f"c{i}"
        series = df.iloc[:, i]
        try:
            if _decodes(pa.Table.from_pandas(pd.DataFrame({"c": series}), preserve_index=False)):
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
    table = None
    try:
        table = pa.Table.from_pandas(wire, preserve_index=None)
    except Exception:
        pass
    if table is None or not _decodes(table):
        # Index untypeable or undecodable (e.g. a CategoricalIndex of
        # Intervals from a groupby on a pd.cut column): send its labels as
        # text, and drop it only if even that fails.
        try:
            table = pa.Table.from_pandas(wire.set_axis(wire.index.map(str), axis=0), preserve_index=None)
        except Exception:
            table = None
        if table is None or not _decodes(table):
            table = pa.Table.from_pandas(wire.reset_index(drop=True), preserve_index=False)
    return _ipc_bytes(table), meta


# ---------------------------------------------------------------------------
# Pre-decode check of an Arrow IPC stream (child → parent).
#
# Arrow can describe a huge table in a few bytes: a null-typed column of N
# rows has no buffers at all, and IPC body compression expands on read.
# `read_all()` decompresses and `to_pandas()` materializes, both in the API
# worker with no memory limit, so the stream is checked first, from its
# flatbuffer message metadata only: no compression (our child never writes
# it), only schema / dictionary / record-batch messages, and the total
# number of values across every array (FieldNode lengths, nested included)
# under a cap. Format: https://arrow.apache.org/docs/format/Columnar.html
# ---------------------------------------------------------------------------

_MSG_SCHEMA, _MSG_DICTIONARY_BATCH, _MSG_RECORD_BATCH = 1, 2, 3
# The walk below is a Python loop over messages and array nodes. Honest
# results have one node per column (plus nested children) and a handful of
# messages; these caps keep a payload of empty nodes/messages from turning
# the check itself into a CPU sink.
_MAX_IPC_MESSAGES = 10_000
_MAX_IPC_NODES = 100_000


def _fb_read(fmt: str, buf: bytes, pos: int):
    size = struct.calcsize(fmt)
    if pos < 0 or pos + size > len(buf):
        raise ProtocolError("malformed Arrow IPC metadata")
    return struct.unpack_from(fmt, buf, pos)[0]


def _fb_field(buf: bytes, table: int, index: int) -> Optional[int]:
    """Absolute position of field `index` of the flatbuffer table at
    `table`, or None when the field is absent (default value)."""
    vtable = table - _fb_read("<i", buf, table)
    vtable_size = _fb_read("<H", buf, vtable)
    entry = 4 + 2 * index
    if entry + 2 > vtable_size:
        return None
    offset = _fb_read("<H", buf, vtable + entry)
    return table + offset if offset else None


def _fb_deref(buf: bytes, pos: int) -> int:
    return pos + _fb_read("<I", buf, pos)


def check_arrow_stream(payload: bytes, max_cells: int) -> None:
    """Raise unless `payload` is an uncompressed Arrow IPC stream holding at
    most `max_cells` values. Reads metadata only; decodes nothing."""
    pos, cells, messages, nodes_seen = 0, 0, 0, 0
    while pos < len(payload):
        messages += 1
        if messages > _MAX_IPC_MESSAGES:
            raise ProtocolError("too many Arrow IPC messages")
        meta_len = _fb_read("<i", payload, pos)
        pos += 4
        if meta_len == -1:  # continuation marker, then the real length
            meta_len = _fb_read("<i", payload, pos)
            pos += 4
        if meta_len == 0:  # end of stream
            return
        if meta_len < 0:
            raise ProtocolError("malformed Arrow IPC stream")
        meta = pos
        message = _fb_deref(payload, meta)
        header_type_at = _fb_field(payload, message, 1)
        header_type = _fb_read("<B", payload, header_type_at) if header_type_at else 0
        body_len_at = _fb_field(payload, message, 3)
        body_len = _fb_read("<q", payload, body_len_at) if body_len_at else 0
        if header_type in (_MSG_RECORD_BATCH, _MSG_DICTIONARY_BATCH):
            header_at = _fb_field(payload, message, 2)
            if header_at is None:
                raise ProtocolError("malformed Arrow IPC metadata")
            batch = _fb_deref(payload, header_at)
            if header_type == _MSG_DICTIONARY_BATCH:
                data_at = _fb_field(payload, batch, 1)
                if data_at is None:
                    raise ProtocolError("malformed Arrow IPC metadata")
                batch = _fb_deref(payload, data_at)
            if _fb_field(payload, batch, 3) is not None:
                raise ProtocolError("compressed Arrow IPC is not accepted")
            nodes_at = _fb_field(payload, batch, 1)
            if nodes_at is not None:
                nodes = _fb_deref(payload, nodes_at)
                count = _fb_read("<I", payload, nodes)
                nodes_seen += count
                if nodes_seen > _MAX_IPC_NODES:
                    raise ProtocolError("too many Arrow arrays")
                for i in range(count):
                    cells += _fb_read("<q", payload, nodes + 4 + 16 * i)
                    if cells > max_cells:
                        raise ResultTooLargeError(
                            f"it holds more than {max_cells:,} values (rows x columns)"
                        )
        elif header_type != _MSG_SCHEMA:
            raise ProtocolError(f"unexpected Arrow IPC message type {header_type}")
        if body_len < 0:
            raise ProtocolError("malformed Arrow IPC metadata")
        pos = meta + meta_len + body_len
    raise ProtocolError("truncated Arrow IPC stream")


def arrow_to_dataframe(payload: bytes, meta: Optional[Dict[str, Any]] = None, *,
                       max_cells: Optional[int] = None) -> pd.DataFrame:
    import pyarrow as pa

    if max_cells is not None:
        check_arrow_stream(payload, max_cells)
    with pa.ipc.open_stream(pa.BufferReader(payload)) as reader:
        table = reader.read_all()
    df = table.to_pandas()
    meta = meta or {}
    columns = meta.get("columns")
    if columns is not None and len(columns) == len(df.columns):
        df.columns = columns
    # `meta` comes from the child: restore each valid column once, however
    # many times (or in whatever shape) the list names it. Each restore
    # copies a whole column, so repeats would let the child pin the parent.
    object_columns = meta.get("object_columns")
    if not isinstance(object_columns, list):
        object_columns = []
    ncols = len(df.columns)
    for i in sorted({i for i in object_columns if type(i) is int and 0 <= i < ncols}):
        restored = df.iloc[:, i].astype(object)
        restored[restored.isna()] = None
        df.isetitem(i, restored)
    return df


def _json_default(value: Any) -> Any:
    # Query arguments cross child → parent as JSON (never pickle). Map the
    # non-JSON types generated code commonly passes as SQL params to what
    # render_sql_with_params produces from the originals: a set renders as
    # an IN list and a datetime as its isoformat. Anything else stays text.
    if isinstance(value, set):
        return list(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def json_safe(value: Any) -> Any:
    """Round-trip `value` through JSON so it is wire-safe."""
    return json.loads(json.dumps(value, default=_json_default, ensure_ascii=False))
