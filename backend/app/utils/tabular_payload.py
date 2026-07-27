"""Recognize a table inside an arbitrary tool payload.

Tool providers — MCP servers, custom REST APIs — rarely return a bare array of
records. The rows almost always arrive wrapped in an envelope carrying
pagination and type metadata::

    {"type": "list", "data": [ ...150 contacts... ], "pages": {"next": "..."}}

Matching only on a top-level list meant those payloads were classified as
opaque JSON: the caller saved a .json blob instead of a CSV, and everything
downstream (previews, generated code) was left guessing at the shape. These
helpers find the row list one or two levels down so the caller can materialize
the table it actually received.
"""
import json
from typing import Any, Dict, List, Optional, Tuple

# Keys APIs conventionally wrap their result arrays in, most specific first.
# Deliberately excludes "content": that's MCP's own content-block wrapper, and
# a list of {type, text} blocks is a message, not a table.
ENVELOPE_KEYS: Tuple[str, ...] = (
    "data", "results", "items", "records", "rows", "entries",
    "elements", "objects", "values", "list",
)

# How many envelope layers to look through, e.g. {"result": {"data": [...]}}.
_MAX_DEPTH = 3

# Sibling values larger than this aren't pagination metadata — skip them
# rather than inflate the observation the model has to read.
_MAX_METADATA_CHARS = 500


def _is_row_list(value: Any, *, strict: bool) -> bool:
    """Is this a non-empty list of records?

    `strict` requires every element to be a dict; it guards the nested search,
    where a heterogeneous list is more likely to be incidental than a table.
    The top-level check stays lenient to preserve long-standing behavior.
    """
    if not isinstance(value, list) or not value:
        return False
    if strict:
        return all(isinstance(item, dict) for item in value)
    return isinstance(value[0], dict)


def find_table(payload: Any) -> Tuple[Optional[List[Any]], str]:
    """Locate the row list in `payload`.

    Returns (rows, dotted_path). `path` is "" when the payload is itself the
    table, and (None, "") when there's no table to be found.
    """
    if _is_row_list(payload, strict=False):
        return payload, ""
    return _descend(payload, depth=0, prefix="")


def _descend(value: Any, depth: int, prefix: str) -> Tuple[Optional[List[Any]], str]:
    if depth >= _MAX_DEPTH or not isinstance(value, dict):
        return None, ""

    # A conventional envelope key holding rows — the overwhelmingly common case.
    for key in ENVELOPE_KEYS:
        if _is_row_list(value.get(key), strict=True):
            return value[key], f"{prefix}{key}"

    # Unconventional key, but only one candidate, so there's nothing to confuse
    # it with. Two or more and we can't tell which is the table — bail out
    # rather than pick the wrong one.
    row_keys = [k for k, v in value.items() if _is_row_list(v, strict=True)]
    if len(row_keys) == 1:
        return value[row_keys[0]], f"{prefix}{row_keys[0]}"

    # Nested envelope: recurse through conventional keys first, then through a
    # sole dict child (same unambiguity rule as above).
    for key in ENVELOPE_KEYS:
        if isinstance(value.get(key), dict):
            rows, path = _descend(value[key], depth + 1, f"{prefix}{key}.")
            if rows is not None:
                return rows, path

    dict_keys = [k for k, v in value.items() if isinstance(v, dict)]
    if len(dict_keys) == 1:
        return _descend(value[dict_keys[0]], depth + 1, f"{prefix}{dict_keys[0]}.")

    return None, ""


def extract_tabular_rows(payload: Any) -> Optional[List[Any]]:
    """The row list inside `payload`, or None if it doesn't hold one."""
    rows, _ = find_table(payload)
    return rows


def detect_content_type(payload: Any) -> str:
    """Classify a tool result as tabular, text, or generic JSON."""
    if isinstance(payload, str):
        return "text"
    if find_table(payload)[0] is not None:
        return "tabular"
    return "json"


def envelope_metadata(payload: Any, path: str) -> Dict[str, Any]:
    """Small values sitting beside the extracted rows.

    Unwrapping a table would otherwise discard the envelope's pagination
    cursors and totals — exactly what the agent needs to decide whether it has
    the whole result set.
    """
    if not path or not isinstance(payload, dict):
        return {}

    parts = path.split(".")
    node: Any = payload
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return {}

    metadata: Dict[str, Any] = {}
    for key, value in node.items():
        if key == parts[-1]:
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            metadata[key] = value
        elif isinstance(value, (dict, list)):
            try:
                if len(json.dumps(value, default=str)) <= _MAX_METADATA_CHARS:
                    metadata[key] = value
            except (TypeError, ValueError):
                continue
    return metadata
