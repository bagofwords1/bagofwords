"""Incremental refresh: the connector-agnostic core.

A custom table refreshed incrementally fetches only the rows at or after a
stored **checkpoint** instead of the whole result. Everything that decides
*what* to fetch lives here and never looks at SQL text or a driver:

    checkpoint   the highest cursor value our copy holds, as a typed value
    lookback     how far before the checkpoint to re-read (late rows)
    window       [lo, hi) over the cursor column — one for an incremental
                 run, many for a first load done in chunks

The only connector-specific step is the last one: writing a typed window into
the source's own query language (`render`). That is a table of literal formats,
not a code path per connector, and it is applied to a value we produced
ourselves (a datetime, a date, a number) — never to text the source handed us,
except for the narrow text-cursor case which is charset-checked.

Boundary rule, used everywhere: the lower bound is **inclusive**. Re-reading
the rows that sit exactly on the checkpoint is what makes ties safe (two rows
sharing a timestamp, one committed after we looked); the write step is
idempotent, so a re-read row is never a duplicate.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

MODE_FULL = "full"        # every refresh rebuilds the whole table (the default)
MODE_APPEND = "append"    # no key: rows at/after the window start are replaced
MODE_MERGE = "merge"      # unique key: rows are upserted, newest wins
MODES = (MODE_FULL, MODE_APPEND, MODE_MERGE)

KIND_TIMESTAMP = "timestamp"
KIND_DATE = "date"
KIND_NUMBER = "number"
KIND_TEXT = "text"
KINDS = (KIND_TIMESTAMP, KIND_DATE, KIND_NUMBER, KIND_TEXT)

GRAINS = ("hour", "day", "month")

# Rows per chunk a first load aims for. A chunk is one query on the source and
# one unit of resumable progress, so this trades query length against count.
CHUNK_TARGET_ROWS = 250_000
MAX_CHUNKS = 400

DEFAULT_LOOKBACK_SECONDS = 86_400       # timestamps and dates
DEFAULT_FULL_REFRESH_DAYS = 7
DEFAULT_ROW_DROP_GUARD_PCT = 50

_SINCE = re.compile(r"\{\{\s*since\s*\}\}", re.IGNORECASE)
_UNTIL = re.compile(r"\{\{\s*until\s*\}\}", re.IGNORECASE)

# A text cursor's value came from the source and is written back into a query,
# so it is only ever accepted when it is made of characters that cannot end a
# string literal in any dialect.
_SAFE_TEXT = re.compile(r"^[0-9A-Za-z:\-+. _/TZ]*$")


class IncrementalConfigError(ValueError):
    """The table's incremental settings cannot work as written."""


# --------------------------------------------------------------------------
# Typed cursor values
# --------------------------------------------------------------------------

def kind_of(value: Any) -> str | None:
    """Which cursor kind a value read from the source or our copy is."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return KIND_TIMESTAMP
    if isinstance(value, date):
        return KIND_DATE
    if isinstance(value, (int, Decimal)):
        return KIND_NUMBER
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else KIND_NUMBER
    if isinstance(value, str):
        return KIND_TEXT
    return None


def encode(value: Any) -> dict | None:
    """A cursor value as JSON that decodes back to the same typed value."""
    kind = kind_of(value)
    if kind is None:
        return None
    if kind == KIND_TIMESTAMP:
        aware = value.tzinfo is not None
        if aware:
            value = value.astimezone(UTC)
        return {"kind": kind, "value": value.replace(tzinfo=None).isoformat(timespec="microseconds"),
                "tz": aware}
    if kind == KIND_DATE:
        return {"kind": kind, "value": value.isoformat()}
    if kind == KIND_NUMBER:
        return {"kind": kind, "value": str(value)}
    return {"kind": kind, "value": value}


def decode(data: Any) -> Any:
    """The typed value `encode` wrote, or None for anything unreadable."""
    if not isinstance(data, dict) or data.get("value") is None:
        return None
    kind, raw = data.get("kind"), data["value"]
    try:
        if kind == KIND_TIMESTAMP:
            parsed = datetime.fromisoformat(str(raw))
            if data.get("tz"):
                return parsed.replace(tzinfo=UTC)
            return parsed.replace(tzinfo=None)
        if kind == KIND_DATE:
            return date.fromisoformat(str(raw))
        if kind == KIND_NUMBER:
            number = Decimal(str(raw))
            return int(number) if number == number.to_integral_value() else number
        if kind == KIND_TEXT:
            return str(raw)
    except (ValueError, ArithmeticError):
        return None
    return None


def parse_initial(kind: str | None, raw: Any) -> Any:
    """An admin-typed start value (`2024-01-01`, `1000`) as a typed value.

    With no cursor type chosen, the value says what it is: a number is a
    number, anything that reads as a date or time is a timestamp.
    """
    if raw in (None, ""):
        return None
    text = str(raw).strip()

    def number():
        value = Decimal(text)
        return int(value) if value == value.to_integral_value() else value

    def timestamp():
        return datetime.fromisoformat(text.replace("Z", "+00:00"))

    try:
        if kind == KIND_TIMESTAMP:
            return timestamp()
        if kind == KIND_DATE:
            return date.fromisoformat(text[:10])
        if kind == KIND_NUMBER:
            return number()
        if kind == KIND_TEXT:
            return text
        for reader in (number, timestamp):
            try:
                return reader()
            except (ValueError, ArithmeticError):
                continue
        return text
    except (ValueError, ArithmeticError):
        raise IncrementalConfigError(f"'{raw}' is not a valid {kind} start value.") from None


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass
class RefreshConfig:
    """How one custom table refreshes. Stored as JSON on the row."""

    mode: str = MODE_FULL
    cursor_column: str | None = None
    # Only needed before the first load when the query carries placeholders;
    # afterwards the kind is read off the data itself.
    cursor_kind: str | None = None
    unique_key: tuple = ()
    lookback_seconds: int | None = None      # timestamps / dates
    lookback_units: int | None = None        # numeric cursors
    initial_value: str | None = None
    grain: str | None = None                 # align the window start (rollups)
    full_refresh_every_days: int | None = DEFAULT_FULL_REFRESH_DAYS
    # Refuse to publish when the row count falls by more than this share.
    # None turns the guard off (tables that legitimately shrink).
    row_drop_guard_pct: int | None = None

    @property
    def incremental(self) -> bool:
        return self.mode in (MODE_APPEND, MODE_MERGE)

    @classmethod
    def from_row(cls, row) -> "RefreshConfig":
        raw = getattr(row, "refresh_config", None)
        raw = raw if isinstance(raw, dict) else {}
        mode = getattr(row, "refresh_mode", None) or MODE_FULL
        return cls.from_dict({**raw, "mode": mode})

    @classmethod
    def from_dict(cls, raw: dict | None) -> "RefreshConfig":
        raw = raw or {}
        key = raw.get("unique_key") or ()
        if isinstance(key, str):
            key = [k.strip() for k in key.split(",")]
        return cls(
            mode=raw.get("mode") or MODE_FULL,
            cursor_column=(raw.get("cursor_column") or None),
            cursor_kind=(raw.get("cursor_kind") or None),
            unique_key=tuple(k for k in key if k),
            lookback_seconds=_int_or_none(raw.get("lookback_seconds")),
            lookback_units=_int_or_none(raw.get("lookback_units")),
            initial_value=(str(raw["initial_value"]) if raw.get("initial_value") not in (None, "") else None),
            grain=(raw.get("grain") or None),
            full_refresh_every_days=_int_or_none(
                raw.get("full_refresh_every_days", DEFAULT_FULL_REFRESH_DAYS)),
            row_drop_guard_pct=_int_or_none(raw.get("row_drop_guard_pct")),
        )

    def to_dict(self) -> dict:
        """The JSON stored beside `refresh_mode` (which is its own column)."""
        return {
            "cursor_column": self.cursor_column,
            "cursor_kind": self.cursor_kind,
            "unique_key": list(self.unique_key),
            "lookback_seconds": self.lookback_seconds,
            "lookback_units": self.lookback_units,
            "initial_value": self.initial_value,
            "grain": self.grain,
            "full_refresh_every_days": self.full_refresh_every_days,
            "row_drop_guard_pct": self.row_drop_guard_pct,
        }

    def validate(self, sql: str = "") -> None:
        """Reject settings that cannot mean what they say. Raises on the first."""
        if self.mode not in MODES:
            raise IncrementalConfigError(f"Unknown refresh mode '{self.mode}'.")
        if self.cursor_kind and self.cursor_kind not in KINDS:
            raise IncrementalConfigError(f"Unknown cursor type '{self.cursor_kind}'.")
        if self.grain and self.grain not in GRAINS:
            raise IncrementalConfigError(f"Unknown grain '{self.grain}'.")
        for name, value in (("Lookback", self.lookback_seconds), ("Lookback", self.lookback_units),
                            ("Full refresh interval", self.full_refresh_every_days)):
            if value is not None and value < 0:
                raise IncrementalConfigError(f"{name} cannot be negative.")
        if self.row_drop_guard_pct is not None and not 1 <= self.row_drop_guard_pct <= 100:
            raise IncrementalConfigError("The row-drop guard must be between 1 and 100 percent.")
        if not self.incremental:
            return
        if not self.cursor_column:
            raise IncrementalConfigError(
                "Incremental refresh needs a cursor column: a date, timestamp or "
                "ever-increasing number that grows when a row is added or changed."
            )
        if self.mode == MODE_MERGE and not self.unique_key:
            raise IncrementalConfigError(
                "Merge needs a unique key so a changed row replaces its earlier "
                "version. Without a key, use append."
            )
        if self.mode == MODE_APPEND and self.unique_key:
            raise IncrementalConfigError("A unique key is only used by merge.")
        if uses_placeholders(sql) and not self.cursor_kind:
            raise IncrementalConfigError(
                "This query uses {{since}} / {{until}}, so the cursor type "
                "(timestamp, date or number) has to be chosen."
            )
        if self.cursor_kind == KIND_TEXT and _UNTIL.search(sql or ""):
            raise IncrementalConfigError(
                "{{until}} is not available for a text cursor; use {{since}} only."
            )


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def definition_hash(sql: str, target: dict | None, config: RefreshConfig) -> str:
    """Fingerprint of everything that decides which rows belong in the table.

    A stored checkpoint only means something for the definition it was taken
    under; when this changes the next refresh starts from scratch.
    """
    payload = json.dumps(
        {
            "sql": (sql or "").strip(),
            "target": target or None,
            "mode": config.mode,
            "cursor": config.cursor_column,
            "kind": config.cursor_kind,
            "key": list(config.unique_key),
            "initial": config.initial_value,
            "grain": config.grain,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """Rows whose cursor is in [lo, hi). None means unbounded on that side."""

    lo: Any = None
    hi: Any = None
    # Rows with no cursor value fall outside every range. Where we write the
    # predicate ourselves they ride along with the open-ended window, so they
    # are fetched on every run rather than silently never.
    include_nulls: bool = False

    def to_json(self) -> dict:
        return {"lo": encode(self.lo), "hi": encode(self.hi), "include_nulls": self.include_nulls}

    @classmethod
    def from_json(cls, data: dict) -> "Window":
        data = data or {}
        return cls(decode(data.get("lo")), decode(data.get("hi")), bool(data.get("include_nulls")))


def lookback_of(config: RefreshConfig, kind: str):
    """The configured lookback in the cursor's own unit."""
    if kind in (KIND_TIMESTAMP, KIND_DATE):
        seconds = config.lookback_seconds
        if seconds is None:
            seconds = DEFAULT_LOOKBACK_SECONDS
        if kind == KIND_DATE:
            return timedelta(days=math.ceil(seconds / 86_400))
        return timedelta(seconds=seconds)
    if kind == KIND_NUMBER:
        return config.lookback_units or 0
    return None


def truncate(value: Any, grain: str | None) -> Any:
    """`value` moved back to the start of its hour / day / month."""
    if not grain or value is None:
        return value
    if isinstance(value, datetime):
        if grain == "hour":
            return value.replace(minute=0, second=0, microsecond=0)
        if grain == "day":
            return value.replace(hour=0, minute=0, second=0, microsecond=0)
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if isinstance(value, date):
        return value.replace(day=1) if grain == "month" else value
    return value


def incremental_window(checkpoint: Any, config: RefreshConfig) -> Window:
    """What one incremental run reads: from the checkpoint, minus the lookback.

    The lookback is applied here, at read time, and never stored: the
    checkpoint itself only ever moves forward.
    """
    kind = kind_of(checkpoint)
    lo = checkpoint
    back = lookback_of(config, kind) if kind else None
    if back:
        try:
            lo = checkpoint - back
        except (OverflowError, TypeError):
            lo = checkpoint
    return Window(lo=truncate(lo, config.grain), hi=None, include_nulls=True)


def cap_checkpoint(observed: Any, now_utc: datetime) -> Any:
    """Never let the checkpoint run ahead of the present.

    One row dated in the future would otherwise carry the checkpoint past all
    real data, and every later run would read nothing.
    """
    if isinstance(observed, datetime):
        if observed.tzinfo is not None:
            limit = now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=UTC)
        else:
            limit = now_utc.replace(tzinfo=None)
        return min(observed, limit)
    if isinstance(observed, date):
        return min(observed, now_utc.date() + timedelta(days=1))
    return observed


def plan_chunks(lo: Any, hi: Any, total_rows: int, *, target_rows: int | None = None,
                max_chunks: int = MAX_CHUNKS, include_nulls: bool = True) -> list[Window]:
    """Split a first load over [lo, hi] into windows of roughly `target_rows`.

    The windows partition the range: each is half-open and the last is
    open-ended, so a row is in exactly one of them and rows that arrive above
    `hi` while the load runs are still picked up by the final window.
    """
    last = Window(lo=None, hi=None, include_nulls=include_nulls)
    kind = kind_of(lo)
    if lo is None or hi is None or kind != kind_of(hi) or kind == KIND_TEXT or not total_rows:
        return [last]
    count = max(1, min(max_chunks, math.ceil(total_rows / max(1, target_rows or CHUNK_TARGET_ROWS))))
    if count == 1 or lo >= hi:
        return [last]

    edges = _edges(lo, hi, count, kind)
    if len(edges) < 3:
        return [last]
    windows = []
    for i in range(len(edges) - 1):
        first, final = i == 0, i == len(edges) - 2
        windows.append(Window(
            lo=None if first else edges[i],
            hi=None if final else edges[i + 1],
            include_nulls=include_nulls and final,
        ))
    return windows


def midpoint(lo: Any, hi: Any) -> Any:
    """A value strictly between two cursor values, or None if there is none."""
    kind = kind_of(lo)
    if kind is None or kind != kind_of(hi) or kind == KIND_TEXT:
        return None
    try:
        if kind == KIND_TIMESTAMP:
            mid = lo + (hi - lo) / 2
        elif kind == KIND_DATE:
            mid = lo + timedelta(days=(hi - lo).days // 2)
        elif isinstance(lo, int) and isinstance(hi, int):
            mid = (lo + hi) // 2
        else:
            mid = (Decimal(str(lo)) + Decimal(str(hi))) / 2
    except (OverflowError, ArithmeticError, TypeError):
        return None
    return mid if lo < mid < hi else None


def plan_by_density(lo: Any, hi: Any, total_rows: int, count_between, *,
                    target_rows: int | None = None, max_chunks: int = MAX_CHUNKS,
                    max_probes: int = 64, include_nulls: bool = True) -> list[Window]:
    """Cut a first load into windows of roughly `target_rows`, by where the
    rows actually are.

    Cutting the range evenly only works when rows are spread evenly, and real
    cursor columns are not: most rows are recent, and a single row stamped
    1970 or 2099 stretches the range until one "chunk" holds the whole table.
    So the busiest window is halved, its halves counted (`count_between(lo,
    hi)` is rows with lo <= cursor < hi), and again, until none is over the
    target or the probe budget is spent. Neighbours that are nearly empty are
    then merged back together.
    """
    target = max(1, target_rows or CHUNK_TARGET_ROWS)
    last = Window(lo=None, hi=None, include_nulls=include_nulls)
    if lo is None or hi is None or not total_rows or total_rows <= target or midpoint(lo, hi) is None:
        return [last]

    # Segments are [a, b) with a known row count; the final one also holds `hi`.
    segments = [[lo, hi, total_rows, True]]
    probes = 0
    while probes < max_probes and len(segments) < max_chunks:
        index = max(range(len(segments)), key=lambda i: segments[i][2] if segments[i][3] else -1)
        a, b, n, splittable = segments[index]
        if not splittable or n <= target:
            break
        mid = midpoint(a, b)
        if mid is None:
            segments[index][3] = False      # every row shares one value: cannot be cut
            continue
        probes += 1
        left = int(count_between(a, mid))
        left = max(0, min(n, left))
        segments[index:index + 1] = [[a, mid, left, True], [mid, b, n - left, True]]

    merged = [segments[0][:3]]
    for a, b, n, _s in segments[1:]:
        if merged[-1][2] + n <= target:
            merged[-1][1], merged[-1][2] = b, merged[-1][2] + n
        else:
            merged.append([a, b, n])
    if len(merged) == 1:
        return [last]
    windows = []
    for i, (a, b, _n) in enumerate(merged):
        first, final = i == 0, i == len(merged) - 1
        windows.append(Window(lo=None if first else a, hi=None if final else b,
                              include_nulls=include_nulls and final))
    return windows


def can_bound_above(sql: str) -> bool:
    """Whether a window's upper bound can be written into this query.

    A query that places only `{{since}}` has nowhere to put one, so it cannot
    be loaded in windows: each would return everything above its start.
    """
    return not uses_placeholders(sql) or bool(_UNTIL.search(sql or ""))


def _edges(lo, hi, count: int, kind: str) -> list:
    """`count + 1` strictly increasing boundaries from lo to hi (fewer if the
    range is too narrow to cut that finely)."""
    edges = [lo]
    if kind == KIND_TIMESTAMP:
        step = (hi - lo) / count
        if step <= timedelta(0):
            return edges
        for i in range(1, count):
            edges.append(lo + step * i)
    elif kind == KIND_DATE:
        days = (hi - lo).days
        for i in range(1, count):
            edges.append(lo + timedelta(days=(days * i) // count))
    else:
        span = Decimal(str(hi)) - Decimal(str(lo))
        for i in range(1, count):
            edge = Decimal(str(lo)) + (span * i) / count
            edges.append(int(edge) if isinstance(lo, int) and isinstance(hi, int) else edge)
    edges.append(hi)
    unique = [edges[0]]
    for edge in edges[1:]:
        if edge > unique[-1]:
            unique.append(edge)
    return unique


# --------------------------------------------------------------------------
# Writing a window into the source's query language
# --------------------------------------------------------------------------

# Dialect names are the ones `sql_guard.CLIENT_DIALECTS` already uses, folded
# into the handful of literal families that actually differ.
_FAMILY = {
    "postgres": "postgres", "postgresql": "postgres", "redshift": "postgres", "duckdb": "postgres",
    "mysql": "mysql", "mariadb": "mysql",
    "tsql": "tsql", "mssql": "tsql", "fabric": "tsql",
    "oracle": "oracle",
    "sqlite": "sqlite",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "clickhouse": "clickhouse",
    "trino": "trino", "presto": "trino", "athena": "trino",
    "databricks": "spark", "spark": "spark",
    # HogQL (PostHog) is ClickHouse SQL with its own function set.
    "hogql": "hogql",
    "sybase": "sybase",
}

# Families exercised against a live engine end to end. The rest render from
# the same table and are unit-tested, but have not met a real server.
VERIFIED_FAMILIES = {"postgres", "tsql", "sqlite", "mysql"}

# Dialects that want the keyword before a subquery's alias. Everywhere else it
# is left out, because Oracle rejects it.
_ALIAS_WITH_AS = {"hogql", "clickhouse"}


def _alias(family: str, name: str) -> str:
    return f"AS {name}" if family in _ALIAS_WITH_AS else name

_MIN = {KIND_TIMESTAMP: datetime(1, 1, 1), KIND_DATE: date(1, 1, 1),
        KIND_NUMBER: -(2 ** 63), KIND_TEXT: ""}
_MAX = {KIND_TIMESTAMP: datetime(9999, 12, 31), KIND_DATE: date(9999, 12, 31),
        KIND_NUMBER: 2 ** 63 - 1}
# Engines whose date types cannot hold year 1.
_FAMILY_MIN = {
    "clickhouse": {KIND_TIMESTAMP: datetime(1970, 1, 1), KIND_DATE: date(1970, 1, 1)},
    "hogql": {KIND_TIMESTAMP: datetime(1970, 1, 1), KIND_DATE: date(1970, 1, 1)},
    "sybase": {KIND_TIMESTAMP: datetime(1753, 1, 1), KIND_DATE: date(1753, 1, 1)},
    "tsql": {KIND_TIMESTAMP: datetime(1753, 1, 1), KIND_DATE: date(1753, 1, 1)},
    "mysql": {KIND_TIMESTAMP: datetime(1000, 1, 1), KIND_DATE: date(1000, 1, 1)},
}


def family_of(dialect: str | None) -> str | None:
    return _FAMILY.get((dialect or "").lower())


def dialect_for(client) -> str | None:
    """The SQL dialect a client's queries are written in, if it speaks SQL.

    A client whose query language is SQL-shaped but is not driven through the
    shared dialect table (PostHog's HogQL, SQL Anywhere) says so itself with
    `INCREMENTAL_DIALECT`.
    """
    declared = getattr(client, "INCREMENTAL_DIALECT", None)
    if declared:
        return str(declared)
    from app.ai.code_execution.sql_guard import CLIENT_DIALECTS
    from app.data_sources.fast import sql_dialect

    return CLIENT_DIALECTS.get(type(client).__name__) or sql_dialect.dialect_of(client) or None


def window_writer(client):
    """A client's own way of writing a window into its query language, for
    the ones that are neither SQL nor a JSON request (SOQL, DAX):
    ``render_window(query, column, kind, window) -> query``."""
    writer = getattr(client, "render_window", None)
    return writer if callable(writer) else None


def uses_placeholders(sql: str | None) -> bool:
    return bool(_SINCE.search(sql or "") or _UNTIL.search(sql or ""))


def filters_at_source(client) -> bool:
    """Whether a window can be written into this client's query.

    When it can, a refresh fetches only the window. When it cannot, the query
    runs whole and the window is taken from its result locally: the table is
    kept just the same, but the source does the full work every time.
    """
    return (json_window_of(client) is not None or window_writer(client) is not None
            or family_of(dialect_for(client)) is not None)


def can_window(client, sql: str) -> bool:
    return filters_at_source(client)


def quote_identifier(family: str, name: str) -> str:
    if family in ("mysql", "bigquery", "clickhouse", "spark", "hogql"):
        return "`" + name.replace("`", "``") + "`"
    if family == "tsql":
        return "[" + name.replace("]", "]]") + "]"
    if family == "oracle" and re.fullmatch(r"[a-z_][a-z0-9_$#]*", name):
        # The driver lower-cases Oracle's upper-case names; quoting that
        # spelling would look for a column that does not exist.
        return name
    return '"' + name.replace('"', '""') + '"'


def literal(family: str, value: Any) -> str:
    """`value` as a literal of its own type in `family`'s SQL."""
    kind = kind_of(value)
    if kind == KIND_NUMBER:
        return format(value, "f") if isinstance(value, Decimal) else repr(value)
    if kind == KIND_TEXT:
        if not _SAFE_TEXT.match(value):
            raise IncrementalConfigError(
                "The cursor column holds text that cannot be used as a checkpoint "
                "safely. Use a date, timestamp or numeric column."
            )
        return f"'{value}'"
    if kind == KIND_DATE:
        text = value.isoformat()
        if family == "tsql":
            return f"CAST('{text}' AS date)"
        if family == "sqlite":
            return f"'{text}'"
        if family == "clickhouse":
            return f"toDate('{text}')"
        if family == "hogql":
            return f"toDate('{text}')"
        if family == "sybase":
            return f"CAST('{text}' AS DATE)"
        return f"DATE '{text}'"
    if kind != KIND_TIMESTAMP:
        raise IncrementalConfigError("Unsupported cursor value.")

    aware = value.tzinfo is not None
    if aware:
        value = value.astimezone(UTC).replace(tzinfo=None)
    # isoformat, not strftime: strftime does not zero-pad a year below 1000,
    # and an engine reads '1-01-01' as 2001.
    text = value.isoformat(sep=" ", timespec="microseconds")
    if family == "tsql":
        iso = value.isoformat(sep="T", timespec="microseconds")
        return (f"CAST('{iso}+00:00' AS datetimeoffset)" if aware
                else f"CAST('{iso}' AS datetime2)")
    if family == "sqlite":
        return f"'{text}'"
    if family == "clickhouse":
        return f"toDateTime64('{text}', 6, 'UTC')" if aware else f"toDateTime64('{text}', 6)"
    if family == "hogql":
        # Whole seconds: rounding a lower bound down only re-reads a little.
        whole = value.replace(microsecond=0).isoformat(sep=" ")
        return f"toDateTime('{whole}', 'UTC')" if aware else f"toDateTime('{whole}')"
    if family == "sybase":
        return f"CAST('{text}' AS TIMESTAMP)"
    if family == "bigquery":
        return f"TIMESTAMP '{text}+00:00'" if aware else f"DATETIME '{text}'"
    if family == "postgres":
        return f"TIMESTAMPTZ '{text}+00:00'" if aware else f"TIMESTAMP '{text}'"
    if family == "oracle":
        return f"TIMESTAMP '{text} +00:00'" if aware else f"TIMESTAMP '{text}'"
    if family == "trino":
        return f"TIMESTAMP '{text} UTC'" if aware else f"TIMESTAMP '{text}'"
    if family == "snowflake":
        return f"'{text} +00:00'::TIMESTAMP_TZ" if aware else f"'{text}'::TIMESTAMP_NTZ"
    return f"TIMESTAMP '{text}'"


def _sentinel(family: str, kind: str, top: bool, like: Any = None) -> Any:
    if top:
        value = _MAX.get(kind)
    else:
        value = _FAMILY_MIN.get(family, {}).get(kind, _MIN.get(kind))
    if isinstance(value, datetime) and isinstance(like, datetime) and like.tzinfo is not None:
        value = value.replace(tzinfo=UTC)
    return value


def render_sql(sql: str, dialect: str | None, column: str, kind: str | None,
               window: Window) -> str:
    """The admin's query restricted to `window`, in the source's own SQL.

    Two shapes. A query that carries `{{since}}` / `{{until}}` has them filled
    in — the admin chose where the filter goes, so it lands on the indexed
    base column. A query without them is wrapped, which is convenient but only
    as fast as the source's ability to push a filter into a subquery.
    """
    family = family_of(dialect)
    if family is None:
        raise IncrementalConfigError("Incremental refresh is not available for this connection type.")
    from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

    if uses_placeholders(sql):
        kind = kind or kind_of(window.lo) or kind_of(window.hi)
        if kind is None:
            raise IncrementalConfigError("The cursor type has to be chosen for a query using {{since}}.")
        like = window.lo if window.lo is not None else window.hi
        lo = window.lo if window.lo is not None else _sentinel(family, kind, False, like)
        hi = window.hi if window.hi is not None else _sentinel(family, kind, True, like)
        out = _SINCE.sub(lambda _m: literal(family, lo), sql)
        if _UNTIL.search(out):
            if hi is None:
                raise IncrementalConfigError("{{until}} is not available for this cursor type.")
            out = _UNTIL.sub(lambda _m: literal(family, hi), out)
        return out

    inner = strip_trailing_semicolon(sql)
    col = quote_identifier(family, column)
    parts = []
    if window.lo is not None:
        parts.append(f"{col} >= {literal(family, window.lo)}")
    if window.hi is not None:
        parts.append(f"{col} < {literal(family, window.hi)}")
    if not parts:
        return inner
    predicate = " AND ".join(parts)
    if window.include_nulls:
        predicate = f"({predicate}) OR {col} IS NULL"
    return f"SELECT * FROM ({inner}) {_alias(family, 'bow_sub')} WHERE {predicate}"


def count_sql(sql: str, dialect: str | None, column: str, kind: str | None,
              window: Window) -> str | None:
    """How many rows of the result fall in `window`, as one statement."""
    family = family_of(dialect)
    if family is None:
        return None
    from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

    inner = strip_trailing_semicolon(render_sql(sql, dialect, column, kind, window))
    return f"SELECT COUNT(*) FROM ({inner}) {_alias(family, 'bow_cnt')}"


def probe_sql(sql: str, dialect: str | None, column: str, kind: str | None,
              window: "Window | None" = None) -> str | None:
    """One aggregate over the result: min, max, rows, rows with a cursor.

    It sizes a first load before any rows move — over `window` when the load
    has a start value, so rows it will never fetch are not counted against it.
    None when the source cannot aggregate over a subquery, in which case the
    load is a single window.
    """
    family = family_of(dialect)
    if family is None:
        return None
    from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

    inner = strip_trailing_semicolon(render_sql(sql, dialect, column, kind, window or Window()))
    col = quote_identifier(family, column)
    return f"SELECT MIN({col}), MAX({col}), COUNT(*), COUNT({col}) FROM ({inner}) {_alias(family, 'bow_sub')}"


# --------------------------------------------------------------------------
# Connectors that take a JSON request instead of SQL
# --------------------------------------------------------------------------

def json_window_of(client) -> dict | None:
    """A client's declared window fields, e.g.
    ``{"start": "start_time", "end": "end_time", "format": "epoch_ms"}``."""
    decl = getattr(client, "INCREMENTAL_WINDOW", None)
    return decl if isinstance(decl, dict) and decl.get("start") else None


def json_bucket(decl: dict, body: dict) -> int | None:
    """The width of one result row's time bucket for this request, in the
    window's own unit, when the connector rolls points up (else None)."""
    bucket = decl.get("bucket")
    if callable(bucket):
        try:
            bucket = bucket(body)
        except Exception:
            return None
    return int(bucket) if isinstance(bucket, (int, float)) and bucket > 0 else None


def align_window(client, spec: str, window: Window, config: "RefreshConfig") -> Window:
    """Start an incremental window on a whole bucket, at least one bucket back.

    A connector that rolls points up returns one row per bucket. Reading from
    the middle of a bucket would rebuild that row from part of its points, and
    the newest bucket is still filling when it is first read — so the window
    starts on a boundary and always re-reads the last complete-looking bucket.
    """
    decl = json_window_of(client)
    if decl is None or not isinstance(window.lo, int) or isinstance(window.lo, bool):
        return window
    try:
        body = json.loads(spec) if isinstance(spec, str) else dict(spec)
    except (TypeError, ValueError):
        return window
    bucket = json_bucket(decl, body if isinstance(body, dict) else {})
    if not bucket:
        return window
    lookback = config.lookback_units or 0
    lo = window.lo + lookback - max(lookback, bucket)
    return Window(lo=lo // bucket * bucket, hi=window.hi, include_nulls=window.include_nulls)


def _json_value(value: Any, fmt: str):
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        if fmt == "epoch_ms":
            return int(aware.timestamp() * 1000)
        if fmt == "epoch_s":
            return int(aware.timestamp())
        return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return _json_value(datetime(value.year, value.month, value.day), fmt)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def render_json(spec: str, decl: dict, window: Window, now_utc: datetime | None = None) -> str:
    """The admin's JSON request with the window written into its own fields."""
    try:
        body = json.loads(spec) if isinstance(spec, str) else dict(spec)
    except (TypeError, ValueError):
        raise IncrementalConfigError("This connection's query must be a JSON object.")
    if not isinstance(body, dict):
        raise IncrementalConfigError("This connection's query must be a JSON object.")
    fmt = decl.get("format") or "iso"
    if window.lo is None and window.hi is None:
        return json.dumps(body)
    # Fields that would contradict an explicit window (a relative duration).
    for field in decl.get("clears") or ():
        body.pop(field, None)
    if window.lo is not None:
        body[decl["start"]] = _json_value(window.lo, fmt)
    end = window.hi if window.hi is not None else (now_utc or datetime.now(UTC))
    if decl.get("end"):
        body[decl["end"]] = _json_value(end, fmt)
    return json.dumps(body)


def render(client, sql: str, column: str, kind: str | None, window: Window) -> str:
    """`sql` restricted to `window`, however this client expresses that.

    A client with no way to express it gets its query back unchanged: the
    caller then takes the window from the result (see `filters_at_source`).
    """
    decl = json_window_of(client)
    if decl is not None:
        return render_json(sql, decl, window)
    writer = window_writer(client)
    if writer is not None:
        return writer(sql, column, kind, window)
    if family_of(dialect_for(client)) is not None:
        return render_sql(sql, dialect_for(client), column, kind, window)
    if uses_placeholders(sql):
        raise IncrementalConfigError(
            "{{since}} / {{until}} are not available for this connection type: its "
            "queries cannot be filtered at the source. Remove them and choose a "
            "cursor column instead."
        )
    return sql
