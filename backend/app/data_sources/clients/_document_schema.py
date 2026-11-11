"""Shared helpers for schemaless (document / key-value) sources.

DynamoDB, Cosmos DB and Couchbase have no declared column schema: the catalog
is inferred from a bounded sample of items. These helpers turn a sample into
`TableColumn`s (dotted paths for nested maps, `[]` for arrays of objects, a
type per path that records conflicts), normalize driver values into plain
JSON-ish Python for DataFrames, and implement the single-statement SELECT
guard the non-SQL dialects (PartiQL, Cosmos SQL, SQL++) share.
"""
from __future__ import annotations

import base64
import datetime as _dt
import decimal
import re
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import TableColumn

# Paths deeper than this are summarized as `object` so one deeply nested
# document can't blow up the catalog.
MAX_DEPTH = 4
MAX_COLUMNS = 300


def _scalar_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, (float, decimal.Decimal)):
        # Decimal is how DynamoDB-style drivers carry their single numeric type.
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (bytes, bytearray)):
        return "binary"
    if isinstance(value, (_dt.datetime, _dt.date)):
        return "datetime"
    if isinstance(value, (set, frozenset)):
        return "set"
    return type(value).__name__


def _walk(doc: dict[str, Any], prefix: str, depth: int, out: OrderedDict[str, set]) -> None:
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            if depth >= MAX_DEPTH or not value:
                out.setdefault(path, set()).add("object")
            else:
                _walk(value, path, depth + 1, out)
        elif isinstance(value, list):
            out.setdefault(path, set()).add("array")
            first_obj = next((v for v in value if isinstance(v, dict)), None)
            if first_obj is not None and depth < MAX_DEPTH:
                _walk(first_obj, f"{path}[]", depth + 1, out)
        else:
            out.setdefault(path, set()).add(_scalar_type(value))


def infer_columns(samples: Iterable[dict[str, Any]], key_order: list[str] | None = None) -> list[TableColumn]:
    """Union of attribute paths across `samples` with their observed types.

    Keys listed in `key_order` come first (partition/sort keys, ids). A path
    seen with several types is reported as e.g. `string|integer`; `null` only
    shows when it is the sole type seen.
    """
    paths: OrderedDict[str, set] = OrderedDict()
    for doc in samples:
        if isinstance(doc, dict):
            _walk(doc, "", 0, paths)
    ordered = list(paths.items())
    if key_order:
        rank = {k: i for i, k in enumerate(key_order)}
        ordered.sort(key=lambda kv: rank.get(kv[0], len(rank)))
    cols: list[TableColumn] = []
    for path, types in ordered[:MAX_COLUMNS]:
        real = sorted(t for t in types if t != "null") or ["null"]
        if set(real) == {"integer", "number"}:
            real = ["number"]  # JSON has one numeric type; 10 and 10.5 are both numbers
        cols.append(TableColumn(name=path, dtype="|".join(real)))
    return cols


def to_plain(value: Any) -> Any:
    """Driver values → JSON-friendly Python (Decimal → int/float, sets →
    sorted lists, bytes → base64, datetimes → ISO strings)."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        items = [to_plain(v) for v in value]
        try:
            return sorted(items)
        except TypeError:
            return items
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return value


def rows_to_frame(rows: list[dict[str, Any]], key_order: list[str] | None = None) -> pd.DataFrame:
    """Items → DataFrame with top-level attributes as columns (nested maps and
    lists stay as Python values in the cell), key attributes first."""
    plain = [to_plain(r) for r in rows]
    if not plain:
        return pd.DataFrame()
    df = pd.DataFrame(plain)
    if key_order:
        first = [k for k in key_order if k in df.columns]
        df = df[first + [c for c in df.columns if c not in first]]
    return df


# ----------------------------------------------------------------- guard

_STRINGS_AND_COMMENTS = re.compile(
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|--[^\n]*|/\*.*?\*/",
    re.DOTALL,
)


class ReadOnlyQueryError(ValueError):
    """The statement is not a single read."""


def ensure_single_select(statement: str, dialect: str, extra_read_verbs: Iterable[str] = ()) -> str:
    """Allow exactly one SELECT-family statement; refuse writes / DDL.

    Works on the text with string literals, quoted identifiers and comments
    removed, so a quoted `'delete'` value or a `"Update"` table name is fine,
    while `SELECT 1; DELETE ...` or `UPDATE ...` is refused.
    Returns the statement with any trailing semicolon stripped.
    """
    text = (statement or "").strip()
    if not text:
        raise ReadOnlyQueryError(f"Empty {dialect} statement.")
    bare = _STRINGS_AND_COMMENTS.sub(" ", text).strip()
    body = bare.rstrip().rstrip(";")
    if ";" in body:
        raise ReadOnlyQueryError(f"Only a single {dialect} statement is allowed.")
    verb = body.lstrip("( \n\t").split(None, 1)[0].upper() if body.strip("( \n\t") else ""
    allowed = {"SELECT", *(v.upper() for v in extra_read_verbs)}
    if verb not in allowed:
        raise ReadOnlyQueryError(
            f"Only read queries are allowed on this connection ({', '.join(sorted(allowed))}); got '{verb or '(empty)'}'."
        )
    return text.rstrip().rstrip(";").rstrip()
