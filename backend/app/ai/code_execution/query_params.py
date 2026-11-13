"""Query parameter resolution, safe SQL rendering, and code checks.

This is the single audited place where parameter VALUES meet SQL TEXT.
Generated code calls `execute_query(sql, params={...})` with `:name`
placeholders; the QueryCapturingClientWrapper pops `params` and calls
`render_sql_with_params` here, then hands the finished plain SQL to the
untouched client. LLM code never assembles the final string — the AST
validator (`check_params_not_formatted`) enforces that `params` values are
never pushed through f-strings/format/%.

Rendering is safe-literal (typed quoting/escaping), not driver binds: values
only, never identifiers, with per-dialect string escaping. Native driver
binds can later be added per-client behind the same wrapper without touching
this contract.
"""
from __future__ import annotations

import ast
import hashlib
import heapq
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote as url_quote, unquote as url_unquote

from app.schemas.param_schema import (
    IDENTITY_ATTR_PREFIX,
    ParamSpec,
)


class ParamError(ValueError):
    """A parameter failed validation/rendering. Surfaced as HTTP 400."""


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def params_fingerprint(values: Optional[Dict[str, Any]]) -> str:
    """Stable hash of resolved param values. '' when there are none, so the
    no-params fast path keeps the pre-params StepUserResult key shape."""
    if not values:
        return ""
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Value validation / coercion
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _parse_iso_temporal(value: str) -> date | datetime:
    # fromisoformat normalizes offset minutes such as +01:60; reject them.
    offset = re.search(r"[+-](\d{2}):?(\d{2})$", value) if len(value) > 10 else None
    if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
        raise ValueError("invalid UTC offset")
    return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _temporal_key(value: str) -> tuple[str, int, Decimal]:
    """Compare ISO bounds without discarding sub-microsecond precision."""
    parsed = _parse_iso_temporal(value)
    seconds = parsed.toordinal() * 86400
    if not isinstance(parsed, datetime):
        return "date", seconds, Decimal(0)
    seconds += parsed.hour * 3600 + parsed.minute * 60 + parsed.second
    offset = parsed.utcoffset()
    if offset is not None:
        seconds -= int(offset.total_seconds())
    fraction = re.search(r"\.(\d+)", value)
    return ("aware" if offset is not None else "naive"), seconds, (Decimal("0." + fraction[1]) if fraction else Decimal(0))


def calendar_date_bounds(value: Any) -> tuple[str | None, str | None]:
    """Calendar-only [from, day-after-to) bounds for DATE/local timestamp queries.

    No timezone conversion or SQL generation. Zoned timestamp columns require
    explicit source/report timezone conversion in the connector's SQL. Exact
    timestamp ranges must not use this calendar helper.
    """
    bounds = coerce_param_value(ParamSpec(name="date_range", type="date_range"), value) or {}
    if any(len(v) != 10 for v in bounds.values()):
        raise ParamError("calendar_date_bounds accepts calendar dates only, not timestamps")
    start, end = bounds.get("from"), bounds.get("to")
    if start and end and start > end:
        raise ParamError("calendar_date_bounds: range 'from' must not be after 'to'")
    try:
        exclusive_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat() if end else None
    except OverflowError as exc:
        raise ParamError("date range upper bound has no representable following day") from exc
    return start, exclusive_end


def date_range_issue(value: Any) -> str | None:
    """Diagnose range semantics without changing legacy query execution.

    Each endpoint retains its own date/timezone convention. Mixed conventions
    need source-specific interpretation, so never invent a conversion or order.
    """
    try:
        bounds = coerce_param_value(ParamSpec(name="date_range", type="date_range"), value) or {}
    except ParamError:
        return "invalid_date_range"
    if "from" not in bounds or "to" not in bounds:
        return None
    start, end = (_temporal_key(bounds[k]) for k in ("from", "to"))
    if start[0] != end[0]:
        return "mixed_date_range"
    if start[1:] > end[1:]:
        return "reversed_date_range"
    return None


def param_values_equal(spec: ParamSpec, expected: Any, applied: Any) -> bool:
    """Compare semantic values without changing stored/executed representations."""
    try:
        left, right = (coerce_param_value(spec, v) for v in (expected, applied))
        if spec.type == "date":
            return (_temporal_key(left) if left else None) == (_temporal_key(right) if right else None)
        if spec.type == "date_range":
            return {k: _temporal_key(v) for k, v in (left or {}).items()} == {k: _temporal_key(v) for k, v in (right or {}).items()}
        return left == right
    except (ParamError, ValueError, TypeError):
        return False


def _coerce_scalar(spec: ParamSpec, value: Any) -> Any:
    t = spec.type
    if value is None:
        return None
    if t == "number":
        if isinstance(value, bool):
            raise ParamError(f"param '{spec.name}': expected number, got boolean")
        if isinstance(value, (int, float)):
            return value
        s = str(value).strip()
        if not _NUM_RE.match(s):
            raise ParamError(f"param '{spec.name}': '{value}' is not a number")
        return float(s) if "." in s else int(s)
    if t == "date":
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        s = str(value).strip()
        if not _DATE_RE.match(s):
            raise ParamError(f"param '{spec.name}': '{value}' is not an ISO date")
        try:
            _parse_iso_temporal(s)
        except ValueError as exc:
            raise ParamError(f"param '{spec.name}': '{value}' is not a valid ISO date/timestamp") from exc
        return s
    if t in ("string", "id"):
        if isinstance(value, (dict, list)):
            raise ParamError(f"param '{spec.name}': expected a scalar {t}")
        return str(value)
    return value


def coerce_param_value(spec: ParamSpec, value: Any) -> Any:
    """Validate + normalize one submitted value against its declaration."""
    if value is None:
        if spec.required and spec.default is None:
            raise ParamError(f"param '{spec.name}' is required")
        return None
    if spec.type == "list":
        items = value if isinstance(value, list) else [value]
        return [
            _coerce_scalar(ParamSpec(name=spec.name, type="string"), v)
            for v in items
        ]
    if spec.type == "date_range":
        if not isinstance(value, dict) or not ("from" in value or "to" in value):
            raise ParamError(
                f"param '{spec.name}': date_range must be an object with 'from'/'to'"
            )
        # This also executes existing saved queries: retain their wire contract.
        # UI metadata is discarded and valid bounds keep their original order
        # and conventions. Semantic checks belong to the opt-in calendar helper
        # and verification, not a new global rejection of legacy inputs.
        out = {}
        for k in ("from", "to"):
            if value.get(k) is not None:
                out[k] = _coerce_scalar(ParamSpec(name=spec.name, type="date"), value[k])
        return out or None
    coerced = _coerce_scalar(spec, value)
    if spec.strict_options and spec.options:
        allowed = {str(o) for o in spec.options}
        if str(coerced) not in allowed:
            raise ParamError(f"param '{spec.name}': value not in allowed options")
    return coerced


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def resolve_identity_binding(binding: str, identity) -> Any:
    """Map a binding expression to a value on a resolved Identity
    (app.data_sources.fast.rls.Identity — full resolution, never the capped
    display payload). Returns None when the identity lacks the attribute."""
    if identity is None:
        return None
    if binding == "viewer.email":
        return getattr(identity, "email", None)
    if binding == "viewer.user_id":
        return getattr(identity, "user_id", None)
    if binding == "viewer.name":
        return getattr(identity, "name", None) or getattr(identity, "email", None)
    if binding == "viewer.groups":
        groups = getattr(identity, "group_names", None)
        if groups is None:
            groups = getattr(identity, "groups", None)
        # Sorted for a stable params fingerprint (group_names is a set).
        return sorted(groups) if groups else []
    if binding.startswith(IDENTITY_ATTR_PREFIX):
        key = binding[len(IDENTITY_ATTR_PREFIX):]
        attrs = getattr(identity, "profile_attributes", None) or {}
        return attrs.get(key)
    return None


def resolve_param_values(
    specs: List[ParamSpec],
    request_values: Optional[Dict[str, Any]],
    identity=None,
    *,
    reject_unknown: bool = True,
) -> Dict[str, Any]:
    """Merge defaults ← request values ← identity bindings into the dict the
    code receives.

    - Unknown request keys are rejected (defense against typo'd/probing input).
    - identity source: client values are REJECTED — the server-resolved
      identity value always wins (an unauthenticated/anonymous identity
      resolves to None, which the code treats as "no rows" per its own guard).
    - input_identity_default: identity value used only when the client sent
      nothing.
    """
    request_values = dict(request_values or {})
    by_name = {s.name: s for s in specs}

    if reject_unknown:
        unknown = [k for k in request_values.keys() if k not in by_name]
        if unknown:
            raise ParamError(f"unknown parameter(s): {', '.join(sorted(unknown))}")

    resolved: Dict[str, Any] = {}
    for spec in specs:
        if spec.source == "identity":
            if spec.name in request_values:
                raise ParamError(
                    f"param '{spec.name}' is identity-locked and cannot be set by the client"
                )
            value = resolve_identity_binding(spec.identity_binding or "viewer.email", identity)
        elif spec.source == "input_identity_default":
            if spec.name in request_values and request_values[spec.name] is not None:
                value = coerce_param_value(spec, request_values[spec.name])
            else:
                value = resolve_identity_binding(spec.identity_binding or "viewer.email", identity)
        else:
            if spec.name in request_values:
                value = coerce_param_value(spec, request_values[spec.name])
            else:
                value = coerce_param_value(spec, spec.default)
        if value is None and spec.required:
            raise ParamError(f"param '{spec.name}' is required")
        resolved[spec.name] = value
    return resolved


def missing_required_inputs(specs: List[ParamSpec], values: Optional[Dict[str, Any]]) -> List[str]:
    """Required `input` params with no default that `values` leaves empty (absent or None).

    The run must not happen without them: Publish and a refresh leave such a
    step unrun, and a reader's run waits for the reader's value. Identity
    params are never "missing" here -- they bind the runner as before.
    """
    values = values or {}
    return [s.name for s in specs
            if s.source == "input" and s.required and s.default is None and values.get(s.name) is None]


# ---------------------------------------------------------------------------
# Safe SQL rendering
# ---------------------------------------------------------------------------
#
# A string value is written the way the connection's engine reads a string
# literal (a `LiteralRenderer`), so it reaches the engine as one string with
# exactly that value. SQL dialects use sqlglot's literal generator for the
# dialect, which its tokenizer (the reading the guard and table scope use)
# reads back as the value; a few engines override it. Connections without a
# sqlglot dialect have their own rule and scanner. Which renderer a client
# gets is declared by type in `client_profiles`.

@dataclass(frozen=True)
class LiteralRenderer:
    """How one engine writes a string literal.

    quote: the value as a complete literal (delimiters and any prefix).
    string_spans: (start, end) of every string literal in a statement, end
    exclusive; a literal left open runs to the end of the text. For the
    grammars read by a scanner (no sqlglot dialect) also every comment and
    quoted identifier, under each reading the engines may give the text, so
    spans of different readings may overlap (SOQL has one reading).
    decode: a literal's body (between its delimiters) as the value the
    engine reads; the inverse of the body `quote` writes.
    """

    name: str
    quote: Callable[[str], str]
    string_spans: Callable[[str], List[Tuple[int, int]]]
    decode: Callable[[str], str]


_STRING_TOKEN_NAMES = frozenset({
    "STRING", "NATIONAL_STRING", "HEREDOC_STRING", "RAW_STRING", "BYTE_STRING",
    "BIT_STRING", "HEX_STRING", "UNICODE_STRING",
})


def _sqlglot_spans(dialect: str) -> Callable[[str], List[Tuple[int, int]]]:
    def spans(sql: str) -> List[Tuple[int, int]]:
        import sqlglot

        try:
            tokens = sqlglot.tokenize(sql, read=dialect)
        except Exception as exc:
            # Strings that cannot be told apart from the rest: no placeholder
            # can be shown to sit outside one.
            raise ParamError("the query's quoted strings could not be read, so parameters cannot be placed") from exc
        return [(t.start, t.end + 1) for t in tokens if t.token_type.name in _STRING_TOKEN_NAMES]
    return spans


def _sqlglot_decode(dialect: str) -> Callable[[str], str]:
    def decode(body: str) -> str:
        import sqlglot

        tokens = sqlglot.tokenize("'" + body + "'", read=dialect)
        if len(tokens) != 1 or tokens[0].token_type.name not in _STRING_TOKEN_NAMES:
            raise ParamError("not the body of one string literal")
        return tokens[0].text
    return decode


def _sqlglot_quote(dialect: str) -> Callable[[str], str]:
    def quote(value: str) -> str:
        from sqlglot import exp

        return exp.Literal.string(value).sql(dialect=dialect)
    return quote


def _doubled(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _refuse_backslash(engine: str) -> Callable[[str], str]:
    def quote(value: str) -> str:
        if "\\" in value:
            raise ParamError(
                f"a parameter value containing a backslash cannot be passed to {engine} safely"
            )
        return _doubled(value)
    return quote


def _mysql_quote(value: str) -> str:
    # Backslash is an escape character in MySQL / MariaDB string literals.
    return "'" + value.replace("'", "''").replace("\\", "\\\\") + "'"


def _national_when_needed(value: str) -> str:
    # T-SQL: a plain 'literal' is a varchar in the database's default
    # collation, so any character outside that code page is silently replaced
    # by '?' at parse time (a Hebrew value on a SQL_Latin1_General database
    # becomes '???' and matches nothing). Non-ASCII strings must be
    # N'literal' (nvarchar) there. ASCII stays plain so comparisons against
    # varchar columns keep their type and index behaviour.
    return ("" if value.isascii() else "N") + _doubled(value)


@dataclass(frozen=True)
class _Reading:
    """One way an engine may read a grammar that has no sqlglot dialect:
    where its strings, quoted identifiers and comments start and end.

    quotes: characters that open a span closed by the same character.
    backslash: those of `quotes` whose spans also escape with a backslash.
    doubled: a doubled quote stays inside the span. Where the engine reads it
    as two strings side by side instead, no code lies between them, so the
    placeholder check gets the same answer either way.
    escape_prefixes: letters that, standing alone right before a quote, make
    its span escape with a backslash (Vertica's E'..').
    verbatim_prefix: a character right before a quote that makes its span
    escape nothing (KQL's @'..').
    fences: texts that open a span closed by the same text, no escapes.
    loose_backslash: a backslash outside any span takes the next character.
    doubled_brackets: ']]' stays inside a [..] span (Sybase).
    q_quotes: q'X..X' with any delimiter X, ([{< closed by their pair
    (Oracle-style quoting).
    executable_comments: a block comment opened with an optional letter and
    '!' (MySQL /*! */, MariaDB /*M! */, TiDB /*T! */) is code.
    """

    quotes: str = "'"
    backslash: str = ""
    doubled: bool = True
    line_comments: Tuple[str, ...] = ()
    block_comments: bool = False
    brackets: bool = False
    escape_prefixes: str = ""
    verbatim_prefix: str = ""
    fences: Tuple[str, ...] = ()
    loose_backslash: bool = False
    doubled_brackets: bool = False
    q_quotes: bool = False
    executable_comments: bool = False


_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}
_EXECUTABLE_COMMENT_RE = re.compile(r"/\*[A-Za-z]?!")


def _q_quote_end(text: str, i: int) -> int:
    """End of a q'X..X' literal opening at i, or -1 when none opens there."""
    n = len(text)
    if i + 2 >= n or text[i + 1] != "'" or text[i + 2].isspace() or (i and (text[i - 1].isalnum() or text[i - 1] == "_")):
        return -1
    close = text.find(_Q_CLOSERS.get(text[i + 2], text[i + 2]) + "'", i + 3)
    return n if close < 0 else close + 2


def _bracket_end(text: str, i: int, doubled: bool) -> int:
    close = text.find("]", i + 1)
    while doubled and close >= 0 and text.startswith("]]", close):
        close = text.find("]", close + 2)
    return len(text) if close < 0 else close + 1


def _quoted_end(text: str, i: int, reading: _Reading) -> int:
    delimiter, n = text[i], len(text)
    escapes = delimiter in reading.backslash
    before = text[i - 1] if i else ""
    if before and before in reading.escape_prefixes and not (i > 1 and (text[i - 2].isalnum() or text[i - 2] == "_")):
        escapes = True
    if before and before == reading.verbatim_prefix:
        escapes = False
    j = i + 1
    while j < n:
        ch = text[j]
        if escapes and ch == "\\":
            j += 2
            continue
        if ch == delimiter:
            if reading.doubled and j + 1 < n and text[j + 1] == delimiter:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _scan(text: str, reading: _Reading) -> List[Tuple[int, int]]:
    """Every span of `text` that is not code under `reading`, in order."""
    out: List[Tuple[int, int]] = []
    i, n = 0, len(text)
    comment_starts = frozenset(c[0] for c in reading.line_comments)
    while i < n:
        ch = text[i]
        if reading.loose_backslash and ch == "\\":
            i += 2
            continue
        if reading.executable_comments and ch == "/":
            marker = _EXECUTABLE_COMMENT_RE.match(text, i)
            if marker:
                i = marker.end()  # the comment's text is code
                continue
        q_end = _q_quote_end(text, i) if reading.q_quotes and ch in "qQ" else -1
        fence = next((f for f in reading.fences if text.startswith(f, i)), None)
        if fence is not None:
            close = text.find(fence, i + len(fence))
            end = n if close < 0 else close + len(fence)
        elif ch in comment_starts and any(text.startswith(c, i) for c in reading.line_comments):
            close = text.find("\n", i)
            end = n if close < 0 else close
        elif reading.block_comments and text.startswith("/*", i):
            close = text.find("*/", i + 2)
            # A comment opened inside another is where a reader that nests
            # comments and one that does not part ways: nothing after it is
            # code to both.
            end = n if close < 0 or text.find("/*", i + 2, close) >= 0 else close + 2
        elif reading.brackets and ch == "[":
            end = _bracket_end(text, i, reading.doubled_brackets)
        elif q_end >= 0:
            end = q_end
        elif ch in reading.quotes:
            end = _quoted_end(text, i, reading)
        else:
            i += 1
            continue
        out.append((i, end))
        i = end
    return out


def _merged(lists: List[List[Tuple[int, int]]]) -> List[Tuple[int, int]]:
    """Sorted spans of several sorted lists, each span once."""
    out: List[Tuple[int, int]] = []
    for span in heapq.merge(*lists):
        if not out or out[-1] != span:
            out.append(span)
    return out


_SpanLists = Callable[[str], List[List[Tuple[int, int]]]]


def _scanned_lists(*readings: _Reading) -> _SpanLists:
    """The spans that are not code, one sorted list per reading."""
    def lists(text: str) -> List[List[Tuple[int, int]]]:
        return [_scan(text, reading) for reading in readings]
    return lists


def _all_spans(lists: _SpanLists) -> Callable[[str], List[Tuple[int, int]]]:
    """The spans that are not code under any reading: a placeholder outside
    all of them is code whichever way the engine reads the text."""
    def spans(text: str) -> List[Tuple[int, int]]:
        return _merged(lists(text))
    return spans


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _percent_decoded(text: str, lo: int, hi: int) -> Tuple[str, List[int]]:
    """text[lo:hi] as a server reads a query option ('+' a space, %XX a
    byte; a byte outside ASCII stands for any character that is not a
    delimiter), and where each decoded character starts in `text` (plus
    `hi` at the end)."""
    out: List[str] = []
    origin: List[int] = []
    i = lo
    while i < hi:
        origin.append(i)
        ch = text[i]
        if ch == "%" and i + 3 <= hi and text[i + 1] in _HEX_DIGITS and text[i + 2] in _HEX_DIGITS:
            byte = int(text[i + 1:i + 3], 16)
            out.append(chr(byte) if byte < 0x80 else chr(0x80))
            i += 3
            continue
        out.append(" " if ch == "+" else ch)
        i += 1
    origin.append(hi)
    return "".join(out), origin


def _segments(text: str, lo: int, hi: int) -> List[Tuple[int, int]]:
    out, start = [], lo
    while True:
        cut = text.find("&", start, hi)
        if cut < 0:
            out.append((start, hi))
            return out
        out.append((start, cut))
        start = cut + 1


def _odata_lists(*readings: _Reading) -> _SpanLists:
    """Spans that are not code in OData options text, as written and as the
    server reads it: split into options at '&' (after a path ending at the
    first '?', for a client that takes one), each option and each option's
    value (after its first '=') percent-decoded on its own. One list per
    reading and split; a value-only list does not cover the text before an
    option's '='."""
    def spans(text: str) -> List[List[Tuple[int, int]]]:
        lists = [_scan(text, reading) for reading in readings]
        query = text.find("?")
        splits = [_segments(text, 0, len(text))]
        if query >= 0:
            splits.append([(0, query)] + _segments(text, query + 1, len(text)))
        for segments in splits:
            for value_only in (False, True):
                found: List[List[Tuple[int, int]]] = [[] for _ in readings]
                for lo, hi in segments:
                    if value_only:
                        equals = text.find("=", lo, hi)
                        lo = hi if equals < 0 else equals + 1
                    decoded, origin = _percent_decoded(text, lo, hi)
                    for k, reading in enumerate(readings):
                        found[k].extend((origin[s], origin[e]) for s, e in _scan(decoded, reading))
                lists.extend(found)
        return lists
    return spans


def _odata_quote(value: str) -> str:
    # OData doubles a quote in a literal; the server percent-decodes the
    # options text first, so the body is percent-encoded to reach it as
    # written, and nothing in it splits or ends the options ('&', '#', '+').
    try:
        return "'" + url_quote(value.replace("'", "''"), safe="") + "'"
    except UnicodeEncodeError as exc:
        raise ParamError("a parameter value holds a character that cannot be sent in an OData query") from exc


def _odata_decode(body: str) -> str:
    return url_unquote(body).replace("''", "'")


def _backslash_decode(escapes: Dict[str, str], only: Optional[str] = None) -> Callable[[str], str]:
    """Body -> value for literals that escape with a backslash: a backslash
    and c read as escapes.get(c, c); with `only`, a backslash before any
    other character stays as written."""
    def decode(body: str) -> str:
        out, i = [], 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body) and (only is None or body[i + 1] in only):
                out.append(escapes.get(body[i + 1], body[i + 1]))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return decode


def _backslash_quote(delimiter: str) -> Callable[[str], str]:
    def quote(value: str) -> str:
        return delimiter + value.replace("\\", "\\\\").replace(delimiter, "\\" + delimiter) + delimiter
    return quote


_C_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _sql_renderer(name: str, dialect: str, quote: Optional[Callable[[str], str]] = None) -> LiteralRenderer:
    return LiteralRenderer(
        name=name,
        quote=quote or _sqlglot_quote(dialect),
        string_spans=_sqlglot_spans(dialect),
        decode=_sqlglot_decode(dialect),
    )


# How the engines behind the scanned renderers may read a statement. Where
# they differ (or the grammar is not documented), each reading is listed and
# a placeholder must be code under every one.
_ANSI_READING = _Reading(quotes="'\"", line_comments=("--",), block_comments=True)
_ANSI_READINGS = (
    _ANSI_READING,
    replace(_ANSI_READING, escape_prefixes="eE", fences=("$$",)),  # Vertica: E'..', $$..$$
    replace(_ANSI_READING, brackets=True),  # [..] names (Sisense)
    replace(_ANSI_READING, brackets=True, doubled_brackets=True),  # [..]] ..] names (Sybase)
    replace(_ANSI_READING, quotes="'\"`"),  # `..` names
    replace(_ANSI_READING, q_quotes=True),  # q'[..]' (NetSuite SuiteQL, Oracle-based)
)
# Spark's lexer: backslash escapes in '..' and "..", `..` names.
_SPARK_READING = _Reading(quotes="'\"`", backslash="'\"", line_comments=("--",), block_comments=True)
_KQL_READING = _Reading(quotes="'\"", backslash="'\"", line_comments=("//",), verbatim_prefix="@", fences=("```",))
_SPL_READING = _Reading(quotes='"', backslash='"', fences=("```",))
# How MySQL and MariaDB read a statement where sqlglot's tokenizer does not:
# '--' starts a comment only before a space or a control character, and an
# executable comment's text is code. With and without NO_BACKSLASH_ESCAPES.
_MYSQL_READING = _Reading(
    quotes="'\"`", backslash="'\"", line_comments=("#",) + tuple("--" + chr(c) for c in range(33)),
    block_comments=True, executable_comments=True,
)
_MYSQL_READINGS = (_MYSQL_READING, replace(_MYSQL_READING, backslash=""))

# The readings the rendered text is re-read with (`_check_rendered`), per
# renderer name: one span list per reading. Every renderer without a sqlglot
# dialect has one; a sqlglot renderer has one where its engines read the
# text other than sqlglot does.
_READINGS: Dict[str, _SpanLists] = {}


def _ansi_decode(body: str) -> str:
    return body.replace("''", "'")


def _scanned(name: str, quote: Callable[[str], str], lists: _SpanLists, decode: Callable[[str], str]) -> LiteralRenderer:
    _READINGS[name] = lists
    return LiteralRenderer(name=name, quote=quote, string_spans=_all_spans(lists), decode=decode)


def _build_renderers() -> Dict[str, LiteralRenderer]:
    renderers = {
        # ANSI: quotes doubled, nothing else (Db2, IBM i, Vertica, Sybase,
        # SAP HANA, Pinot, NetSuite, Oracle BI, Sisense).
        "ansi": _scanned("ansi", _doubled, _scanned_lists(*_ANSI_READINGS), _ansi_decode),
        # OData options text (Priority ERP, SAP Datasphere, MECM): quotes
        # doubled, the body percent-encoded.
        "odata": _scanned("odata", _odata_quote, _odata_lists(_Reading(quotes="'"), _Reading(quotes="'\"")),
                          _odata_decode),
        # Timbr: its lexer is not documented, so the text is read both the
        # ANSI way and Spark's, quotes are doubled (the same value to ANSI;
        # never an escape to Spark) and a backslash value is refused.
        "timbr": _scanned("timbr", _refuse_backslash("Timbr"), _scanned_lists(*_ANSI_READINGS, _SPARK_READING),
                          _ansi_decode),
        # Kusto: '...' with \\ and \'; @'..' verbatim, ```..``` and ~~~..~~~
        # multi-line, // comments.
        "kql": _scanned("kql", _backslash_quote("'"),
                        _scanned_lists(_KQL_READING, replace(_KQL_READING, fences=("~~~",))),
                        _backslash_decode(_C_ESCAPES)),
        # Salesforce SOQL: '...' with \\ and \'; no comments.
        "soql": _scanned("soql", _backslash_quote("'"),
                         _scanned_lists(_Reading(quotes="'", backslash="'", doubled=False)),
                         _backslash_decode(_C_ESCAPES)),
        # Splunk SPL (bare searches): "..." with \\ and \"; ```..``` comments;
        # '..' field names in eval and where; a backslash outside quotes.
        "spl": _scanned("spl", _backslash_quote('"'),
                        _scanned_lists(_SPL_READING, replace(_SPL_READING, quotes="\"'"),
                                       replace(_SPL_READING, loose_backslash=True)),
                        _backslash_decode({}, only='\\"')),
        # sqlglot's Athena tokenizer reads a backslash as an escape; the
        # Trino-based engine Athena runs DML on does not. No rendering of a
        # backslash satisfies both, so such a value is refused.
        "athena": _sql_renderer("athena", "athena", _refuse_backslash("Athena")),
        # sqlglot's Redshift generator writes \' for a quote, which the
        # Redshift rule (no backslash in plain strings) cannot accept.
        "redshift": _sql_renderer("redshift", "redshift", _refuse_backslash("Redshift")),
        "mysql": _sql_renderer("mysql", "mysql", _mysql_quote),
        "tsql": _sql_renderer("tsql", "tsql", _national_when_needed),
        "fabric": _sql_renderer("fabric", "fabric", _national_when_needed),
    }
    for dialect in _SQLGLOT_RENDERER_DIALECTS:
        renderers.setdefault(dialect, _sql_renderer(dialect, dialect))
    for name in _MYSQL_FAMILY_RENDERERS:
        _READINGS[name] = _scanned_lists(*_MYSQL_READINGS)
    return renderers


# Renderers that are a sqlglot dialect's own reading (the name is the
# dialect): every SQL dialect a guard profile names, plus duckdb (file
# connections, accelerated custom tables) and clickhouse (PostHog's HogQL).
_SQLGLOT_RENDERER_DIALECTS = frozenset({
    "postgres", "redshift", "mysql", "tsql", "fabric", "oracle", "sqlite", "snowflake", "bigquery",
    "athena", "presto", "trino", "clickhouse", "databricks", "spark", "duckdb", "teradata", "druid",
})
# Renderers for MySQL-family engines: the text is also read the MySQL way
# (_MYSQL_READINGS) and their '--' rule applies (_refuse_engine_comments).
_MYSQL_FAMILY_RENDERERS = frozenset({"mysql"})

_RENDERERS: Dict[str, LiteralRenderer] = _build_renderers()
_ANSI = _RENDERERS["ansi"]


def _sqlglot_dialect_of(renderer: Optional[LiteralRenderer]) -> Optional[str]:
    """The sqlglot dialect whose reading `renderer` is, if it is one."""
    if renderer is not None and renderer.name in _SQLGLOT_RENDERER_DIALECTS:
        return renderer.name
    return None


def _renderer_for_entry(entry) -> Optional[LiteralRenderer]:
    if entry is None:
        return None
    if entry.renderer:
        return _RENDERERS[entry.renderer]
    if entry.profile is not None and entry.profile.kind == "sql":
        return _RENDERERS[entry.profile.dialect]
    return None


def renderer_for(client) -> Optional[LiteralRenderer]:
    """The renderer declared for `client`'s type (`client_profiles`), or None
    when the client has none: its parameters cannot be rendered."""
    from app.ai.code_execution.client_profiles import client_entry

    return _renderer_for_entry(client_entry(client))


def renderer_for_name(family: str) -> Optional[LiteralRenderer]:
    """The renderer for a client family name (a captured query's tag)."""
    from app.ai.code_execution.client_profiles import _CLIENT_TABLE

    for entry in _CLIENT_TABLE.values():
        if entry.family == family:
            return _renderer_for_entry(entry)
    return None


_PLACEHOLDER_RE = re.compile(r"(?<![:\w]):([a-zA-Z_][a-zA-Z0-9_]*)")

# A placeholder is rendered only where it stands as code: there the literal
# written for it is one value. Anywhere else (a string, a comment, a quoted
# name) the literal's quotes would end or open something else instead.
_QUOTED_TOKEN_NAMES = _STRING_TOKEN_NAMES | {"IDENTIFIER"}
# Engines where E'..' escapes with a backslash, which sqlglot's tokenizer
# ends at a backslash-quote.
_E_STRING_DIALECTS = frozenset({"postgres", "duckdb", "redshift"})


def _inside_every(span_lists: List[List[Tuple[int, int]]], positions: List[int]) -> List[bool]:
    """For each of the sorted `positions`, whether it is inside a span of
    every list (each list sorted, spans not overlapping, end exclusive)."""
    inside = [True] * len(positions)
    for spans in span_lists:
        k = 0
        for p, at in enumerate(positions):
            while k < len(spans) and spans[k][1] <= at:
                k += 1
            if not (k < len(spans) and spans[k][0] <= at):
                inside[p] = False
    return inside


# Line ends other than LF (a CR before an LF ends the line where the LF
# does). Readers disagree on them in a line comment: sqlglot, Postgres and
# DuckDB end one at a CR, SQLite and MySQL do not; VT, FF, FS, GS, RS, NEL,
# LS and PS end a line for some text readers. Readers also disagree on where
# the text ends: libpq and DuckDB stop at a NUL, sqlglot and the scanned
# readings read past it, so render_sql_with_params refuses a NUL anywhere.
_OTHER_LINE_BREAK_RE = re.compile("\r(?!\n)|[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_NUL = chr(0)
# How a comment span of a scanned reading starts (_Reading.line_comments and
# block comments); every other span is a string or a quoted name.
_COMMENT_MARKS = ("--", "//", "#", "/*")


def _refuse_other_line_breaks(sql: str, kept: List[List[Tuple[int, int]]]) -> None:
    """Refuse a line end other than LF anywhere but inside a string or quoted
    name (a span of every list in `kept`). In a line comment, one reader may
    end the comment there and another not, so text one takes for code the
    other takes for comment, and a placeholder one sees as code the other
    sees in a comment or a string. The re-read of the rendered text
    (`_check_rendered`) reads it with this same tokenizer and these same
    readings, so it cannot see that; this rule is its only guard."""
    breaks = [m.start() for m in _OTHER_LINE_BREAK_RE.finditer(sql)]
    if not all(_inside_every(kept, breaks)):
        raise ParamError(
            "parameters cannot be placed in a query with a line break other than a newline (a lone carriage "
            "return or another line separator) outside a string; use plain newlines"
        )


def _refuse_text_read_two_ways(sql: str, tokens, dialect: str) -> None:
    """Refuse text whose strings or comments the engine may end somewhere
    other than sqlglot's tokenizer does."""
    _refuse_other_line_breaks(sql, [[(t.start, t.end + 1) for t in tokens if t.token_type.name in _QUOTED_TOKEN_NAMES]])
    _refuse_engine_comments(sql, tokens, dialect)
    for k, token in enumerate(tokens):
        if any("/*" in c for c in token.comments):
            raise ParamError("parameters cannot be placed in a query with a nested comment; remove the comment")
        kind = token.token_type.name
        if kind not in _STRING_TOKEN_NAMES:
            continue
        if dialect in _E_STRING_DIALECTS and "\\" in token.text and (kind == "BYTE_STRING" or sql[token.start] in "eE"):
            raise ParamError(
                "parameters cannot be placed in a query with an E'...' string holding a backslash; "
                "use a plain string or pass the value as a parameter"
            )
        before = tokens[k - 1] if k else None
        if before is not None and before.token_type.name == "VAR" and before.end == token.start - 1:
            # A string with a prefix sqlglot does not know (Oracle's q'..').
            raise ParamError(
                "parameters cannot be placed in a query with a prefixed string such as q'...'; use a plain string"
            )


# A comment whose text an engine runs: MySQL /*! */, MariaDB /*M! */, TiDB
# /*T! */ (sqlglot's tokenizer keeps the comment's text without '/*').
_RUN_COMMENT_RE = re.compile(r"[A-Za-z]?!")
# MySQL and MariaDB read '--' as a comment only before a space or a control
# character (or at the end); sqlglot always does.
_MYSQL_CODE_DASHES_RE = re.compile(r"--(?![\x00-\x20]|\Z)")


def _refuse_engine_comments(sql: str, tokens, dialect: str) -> None:
    """Refuse comments an engine reads as code while sqlglot drops them."""
    if any(_RUN_COMMENT_RE.match(c) for token in tokens for c in token.comments):
        raise ParamError(
            "parameters cannot be placed in a query with an executable comment such as /*! */ or /*M! */; "
            "remove the comment"
        )
    if dialect not in _MYSQL_FAMILY_RENDERERS:
        return
    dashes = [m.start() for m in _MYSQL_CODE_DASHES_RE.finditer(sql)]
    if not dashes:
        return
    # Fine inside a string or a quoted name, or inside a span under every
    # MySQL reading (such as a comment both read).
    quoted = _inside_every([[(t.start, t.end + 1) for t in tokens if t.token_type.name in _QUOTED_TOKEN_NAMES]], dashes)
    not_code = _inside_every(_READINGS[dialect](sql), dashes)
    if not all(a or b for a, b in zip(quoted, not_code)):
        raise ParamError(
            "parameters cannot be placed in a query with '--' not followed by a space, "
            "which MySQL reads as code; write '-- ' for a comment or '- -' for two minus signs"
        )


def _misplaced_tokens(sql: str, matches: List["re.Match[str]"], dialect: str) -> List[str]:
    import sqlglot

    try:
        tokens = sqlglot.tokenize(sql, read=dialect)
    except Exception as exc:
        # Strings that cannot be told apart from the rest: no placeholder can
        # be shown to stand as code.
        raise ParamError("the query's quoted strings could not be read, so parameters cannot be placed") from exc
    _refuse_text_read_two_ways(sql, tokens, dialect)
    at = {token.start: k for k, token in enumerate(tokens)}

    def quoted(k: int) -> bool:
        return 0 <= k < len(tokens) and tokens[k].token_type.name in _QUOTED_TOKEN_NAMES

    misplaced = []
    for m in matches:
        k = at.get(m.start())
        stands = (
            k is not None and k + 1 < len(tokens)
            and tokens[k].token_type.name == "COLON"
            and (tokens[k + 1].start, tokens[k + 1].end) == (m.start() + 1, m.end() - 1)
            and not quoted(k + 1)
            # Not glued to a quoted token: the two would read as one literal.
            and not (quoted(k - 1) and tokens[k - 1].end == m.start() - 1)
            and not (quoted(k + 2) and tokens[k + 2].start == m.end())
        )
        if not stands:
            misplaced.append(m.group(1))
    return misplaced


def _misplaced_spans(sql: str, matches: List["re.Match[str]"], spans: List[Tuple[int, int]]) -> List[str]:
    """Placeholders inside or touching a span (a quote next to the literal
    written for it would join the two)."""
    n = len(sql)
    delta = [0] * (n + 1)
    for start, end in spans:
        delta[start] += 1
        delta[end] -= 1
    covered = [0] * (n + 1)  # covered[i]: characters before i inside a span
    level = 0
    for i in range(n):
        level += delta[i]
        covered[i + 1] = covered[i] + (level > 0)
    return [m.group(1) for m in matches
            if covered[min(m.end() + 1, n)] - covered[max(m.start() - 1, 0)] > 0]


_JOINS_AFTER_RE = re.compile(r"[.\w]")


def _misplaced_placeholders(sql: str, matches: List["re.Match[str]"], renderer: LiteralRenderer) -> List[str]:
    dialect = _sqlglot_dialect_of(renderer)
    if dialect is not None:
        return _misplaced_tokens(sql, matches, dialect)
    for m in matches:
        # A '.' or a name character after the placeholder would join the
        # value (':v.5' with 5 reads 5.5); the token re-read refuses this
        # for sqlglot renderers.
        if _JOINS_AFTER_RE.match(sql, m.end()):
            raise ParamError(
                f"parameter placeholder :{m.group(1)} is followed by {sql[m.end()]!r}; "
                "put a space or an operator after it"
            )
    readings = _READINGS.get(renderer.name)
    if readings is None:
        return _misplaced_spans(sql, matches, renderer.string_spans(sql))
    lists = readings(sql)
    _refuse_other_line_breaks(sql, [[s for s in spans if not sql.startswith(_COMMENT_MARKS, s[0])] for spans in lists])
    return _misplaced_spans(sql, matches, _merged(lists))


def _rendered_value(value: Any, renderer: LiteralRenderer) -> Tuple[str, List[Tuple[int, int]]]:
    """The text written for `value`, and where its string literals are in it
    (start, end exclusive)."""
    if value is None:
        return "NULL", []
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), []
    if isinstance(value, (int, float)):
        # Guard inf/nan from reaching SQL text.
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ParamError("non-finite number cannot be rendered into SQL")
        return repr(value), []
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if not items:
            # Empty IN () is invalid SQL nearly everywhere; render a
            # never-matching list instead so "no groups" means no rows.
            return "(NULL)", []
        parts, spans, at = [], [], 1
        for item in items:
            text, inner = _rendered_value(item, renderer)
            spans.extend((at + s, at + e) for s, e in inner)
            parts.append(text)
            at += len(text) + 2
        return "(" + ", ".join(parts) + ")", spans
    if isinstance(value, dict):
        raise ParamError("object values cannot be rendered into SQL directly")
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ParamError("byte values cannot be rendered into a query; pass text")
    value = str(value)
    if _NUL in value:
        raise ParamError("a parameter value holds a NUL character, which cannot be sent in a query")
    text = renderer.quote(value)
    return text, [(0, len(text))]


def _render_literal(value: Any, renderer: LiteralRenderer) -> str:
    return _rendered_value(value, renderer)[0]


_SPACES = frozenset(" \t\n\r")
# What may stand right before a placeholder: the start of the text, a space,
# or a character that ends an operator or opens a list. '=<>!' compare, '+-*/%'
# compute, '|' (SQL's '||', a KQL or SPL pipe), '^&~' bit operators, '(' '['
# ',' start an argument, list or array item, ';' ends a statement. Anything
# else may change how the literal is read: KQL's @'..' escapes nothing, SPL's
# backslash takes the quote, MySQL's @ names a variable, a quote, '.', '$',
# '#' or '`' joins the literal to a name, string or comment.
_BEFORE_PLACEHOLDER = _SPACES | frozenset("=<>!+-*/%|^&~([,;")
# OData puts a value after an operator word, '=' (an option's value, a named
# key), '(' or ','; after '&', '?', ';', '/' or an operator character the
# server would read it as an option name, a path or a different value.
_BEFORE_ODATA_PLACEHOLDER = _SPACES | frozenset("=(,")
# Where an OData options text stops for the server: a URL fragment mark, or
# a percent-encoded NUL.
_ODATA_TEXT_END = re.compile(r"#|%00")


def _refuse_prefixes(sql: str, matches: List["re.Match[str]"], renderer: LiteralRenderer) -> None:
    allowed = _BEFORE_ODATA_PLACEHOLDER if renderer.name == "odata" else _BEFORE_PLACEHOLDER
    for m in matches:
        at = m.start()
        if at == 0:
            continue
        before = sql[at - 1]
        # U&'..' is a string whose backslashes are escapes (Postgres, Trino).
        unicode_prefix = before == "&" and at > 1 and sql[at - 2] in "uU"
        if before not in allowed or unicode_prefix:
            raise ParamError(
                f"parameter placeholder :{m.group(1)} follows {before!r}; put it after a space, "
                "an operator or an opening bracket"
            )


def _separated(sql: str, start: int, text: str, renderer: LiteralRenderer) -> str:
    """`text` as written at `start`: a value starting with '-' gets a space
    after any character but a space. After a '-' it would make '--', a
    comment; after another operator character a Postgres-family lexer joins
    the minus to it, making an operator that does not exist. A space
    separates tokens in every grammar rendered here and keeps the minus a
    unary minus. SPL is spaced only after a '-': it reads `field=value` as
    one pair, so the value stays glued to the '=' before it. OData is never
    spaced: it allows no space in a key or an option value."""
    if not text.startswith("-") or not start or renderer.name == "odata":
        return text
    before = sql[start - 1]
    if before == "-" or (renderer.name != "spl" and before not in _SPACES):
        return " " + text
    return text


def _check_rendered(sql: str, matches: List["re.Match[str]"], placed: List[Tuple[int, int, List[Tuple[int, int]]]],
                    rendered: str, renderer: LiteralRenderer) -> None:
    """Re-read the rendered text the way it was checked and refuse it unless
    every value is read exactly as written: each string literal one string,
    nothing in a value opened or closed by its neighbours, and everything
    outside the values read as the template's code was.

    placed: per match, (start, end) of the text written for it in `rendered`
    and its string literals' spans relative to that start."""
    dialect = _sqlglot_dialect_of(renderer)
    readings = _READINGS.get(renderer.name)
    if dialect is None and readings is None:
        raise ParamError("parameters cannot be checked for this connection")
    if dialect is not None:
        _check_tokens(sql, matches, placed, rendered, dialect)
    if readings is not None:
        may_skip = renderer.name == "odata"
        template_lists, rendered_lists = readings(sql), readings(rendered)
        if len(template_lists) != len(rendered_lists):
            raise ParamError(
                "the query with its parameter values was not read the way its template was, "
                "so the values were not placed"
            )
        for template_spans, rendered_spans in zip(template_lists, rendered_lists):
            _check_spans(matches, placed, template_spans, rendered_spans, may_skip)


def _misread() -> ParamError:
    return ParamError(
        "a parameter value would not be read as one value in this query; "
        "put the placeholder where the value goes, after a space or an operator"
    )


def _check_tokens(sql: str, matches, placed, rendered: str, dialect: str) -> None:
    """The rendered text's tokens are the template's, each placeholder's two
    (':' and its name) replaced by the tokens of the text written for it, read
    alone; each string literal is one string token; comments are unchanged."""
    import sqlglot

    try:
        template = sqlglot.tokenize(sql, read=dialect)
        actual = sqlglot.tokenize(rendered, read=dialect)
    except Exception as exc:
        raise _misread() from exc
    at = {token.start: k for k, token in enumerate(template)}
    alone: Dict[str, list] = {}
    expected, comments = [], []
    k, shift = 0, 0
    for m, (start, end, spans) in zip(matches, placed):
        colon = at[m.start()]  # the placement rule found ':' and the name here
        for token in template[k:colon]:
            expected.append((token.token_type, token.text, token.start + shift, token.end + shift))
            comments.extend(token.comments)
        comments.extend(template[colon].comments + template[colon + 1].comments)
        text = rendered[start:end]
        if text not in alone:
            alone[text] = sqlglot.tokenize(text, read=dialect)
        value_tokens = alone[text]
        strings = [(t.start, t.end + 1) for t in value_tokens if t.token_type.name in _STRING_TOKEN_NAMES]
        if strings != spans or any(t.comments for t in value_tokens):
            raise _misread()
        expected.extend((t.token_type, t.text, t.start + start, t.end + start) for t in value_tokens)
        k, shift = colon + 2, end - m.end()
    for token in template[k:]:
        expected.append((token.token_type, token.text, token.start + shift, token.end + shift))
        comments.extend(token.comments)
    if [(t.token_type, t.text, t.start, t.end) for t in actual] != expected:
        raise _misread()
    if [c for t in actual for c in t.comments] != comments:
        raise _misread()


def _check_spans(matches, placed, template_spans, rendered_spans, may_skip: bool) -> None:
    """Under one reading: the spans outside the values are the template's,
    none touching a placeholder, and inside each value exactly its string
    literals (or, where the reading does not cover that part of the text,
    nothing). No span crosses a value's edge."""
    expected_outside: List[Tuple[int, int]] = []
    p, shift = 0, 0
    for s, e in template_spans:
        while p < len(matches) and matches[p].end() < s:
            shift = placed[p][1] - matches[p].end()
            p += 1
        if p < len(matches) and matches[p].start() <= e:
            raise _misread()  # the placeholder is in or against the span: not code here
        expected_outside.append((s + shift, e + shift))
    outside: List[Tuple[int, int]] = []
    inside: List[List[Tuple[int, int]]] = [[] for _ in placed]
    p = 0
    for s, e in rendered_spans:
        while p < len(placed) and placed[p][1] <= s:
            p += 1
        if p < len(placed) and placed[p][0] < e:
            start, end, _ = placed[p]
            if s < start or e > end:
                raise _misread()
            inside[p].append((s - start, e - start))
        else:
            outside.append((s, e))
    if outside != expected_outside:
        raise _misread()
    for (_, _, spans), got in zip(placed, inside):
        if got != spans and not (may_skip and not got):
            raise _misread()


def render_sql_with_params(
    sql: str,
    values: Dict[str, Any],
    *,
    client_type_name: str = "",
    renderer: Optional[LiteralRenderer] = None,
) -> str:
    """Substitute `:name` placeholders in `sql` with safely rendered literals.

    Every placeholder must have a value in `values` (missing → ParamError).
    Extra values are fine — a multi-query generate_df doesn't use every param
    in every statement. Values only, never identifiers. A placeholder must
    stand as code: one inside or against a string, comment or quoted name is
    refused, as is a query whose strings or comments the engine may read
    differently from the check.

    String values are written with `renderer` (the connection's, from
    `renderer_for`); without one, with the renderer of the family
    `client_type_name` names, else ANSI (quotes doubled).
    """
    renderer = renderer or renderer_for_name(client_type_name) or _ANSI
    if renderer.name == "spl" and sql.lstrip().startswith("{"):
        # A JSON envelope: its SPL sits inside a JSON string, where an SPL
        # literal is not what the search receives.
        raise ParamError("parameters are not supported with a JSON search envelope; pass the SPL as text")

    matches = list(_PLACEHOLDER_RE.finditer(sql))
    if not matches:
        return sql
    if _NUL in sql:
        # Some engines read only up to it; the checks below read past it.
        raise ParamError("parameters cannot be placed in a query that holds a NUL character; remove it")
    if renderer.name == "odata" and _ODATA_TEXT_END.search(sql):
        # The options text travels in a URL: the client drops what follows a
        # '#', and the server decodes '%00' to a NUL. Either way, text after
        # it would be checked here and never applied.
        raise ParamError(
            "parameters cannot be placed in OData options that hold '#' or '%00'; remove it "
            "(write a literal '#' in a value as a parameter)"
        )
    _refuse_prefixes(sql, matches, renderer)
    misplaced = _misplaced_placeholders(sql, matches, renderer)
    if misplaced:
        raise ParamError(
            "parameter placeholder(s) inside or against a quoted string, quoted name or comment: "
            + ", ".join(sorted(set(misplaced)))
            + "; put the placeholder where the value goes, without quotes"
        )
    missing = sorted({m.group(1) for m in matches if m.group(1) not in values})
    if missing:
        raise ParamError("SQL uses parameter placeholder(s) with no value: " + ", ".join(missing))

    parts: List[str] = []
    placed: List[Tuple[int, int, List[Tuple[int, int]]]] = []
    pos, length = 0, 0
    for m in matches:
        text, spans = _rendered_value(values[m.group(1)], renderer)
        written = _separated(sql, m.start(), text, renderer)
        lead = len(written) - len(text)
        parts.append(sql[pos:m.start()])
        length += m.start() - pos
        placed.append((length, length + len(written), [(s + lead, e + lead) for s, e in spans]))
        parts.append(written)
        length += len(written)
        pos = m.end()
    parts.append(sql[pos:])
    rendered = "".join(parts)
    _check_rendered(sql, matches, placed, rendered, renderer)
    return rendered


# ---------------------------------------------------------------------------
# Code checks
# ---------------------------------------------------------------------------

def extract_code_param_names(code: str) -> set:
    """Names the code reads off the `params` dict: params["x"], params.get("x")."""
    names = set()
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        # params["x"]
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == "params":
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                names.add(sl.value)
        # params.get("x", ...)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "params":
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
    return names


def code_accepts_params(code: str) -> bool:
    """Does generate_df declare a `params` parameter?"""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "generate_df":
            all_args = node.args.args + node.args.kwonlyargs
            return any(a.arg == "params" for a in all_args)
    return False


def check_params_not_formatted(code: str) -> Optional[str]:
    """Best-effort AST guard: `params` must never be pushed through string
    formatting (f-string, .format, %). Returns a message on violation, None
    when clean. Defense-in-depth — the rendering wrapper is the real
    enforcement."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return None

    def _mentions_params(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == "params":
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue) and _mentions_params(v.value):
                    return "param values must not be interpolated into f-strings — pass them via execute_query(sql, params=...)"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "format":
            if any(_mentions_params(a) for a in list(node.args) + [kw.value for kw in node.keywords]):
                return "param values must not be passed through .format() — use execute_query(sql, params=...)"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if _mentions_params(node.right):
                return "param values must not be %-formatted into strings — use execute_query(sql, params=...)"
    return None


def check_date_range_code(code: str, specs: List[ParamSpec]) -> List[str]:
    """Catch literal wrong-bound reads before a null-default preview hides them.

    Conservative static check, not a proof that arbitrary Python applies a filter.
    Track straight-line aliases within each function; reassignment clears them.
    """
    names = {s.name for s in specs if s.type == "date_range"}
    if not names:
        return []
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    errors = set()

    class Reads(ast.NodeVisitor):
        def __init__(self):
            self.aliases = set()

        def is_range(self, node):
            if isinstance(node, ast.Name):
                return node.id in self.aliases
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                return self.is_range(node.values[0]) and all(isinstance(v, ast.Dict) and not v.keys for v in node.values[1:])
            base, key = self.lookup(node)
            return isinstance(base, ast.Name) and base.id == "params" and key in names

        @staticmethod
        def lookup(node):
            if isinstance(node, ast.Subscript):
                return node.value, node.slice.value if isinstance(node.slice, ast.Constant) else None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
                return node.func.value, node.args[0].value if isinstance(node.args[0], ast.Constant) else None
            return None, None

        def visit_FunctionDef(self, node):
            previous = self.aliases
            self.aliases = set()
            for stmt in node.body:
                self.visit(stmt)
            self.aliases = previous

        def visit_Assign(self, node):
            self.visit(node.value)
            is_range = self.is_range(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases.discard(target.id)
                    if is_range:
                        self.aliases.add(target.id)

        def visit(self, node):
            base, key = self.lookup(node)
            if base is not None and key is not None and self.is_range(base) and key not in ("from", "to"):
                errors.add("date_range uses 'from'/'to', not " + repr(key) + "; use calendar_date_bounds for inclusive calendar dates and open bounds")
            return super().visit(node)

    Reads().visit(tree)
    return sorted(errors)


def check_declarations_vs_code(code: str, specs: List[ParamSpec]) -> List[str]:
    """Consistency errors between declarations and the code's params usage.

    Declared-but-unreferenced params would render dead controls in the UI;
    referenced-but-undeclared names would never receive a value. Only applies
    when the code opts into params (declares the argument)."""
    errors: List[str] = check_date_range_code(code, specs)
    declared = {s.name for s in specs}
    referenced = extract_code_param_names(code)
    if not code_accepts_params(code):
        if declared:
            errors.append(
                "code does not accept a `params` argument but parameters are declared"
            )
        return errors
    for name in sorted(declared - referenced):
        errors.append(f"declared parameter '{name}' is never read by the code")
    for name in sorted(referenced - declared):
        errors.append(f"code reads params['{name}'] but no such parameter is declared")
    return errors


_SQL_COMMENT_RE = re.compile(
    r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")|--[^\n]*|/\*.*?\*/", re.S
)
_SQL_READS_RE = re.compile(r"\bFROM\b", re.I)
_SQL_START_RE = re.compile(r"^\s*(\(\s*)*(SELECT|WITH|VALUES|SET|SHOW|DESCRIBE|DESC|EXPLAIN|PRAGMA|USE)\b", re.I)
_SQL_PREDICATE_RE = re.compile(r"\b(WHERE|ON|HAVING|QUALIFY|USING)\b", re.I)


def _strip_sql_comments(sql: str) -> str:
    """Drop ``--`` and ``/* */`` comments, leaving quoted literals intact (a
    ``--`` inside a string is data, not a comment)."""
    return _SQL_COMMENT_RE.sub(lambda m: m.group(1) or " ", sql)


def _heuristic_binds_value(capture: str, needles: List[str]) -> bool:
    """Legacy text heuristic: whether one executed query carries an identity
    value where it can actually filter rows. Used only when the structural
    check (identity_bind.structural_binds) cannot decide.

    SQL: the value must appear AFTER a predicate keyword (WHERE/ON/HAVING/...),
    once comments are stripped — a value echoed in the SELECT list or hidden in
    a comment scopes nothing. Non-SQL captures (structured/keyword calls to
    API connectors) have no grammar to check, so containment is all we can
    ask. This is a heuristic backstop, not a proof: ``WHERE 'x' = 'x'`` still
    satisfies it. Hard isolation belongs in the database (RLS / per-user
    credentials)."""
    if not _SQL_START_RE.match(_strip_sql_comments(capture)):
        return any(n in capture for n in needles)
    sql = _strip_sql_comments(capture)
    if not _SQL_READS_RE.search(sql):
        # Reads no table (``SELECT 1``, ``SET ...``): no rows to leak.
        return True
    first = _SQL_PREDICATE_RE.search(sql)
    if first is None:
        return False
    tail = sql[first.start():]
    return any(n in tail for n in needles)


def _query_binds_value(capture: str, needles: List[str], param: str = "") -> bool:
    """Whether one executed query restricts its rows by an identity value.

    Structural first (sqlglot for SQL/SOQL, a small tokenizer for Splunk SPL —
    see identity_bind); when the structure can't be decided (parse error,
    unsupported shape, non-SQL API capture) falls back to the legacy text
    heuristic, unchanged, and logs a structured warning."""
    from app.ai.code_execution.identity_bind import log_fallback, structural_binds

    client_type = getattr(capture, "client_type", None)
    renderer = renderer_for_name(client_type or "")
    if renderer is not None and renderer.name == "odata":
        # The server never receives OData text after a '#' (the client drops
        # the fragment) or a '%00' (decoded to a NUL): it filters nothing.
        end = _ODATA_TEXT_END.search(capture)
        if end is not None:
            capture = capture[:end.start()]
    stripped = _strip_sql_comments(capture)
    looks_sql = bool(_SQL_START_RE.match(stripped))
    verdict = structural_binds(str(capture), client_type, tuple(needles), looks_sql)
    if verdict is not None:
        return verdict
    if looks_sql or client_type == "SplunkClient":
        log_fallback(str(capture), client_type, param)
    return _heuristic_binds_value(capture, needles + _rendered_needles(client_type, needles))


def _rendered_needles(client_type: Optional[str], needles: List[str]) -> List[str]:
    """Each value as the capture's renderer writes it, quotes included: where
    the renderer escapes a character the raw value is not in the text."""
    renderer = renderer_for_name(client_type or "")
    if renderer is None:
        return []
    out = []
    for needle in needles:
        try:
            out.append(renderer.quote(needle))
        except ParamError:
            continue  # a value this connection cannot carry was never rendered
    return out


def verify_identity_binds_in_queries(
    captured_queries: List[str],
    values: Dict[str, Any],
    specs: List[ParamSpec],
) -> Optional[str]:
    """Post-execution check: every identity-locked param whose value is set
    must be applied, as a filter, in EVERY executed query that reads a table.
    Prevents "the LLM forgot the WHERE clause" from silently serving unscoped
    rows to a viewer — including the multi-query case where one query is
    scoped and another is not. Code that issues no query at all (params
    applied in pandas over files or a BOW source) has nothing to verify here
    and passes, as before. Returns an error message or None."""
    identity_specs = [
        s for s in specs
        if s.source == "identity" and values.get(s.name) not in (None, "", [])
    ]
    if not identity_specs:
        return None
    queries = [q for q in (captured_queries or []) if isinstance(q, str) and q.strip()]
    if not queries:
        return None
    for spec in identity_specs:
        value = values.get(spec.name)
        needles = [str(n) for n in (value if isinstance(value, list) else [value]) if n not in (None, "")]
        for q in queries:
            if not _query_binds_value(q, needles, spec.name):
                return (
                    f"identity parameter '{spec.name}' was not applied as a filter in every "
                    "executed query; refusing to serve unscoped results"
                )
    return None
