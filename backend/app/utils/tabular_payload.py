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
# Six, not three: ERP-style BOD responses really do nest this deep —
# {"ShowResponse": {"ShowResponse": {"DataArea": {"ProductionOrders": {...}}}}}
# put the rows at level 5, and a 3-level cutoff classified the whole response
# as opaque JSON. Descent below stays conservative (envelope keys or a sole
# dict child), so extra depth doesn't make it grab incidental lists.
_MAX_DEPTH = 6

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
    rows, path = _descend(payload, depth=0, prefix="")
    if rows is not None:
        return rows, path
    # The descent above recurses only through envelope keys or a SOLE dict
    # child, so a multi-branch envelope (an ApplicationArea sitting beside the
    # DataArea that holds the rows) hides its table from it. The candidate walk
    # visits every dict child; take its winner when it wins outright — an
    # equal-length tie is still a coin flip we refuse to call.
    candidates = table_candidates(payload)
    if candidates and (len(candidates) == 1 or candidates[0][1] > candidates[1][1]):
        found = _rows_at(payload, candidates[0][0])
        if found is not None:
            return found, candidates[0][0]
    # Last resort: shapes that are tables without being a list of records.
    return _reshape(payload, prefix="")


def _rows_at(payload: Any, path: str) -> Optional[List[Any]]:
    """The list at a dotted `path` inside `payload`, or None."""
    node: Any = payload
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, list) else None


def table_candidates(payload: Any) -> List[Tuple[str, int]]:
    """Every path in `payload` that could be the table, as (path, row_count).

    `find_table` returns only the best one. When a payload carries more than one
    array of records — rows plus a `warnings` list, say — the runner-up paths
    are what let the agent (or a human reading the observation) see that a
    choice was made and pick differently.
    Ordered most rows first, matching find_table's own preference.
    """
    if _is_row_list(payload, strict=False):
        return [("", len(payload))]
    if not isinstance(payload, dict):
        return []
    found: List[Tuple[str, int]] = []
    _collect(payload, depth=0, prefix="", out=found)
    return sorted(found, key=lambda pair: pair[1], reverse=True)


def _collect(value: Any, depth: int, prefix: str, out: List[Tuple[str, int]]) -> None:
    if depth >= _MAX_DEPTH or not isinstance(value, dict):
        return
    for key, child in value.items():
        if _is_row_list(child, strict=True):
            out.append((f"{prefix}{key}", len(child)))
        elif isinstance(child, dict):
            _collect(child, depth + 1, f"{prefix}{key}.", out)


def _descend(value: Any, depth: int, prefix: str) -> Tuple[Optional[List[Any]], str]:
    if depth >= _MAX_DEPTH or not isinstance(value, dict):
        return None, ""

    # A conventional envelope key holding rows — the overwhelmingly common case.
    for key in ENVELOPE_KEYS:
        if _is_row_list(value.get(key), strict=True):
            return value[key], f"{prefix}{key}"

    # Unconventional keys. More than one array of records used to mean "give up"
    # — but the realistic multi-candidate payload is a table plus a short
    # sidecar list (`validationWarnings`, `errors`, `links`), and giving up there
    # threw away the rows and left the caller with an opaque blob. Take the
    # longest list when it wins outright: the table is the thing the tool was
    # asked for, and a sidecar that outnumbers the result set is not a shape
    # worth optimizing for. Equal-length candidates are a real coin flip, so
    # those still fall through rather than guess. Either way
    # `table_candidates` reports every path, so the choice stays visible.
    row_keys = sorted(
        (k for k, v in value.items() if _is_row_list(v, strict=True)),
        key=lambda k: len(value[k]),
        reverse=True,
    )
    if len(row_keys) == 1:
        return value[row_keys[0]], f"{prefix}{row_keys[0]}"
    if len(row_keys) > 1 and len(value[row_keys[0]]) > len(value[row_keys[1]]):
        return value[row_keys[0]], f"{prefix}{row_keys[0]}"

    # Nested envelope: recurse through conventional keys first, then through a
    # sole dict child (nothing to confuse it with).
    for key in ENVELOPE_KEYS:
        if isinstance(value.get(key), dict):
            rows, path = _descend(value[key], depth + 1, f"{prefix}{key}.")
            if rows is not None:
                return rows, path

    dict_keys = [k for k, v in value.items() if isinstance(v, dict)]
    if len(dict_keys) == 1:
        return _descend(value[dict_keys[0]], depth + 1, f"{prefix}{dict_keys[0]}.")

    return None, ""


# A mapping needs at least this many entries before we read it as a table
# keyed by id rather than as one record with a few object-valued fields.
_MIN_KEYED_ROWS = 2


def _reshape(payload: Any, prefix: str) -> Tuple[Optional[List[Any]], str]:
    """Recognize tables that aren't lists of records.

    Two shapes APIs return often enough to matter:

      * keyed by id — ``{"c1": {...}, "c2": {...}}``; the key is data, so it is
        preserved as a ``key`` column (``_key`` if the records already use it),
      * column-oriented — ``{"name": [...], "qty": [...]}``, equal-length
        parallel arrays, which is a DataFrame transposed.

    Both used to be classified as opaque JSON.
    """
    if not isinstance(payload, dict) or not payload:
        return None, ""

    values = list(payload.values())

    # Keyed by id: every value a record, and the records agree on some field.
    if len(payload) >= _MIN_KEYED_ROWS and all(isinstance(v, dict) and v for v in values):
        shared = set(values[0])
        for v in values[1:]:
            shared &= set(v)
        if shared:
            key_field = "key" if not any("key" in v for v in values) else "_key"
            rows = [{key_field: k, **v} for k, v in payload.items()]
            return rows, prefix.rstrip(".")

    # Column-oriented: parallel scalar arrays of equal, non-zero length.
    if len(payload) >= 2 and all(
        isinstance(v, list) and v and not any(isinstance(i, (dict, list)) for i in v)
        for v in values
    ):
        length = len(values[0])
        if all(len(v) == length for v in values):
            columns = list(payload)
            rows = [{c: payload[c][i] for c in columns} for i in range(length)]
            return rows, prefix.rstrip(".")

    return None, ""


def extract_tabular_rows(payload: Any) -> Optional[List[Any]]:
    """The row list inside `payload`, or None if it doesn't hold one."""
    rows, _ = find_table(payload)
    return rows


# Enough of a text payload to sniff its dialect without reading a huge blob.
_SNIFF_CHARS = 8192


def parse_text_payload(text: str) -> Tuple[str, Optional[List[Any]]]:
    """Second-guess a payload the provider handed back as plain text.

    An MCP server that returns CSV, NDJSON, or JSON-that-failed-to-parse-once
    arrives here as an opaque string. Everything downstream then treats a
    perfectly good table as prose. Returns ``(kind, rows)`` where kind is one of
    ``json`` / ``ndjson`` / ``csv`` / ``text``; ``rows`` is None unless the text
    turned out to hold records.
    """
    if not isinstance(text, str) or not text.strip():
        return "text", None

    stripped = text.strip()

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        pass
    else:
        rows, _ = find_table(parsed)
        return "json", rows

    # NDJSON / JSON Lines: every non-blank line its own object.
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) >= 2:
        records = []
        for line in lines:
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                records = []
                break
            if not isinstance(obj, dict):
                records = []
                break
            records.append(obj)
        if records:
            return "ndjson", records

    # Delimited text. csv.Sniffer will happily "detect" a delimiter in ordinary
    # prose — English is full of commas — and templated prose even yields a
    # consistent column count, so shape alone is not evidence. Require the text
    # to look like a data file and the first row to look like field names.
    if len(lines) >= 2 and not _has_blank_line(stripped):
        import csv
        import io

        sample = stripped[:_SNIFF_CHARS]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            rows = list(csv.DictReader(io.StringIO(stripped), dialect=dialect))
        except (csv.Error, ValueError):
            rows = []
        if (
            len(rows) >= 2
            and len(rows[0]) >= 2
            and all(len(r) == len(rows[0]) for r in rows[:20])
            and _looks_like_header(list(rows[0]))
        ):
            return "csv", rows

    return "text", None


def _has_blank_line(text: str) -> bool:
    """Paragraph breaks mean prose. A CSV does not separate records this way."""
    return any(not line.strip() for line in text.splitlines())


# A column name is a label, not a sentence.
_MAX_HEADER_CHARS = 64
_MAX_HEADER_WORDS = 6


def _looks_like_header(names: List[Any]) -> bool:
    if len(set(names)) != len(names):  # a real header row has distinct names
        return False
    for name in names:
        if not isinstance(name, str):
            return False
        cleaned = name.strip()
        if not cleaned or len(cleaned) > _MAX_HEADER_CHARS:
            return False
        if len(cleaned.split()) > _MAX_HEADER_WORDS:
            return False
        # Sentence punctuation — the tell that this line is prose that happened
        # to contain the delimiter.
        if cleaned.endswith((".", ":", "!", "?")) or ". " in cleaned:
            return False
    return True


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
