"""Evaluate an OData `$filter` / `$orderby` expression locally, over plain rows.

SharePoint Server refuses a `$filter` or `$orderby` on a column that is not
indexed once a list holds more items than the List View Threshold (5,000 by
default): it has to scan the whole list to answer, and the threshold counts the
items scanned, not the ones returned. `$top` does not help — it is applied after
the filter. So the on-premises lists connector never sends a filter to the
server. It reads the list in ID order, which is always indexed and safe at any
size, and applies the query's filter and sort here.

The grammar is the subset of OData v3 that people and models write against
SharePoint:

    expr     := and_expr ('or' and_expr)*
    and_expr := not_expr ('and' not_expr)*
    not_expr := 'not' not_expr | compare
    compare  := operand (('eq'|'ne'|'gt'|'ge'|'lt'|'le') operand)?
    operand  := literal | column | function '(' expr (',' expr)* ')' | '(' expr ')'

Functions: startswith, endswith, substringof (SharePoint's argument order),
contains, tolower, toupper, trim, length, year, month, day, hour, minute.
Literals: 'text' ('' escapes a quote), numbers, true, false, null,
datetime'2026-01-01T00:00:00Z'. A plain string literal compared with a date
column is read as a date.

Semantics follow SharePoint rather than strict OData where they differ:

* text comparisons are case-insensitive, as they are in SharePoint's SQL
  collation;
* a multi-value column (multi-choice, multi-person, multi-lookup) matches
  `eq` when any of its values does, and `ne` when none does;
* comparing an empty value with anything but `null` is false.

Column references are resolved by a caller-supplied resolver so the parser
never needs to know about display versus internal names. Nothing here calls
`eval`; an unknown token is an error that names it.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


class ODataFilterError(ValueError):
    """The expression is not valid, or names a column the list does not have."""


_COMPARISONS = {"eq", "ne", "gt", "ge", "lt", "le"}
_KEYWORDS = _COMPARISONS | {"and", "or", "not"}
_FUNCTIONS = {
    "startswith", "endswith", "substringof", "contains", "tolower", "toupper",
    "trim", "length", "year", "month", "day", "hour", "minute",
}
_NUMBER = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[mMdDfFlL]?")
_WORD = re.compile(r"[A-Za-z_][\w./]*")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?$")


@dataclass(frozen=True)
class _Tok:
    kind: str      # 'str' | 'num' | 'date' | 'col' | 'kw' | 'func' | 'lit' | '(' | ')' | ','
    value: Any
    pos: int


@dataclass(frozen=True)
class Column:
    """What a resolver hands back for a column reference."""
    key: str       # the row key the value lives under
    dtype: str     # 'string' | 'integer' | 'float' | 'datetime' | 'boolean'
    name: str      # the name to show in errors


Resolver = Callable[[str], Optional[Column]]


def parse_datetime(value: Any) -> Optional[dt.datetime]:
    """An aware UTC datetime from an ISO-8601 string (or a datetime); None if not one.

    A value without an offset is taken as UTC: SharePoint's REST service
    returns dates in UTC, and a filter literal without a zone means the same.
    """
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _ISO_DATE.match(text):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _tokenize(expr: str, column_names: Sequence[str]) -> List[_Tok]:
    """Split an expression into tokens.

    Column names may contain spaces or start with a digit ("Total 2017",
    "2017"), so at each position the longest known column name wins — but a
    number right after a comparison operator is always a value, never a column.
    """
    names = sorted({n for n in column_names if n}, key=len, reverse=True)
    lowered = [(n, n.casefold()) for n in names]
    tokens: List[_Tok] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "(),":
            tokens.append(_Tok(ch, ch, i))
            i += 1
            continue
        if ch == "'":
            text, i = _read_string(expr, i)
            tokens.append(_Tok("str", text, i))
            continue
        prev = tokens[-1] if tokens else None
        after_operator = prev is not None and prev.kind == "kw" and prev.value in _COMPARISONS
        # A known column name, longest first, on word boundaries.
        if not after_operator or not _NUMBER.match(expr, i):
            matched = _match_column(expr, i, lowered)
            if matched is not None:
                name, length = matched
                tokens.append(_Tok("col", name, i))
                i += length
                continue
        m = _NUMBER.match(expr, i)
        if m and (m.end() == n or not (expr[m.end()].isalnum() or expr[m.end()] == "_")):
            raw = m.group(0).rstrip("mMdDfFlL")
            tokens.append(_Tok("num", float(raw) if any(c in raw for c in ".eE") else int(raw), i))
            i = m.end()
            continue
        m = _WORD.match(expr, i)
        if m:
            word = m.group(0)
            low = word.lower()
            if low in ("datetime", "datetimeoffset") and m.end() < n and expr[m.end()] == "'":
                text, i = _read_string(expr, m.end())
                parsed = parse_datetime(text)
                if parsed is None:
                    raise ODataFilterError(f"Invalid date literal {text!r}; use ISO-8601, e.g. '2026-01-01T00:00:00Z'.")
                tokens.append(_Tok("date", parsed, m.start()))
                continue
            if low in _KEYWORDS:
                tokens.append(_Tok("kw", low, i))
            elif low in ("true", "false"):
                tokens.append(_Tok("lit", low == "true", i))
            elif low == "null":
                tokens.append(_Tok("lit", None, i))
            elif low in _FUNCTIONS and _next_non_space(expr, m.end()) == "(":
                tokens.append(_Tok("func", low, i))
            else:
                # Graph-style `fields/Name` references, or an unknown word.
                tokens.append(_Tok("col", word[7:] if low.startswith("fields/") else word, i))
            i = m.end()
            continue
        raise ODataFilterError(f"Unexpected character {ch!r} at position {i} in filter.")
    return tokens


def _read_string(expr: str, start: int) -> Tuple[str, int]:
    """Read a single-quoted literal starting at `start`; '' is an escaped quote."""
    out, i, n = [], start + 1, len(expr)
    while i < n:
        if expr[i] == "'":
            if i + 1 < n and expr[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), i + 1
        out.append(expr[i])
        i += 1
    raise ODataFilterError("Unterminated string literal in filter (strings use single quotes; double a quote to escape it).")


def _match_column(expr: str, i: int, names: Sequence[Tuple[str, str]]) -> Optional[Tuple[str, int]]:
    if i > 0 and (expr[i - 1].isalnum() or expr[i - 1] in "_/"):
        return None
    offset = 7 if expr[i:i + 7].lower() == "fields/" else 0
    head = expr[i + offset:].casefold()
    for name, low in names:
        if head.startswith(low):
            end = i + offset + len(name)
            if end < len(expr) and (expr[end].isalnum() or expr[end] == "_"):
                continue
            # `year(Created)`: a function call, even when a column is named Year.
            if low in _FUNCTIONS and _next_non_space(expr, end) == "(":
                continue
            return name, offset + len(name)
    return None


def _next_non_space(expr: str, i: int) -> str:
    while i < len(expr) and expr[i].isspace():
        i += 1
    return expr[i] if i < len(expr) else ""


# --------------------------------------------------------------------- parsing
# The parsed form is a small tuple tree:
#   ('or', a, b) ('and', a, b) ('not', a) ('cmp', op, a, b)
#   ('col', Column) ('lit', value) ('func', name, [args])


class _Parser:
    def __init__(self, tokens: List[_Tok], resolve: Resolver):
        self.tokens, self.i, self.resolve = tokens, 0, resolve

    def peek(self) -> Optional[_Tok]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, kind: str, value: Any = None) -> _Tok:
        tok = self.peek()
        if tok is None or tok.kind != kind or (value is not None and tok.value != value):
            want = value or kind
            got = "end of filter" if tok is None else repr(tok.value)
            raise ODataFilterError(f"Expected {want!r} but found {got} in filter.")
        self.i += 1
        return tok

    def parse(self):
        if not self.tokens:
            raise ODataFilterError("Filter is empty.")
        node = self.or_expr()
        if self.peek() is not None:
            raise ODataFilterError(f"Unexpected {self.peek().value!r} in filter; combine conditions with 'and'/'or'.")
        return node

    def or_expr(self):
        node = self.and_expr()
        while (tok := self.peek()) is not None and tok.kind == "kw" and tok.value == "or":
            self.i += 1
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.not_expr()
        while (tok := self.peek()) is not None and tok.kind == "kw" and tok.value == "and":
            self.i += 1
            node = ("and", node, self.not_expr())
        return node

    def not_expr(self):
        tok = self.peek()
        if tok is not None and tok.kind == "kw" and tok.value == "not":
            self.i += 1
            return ("not", self.not_expr())
        return self.compare()

    def compare(self):
        left = self.operand()
        tok = self.peek()
        if tok is not None and tok.kind == "kw" and tok.value in _COMPARISONS:
            self.i += 1
            return ("cmp", tok.value, left, self.operand())
        return left

    def operand(self):
        tok = self.peek()
        if tok is None:
            raise ODataFilterError("Filter ends where a value or column was expected.")
        self.i += 1
        if tok.kind == "(":
            node = self.or_expr()
            self.take(")")
            return node
        if tok.kind in ("str", "num", "lit"):
            return ("lit", tok.value)
        if tok.kind == "date":
            return ("lit", tok.value)
        if tok.kind == "col":
            column = self.resolve(tok.value)
            if column is None:
                raise ODataFilterError(f"Unknown column {tok.value!r} in filter.")
            return ("col", column)
        if tok.kind == "func":
            self.take("(")
            args = [self.or_expr()]
            while (nxt := self.peek()) is not None and nxt.kind == ",":
                self.i += 1
                args.append(self.or_expr())
            self.take(")")
            return ("func", tok.value, args)
        raise ODataFilterError(f"Unexpected {tok.value!r} in filter.")


# ------------------------------------------------------------------ evaluation

def _fold(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def _values(node, row: Dict[str, Any]) -> Tuple[List[Any], Optional[str]]:
    """Evaluate an operand to its candidate values plus the column dtype it came from."""
    kind = node[0]
    if kind == "col":
        column: Column = node[1]
        value = row.get(column.key)
        if isinstance(value, (list, tuple)):
            return list(value), column.dtype
        return [value], column.dtype
    if kind == "lit":
        return [node[1]], None
    if kind == "func":
        return [_call(node[1], node[2], row)], None
    return [_truth(node, row)], "boolean"


def _scalar(node, row) -> Any:
    values, _ = _values(node, row)
    if len(values) == 1:
        return values[0]
    return values


def _call(name: str, args, row) -> Any:
    def arity(count):
        if len(args) != count:
            raise ODataFilterError(f"{name}() takes {count} argument{'s' if count != 1 else ''}.")

    if name in ("startswith", "endswith", "contains", "substringof"):
        arity(2)
        if name == "substringof":  # substringof('needle', Column)
            needles, haystacks = _values(args[0], row)[0], _values(args[1], row)[0]
        else:
            haystacks, needles = _values(args[0], row)[0], _values(args[1], row)[0]
        needle = needles[0] if needles else None
        if needle is None:
            return False
        needle = str(needle).casefold()
        for hay in haystacks:
            if hay is None:
                continue
            text = str(hay).casefold()
            if ((name == "startswith" and text.startswith(needle))
                    or (name == "endswith" and text.endswith(needle))
                    or (name in ("contains", "substringof") and needle in text)):
                return True
        return False
    arity(1)
    value = _scalar(args[0], row)
    if value is None:
        return None
    if name in ("tolower", "toupper", "trim"):
        text = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
        return {"tolower": text.lower, "toupper": text.upper, "trim": text.strip}[name]()
    if name == "length":
        return len(value) if isinstance(value, (str, list)) else len(str(value))
    when = parse_datetime(value)
    if when is None:
        raise ODataFilterError(f"{name}() needs a date column.")
    return getattr(when, name)


def _coerce(value: Any, dtype: Optional[str]) -> Any:
    """Bring a literal to the type of the column it is compared with."""
    if value is None:
        return None
    if dtype == "datetime":
        parsed = parse_datetime(value)
        if parsed is None:
            raise ODataFilterError(f"Cannot compare a date column with {value!r}; use ISO-8601, e.g. '2026-01-01T00:00:00Z'.")
        return parsed
    if dtype in ("float", "integer"):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).strip())
        except ValueError:
            raise ODataFilterError(f"Cannot compare a number column with {value!r}.")
    if dtype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        raise ODataFilterError(f"Cannot compare a yes/no column with {value!r}.")
    if dtype == "string":
        return str(value)
    return value


def _compare(op: str, left: Any, right: Any) -> bool:
    if left is None or right is None:
        if op == "eq":
            return left is None and right is None
        if op == "ne":
            return not (left is None and right is None)
        return False
    left, right = _fold(left), _fold(right)
    try:
        if op == "eq":
            return left == right
        if op == "ne":
            return left != right
        if op == "gt":
            return left > right
        if op == "ge":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right
    except TypeError:
        # e.g. text against a number on an untyped column: never equal, never ordered.
        return op == "ne"


def _truth(node, row: Dict[str, Any]) -> bool:
    kind = node[0]
    if kind == "or":
        return _truth(node[1], row) or _truth(node[2], row)
    if kind == "and":
        return _truth(node[1], row) and _truth(node[2], row)
    if kind == "not":
        return not _truth(node[1], row)
    if kind == "cmp":
        op, left_node, right_node = node[1], node[2], node[3]
        lefts, ldtype = _values(left_node, row)
        rights, rdtype = _values(right_node, row)
        # Coerce each side to the other side's column type.
        if ldtype and not rdtype:
            rights = [_coerce(v, ldtype) for v in rights]
        elif rdtype and not ldtype:
            lefts = [_coerce(v, rdtype) for v in lefts]
        if ldtype == "datetime":
            lefts = [parse_datetime(v) if v is not None else None for v in lefts]
        if rdtype == "datetime":
            rights = [parse_datetime(v) if v is not None else None for v in rights]
        lefts = lefts or [None]
        rights = rights or [None]
        if op == "ne":
            # Multi-value: "ne x" holds when no value equals x.
            return not any(_compare("eq", l, r) for l in lefts for r in rights)
        return any(_compare(op, l, r) for l in lefts for r in rights)
    if kind == "func":
        return bool(_call(node[1], node[2], row))
    if kind == "col":  # a bare yes/no column: `IsActive`
        return bool(row.get(node[1].key))
    if kind == "lit":
        return bool(node[1])
    raise ODataFilterError("Unsupported filter expression.")


class CompiledFilter:
    """A parsed `$filter`, applied with `matches(row)`; `keys` are the row keys it reads."""

    def __init__(self, tree, keys: Iterable[str]):
        self._tree = tree
        self.keys = tuple(dict.fromkeys(keys))

    def matches(self, row: Dict[str, Any]) -> bool:
        return _truth(self._tree, row)


def compile_filter(expr: str, column_names: Sequence[str], resolve: Resolver) -> CompiledFilter:
    """Parse `expr` once. Raises ODataFilterError on a syntax error or unknown column."""
    keys: List[str] = []

    def tracking(name: str) -> Optional[Column]:
        column = resolve(name)
        if column is not None:
            keys.append(column.key)
        return column

    tree = _Parser(_tokenize(str(expr), column_names), tracking).parse()
    if tree[0] == "lit":
        raise ODataFilterError("Filter must be a condition, e.g. Status eq 'Open'.")
    return CompiledFilter(tree, keys)


def parse_orderby(expr: str, column_names: Sequence[str], resolve: Resolver) -> List[Tuple[Column, bool]]:
    """`"Modified desc, Title"` → [(Column, descending)]."""
    out: List[Tuple[Column, bool]] = []
    for part in [p.strip() for p in str(expr).split(",") if p.strip()]:
        descending = False
        low = part.lower()
        if low.endswith(" desc"):
            part, descending = part[:-5].strip(), True
        elif low.endswith(" asc"):
            part = part[:-4].strip()
        if part.lower().startswith("fields/"):
            part = part[7:]
        column = resolve(part)
        if column is None:
            raise ODataFilterError(f"Unknown column {part!r} in orderby.")
        out.append((column, descending))
    return out


def sort_rows(rows: List[Dict[str, Any]], order: List[Tuple[Column, bool]]) -> List[Dict[str, Any]]:
    """Stable multi-key sort; empty values last in either direction; text case-insensitive."""
    def key_for(column: Column):
        def key(row):
            value = row.get(column.key)
            if isinstance(value, list):
                value = "; ".join(map(str, value)) if value else None
            if value is None:
                return None
            if column.dtype == "datetime":
                return parse_datetime(value)
            return _fold(value)
        return key

    out = list(rows)
    for column, descending in reversed(order):
        key = key_for(column)
        present = [r for r in out if key(r) is not None]
        missing = [r for r in out if key(r) is None]
        try:
            present.sort(key=key, reverse=descending)
        except TypeError:
            present.sort(key=lambda r: str(key(r)), reverse=descending)
        out = present + missing
    return out
