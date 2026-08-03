"""Materializing from PostHog through its HogQL query API.

PostHog is the first accelerated source that is not a database connection at
all. There is no cursor and no session: every read is one HTTP POST to
`/api/projects/{id}/query/` that compiles a HogQL statement, runs it on
ClickHouse, and returns a *bounded* result as JSON. Everything below follows
from two measured facts about that endpoint.

**One response carries at most 50,000 rows, and says nothing when it truncates.**
Asking for 100,000 returns 50,000 with no flag, no error and no `hasMore` — a
naive extraction would materialize a silently incomplete relation, which is the
worst outcome available here, since nothing about it looks wrong afterwards.

**`OFFSET` is rejected outright.** Not slow, not discouraged — the API refuses
it for personal API keys, which is the only credential this connector has:

    OFFSET is not supported on queries made with a personal API key. For
    pagination, use keyset pagination on the `timestamp` column […] For bulk
    data extraction, use batch exports.

So the obvious `LIMIT n OFFSET m` loop is unavailable, and the three answers
this protocol asks for are built out of what remains.

**Estimating — an exact count, not a guess.** `SELECT count() FROM (<sql>)`
returns the true row count for about the cost of one aggregate. This is the
one source whose estimate is exact rather than a planner's opinion, and it is
worth the query for a second reason: it is what makes truncation *detectable*.

  This does run the admin's query, which the protocol elsewhere promises not
  to do. That promise exists to keep a save-time click from costing a scan on a
  loaded server; PostHog has no planner to ask instead, and a count over a
  columnar store transfers one row. Trading that for the ability to prove an
  extraction is complete is the right side of the deal.

**Extracting — half-open cursor windows, verified against the count.** Under
50,000 rows the whole result is one request. Above it, the extraction is split
into windows over a datetime column — `[lo, hi)`, `[hi, …)`, with only the
final window closed at the top. Windows *partition* the rows: every row falls
in exactly one, so unlike the keyset `>` the error message suggests, a tie on
the cursor can neither be skipped nor duplicated. (Ties are not hypothetical:
the two most recent events in the project this was verified against share a
timestamp to the microsecond.) A window that comes back at the 50,000 ceiling
is ambiguous, so it is split and retried rather than trusted.

Then the total is compared against the count, and a shortfall aborts the
refresh. That check is what makes the whole strategy safe to rely on: if a
window is ever mis-drawn, or the admin's query returns different rows on each
run, the result is a failed refresh that keeps serving the previous artifact —
never a relation that is quietly missing rows.

  A windowed extraction re-runs the admin's query once per window, so it costs
  one scan per window at the source. That is the price of an API with no
  cursor, and the count check is what bounds it.

**Cancelling — nothing to cancel.** By the time the extractor decides to stop,
the request it was reading has already been served. Aborting means not asking
for the next window.
"""

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Rows a single HogQL response will carry. Measured against a live project:
# asking for 100,000 returns exactly 50,000, silently. Requests are made at
# this size, and a response that reaches it is treated as unproven rather than
# complete.
SINGLE_RESPONSE_ROWS = 50_000

# A windowed extraction that needs more requests than this has stopped
# converging (a cursor with no spread, a non-deterministic query). Bounded so a
# refresh fails with a reason rather than hammering the project.
MAX_WINDOW_REQUESTS = 512

# Finest split a window can be cut to. Below this the cursor cannot separate
# rows and no further split can help.
MIN_WINDOW = timedelta(microseconds=1)


class PostHogSource:
    """Extraction over PostHog's HogQL query endpoint."""

    def __init__(self, client):
        self.client = client

    # -- the protocol -----------------------------------------------------

    def estimate(self, sql: str):
        """The exact row count, and whether this query can be materialized.

        Never raises: a failure here downgrades to `supported=False` and the
        extractor falls back to its hard caps, same as every other source.
        """
        from app.data_sources.fast.extractor import Estimate
        from app.data_sources.fast.sql_dialect import ASSUMED_ROW_WIDTH_BYTES

        try:
            rows = self._count(sql)
        except Exception as e:
            return Estimate(supported=False, note=f"PostHog count() failed: {e}")

        est = Estimate(
            rows=rows,
            width_bytes=ASSUMED_ROW_WIDTH_BYTES,
            total_bytes=rows * ASSUMED_ROW_WIDTH_BYTES,
            note="exact row count from count(); row width estimated",
        )
        if rows > SINGLE_RESPONSE_ROWS and self._cursor_column(sql) is None:
            # Nothing to window on, and one response cannot carry the result.
            # Refusing at save time beats discovering it on the first refresh.
            est.source_max_rows = SINGLE_RESPONSE_ROWS
            est.source_max_rows_note = (
                f"PostHog returns at most {SINGLE_RESPONSE_ROWS:,} rows per query and "
                f"cannot page through more, unless the result carries a date or "
                f"timestamp column to extract it in windows. Add one to the SELECT, "
                f"aggregate or filter the query, or use PostHog batch exports for "
                f"bulk data."
            )
        return est

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        """First `limit` rows — a single bounded request, never the full result."""
        names, types, rows = self.client.run_hogql(bounded_hogql(sql, limit))
        return _columns(names, types), [list(r) for r in rows]

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Yield pyarrow.Table batches covering the whole result, or fail.

        The count is taken first and checked last. Between those two points the
        strategy can be one request or many windows; what the caller is
        promised is that a batch sequence which reaches the end is complete.
        """
        total = self._count(sql)
        schema = [None]

        if total <= SINGLE_RESPONSE_ROWS:
            names, types, rows = self._fetch(sql, None, None)
            _verify(len(rows), total)
            yield from _tables(names, types, rows, batch_rows, schema, allow_empty=True)
            return

        yield from self._windowed(sql, total, batch_rows, schema)

    # -- windowed extraction ----------------------------------------------

    def _windowed(self, sql: str, total: int, batch_rows: int, schema):
        cursor = self._cursor_column(sql)
        if cursor is None:
            raise RuntimeError(
                f"This query returns {total:,} rows and PostHog serves at most "
                f"{SINGLE_RESPONSE_ROWS:,} per request with no way to page through "
                f"the rest. Extracting it in windows needs a date or timestamp "
                f"column in the result; add one to the SELECT, or aggregate or "
                f"filter the query."
            )

        lo, hi, nulls = self._range(sql, cursor)
        if nulls:
            raise RuntimeError(
                f"{nulls:,} rows have no '{cursor}' value. A row with an empty "
                f"cursor falls outside every window and would be lost, so the "
                f"extraction is refused; filter them out or use another column."
            )
        if lo is None or hi is None:
            raise RuntimeError(f"Could not read the range of '{cursor}'.")

        emitted = 0
        requests = 0
        # (lo, hi, top_is_inclusive) — only the last window includes its top,
        # so the windows partition the range rather than overlapping at edges.
        # Processed newest-first off the end of the list; the order does not
        # matter, only that the set covers the range exactly once.
        windows = list(reversed(_initial_windows(lo, hi, total)))

        while windows:
            w_lo, w_hi, closed = windows.pop()
            requests += 1
            if requests > MAX_WINDOW_REQUESTS:
                raise RuntimeError(
                    f"Extraction did not converge after {MAX_WINDOW_REQUESTS} "
                    f"requests. This usually means the query returns different "
                    f"rows each time it runs — give it an ORDER BY, or drop a "
                    f"LIMIT that has no ORDER BY."
                )

            names, types, rows = self._fetch(sql, cursor, (w_lo, w_hi, closed))

            if len(rows) >= SINGLE_RESPONSE_ROWS:
                # At the ceiling: there may be more in this window and the
                # response cannot say. Split rather than trust it.
                mid = _midpoint(w_lo, w_hi)
                if mid is None:
                    raise RuntimeError(
                        f"More than {SINGLE_RESPONSE_ROWS:,} rows share a single "
                        f"'{cursor}' value, so the extraction cannot be split any "
                        f"further. Aggregate the query, or use PostHog batch exports."
                    )
                windows.append((mid, w_hi, closed))
                windows.append((w_lo, mid, False))
                continue

            emitted += len(rows)
            yield from _tables(names, types, rows, batch_rows, schema, allow_empty=False)

        _verify(emitted, total)

    # -- talking to the API -----------------------------------------------

    def _count(self, sql: str) -> int:
        from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

        inner = strip_trailing_semicolon(sql)
        _n, _t, rows = self.client.run_hogql(
            f"SELECT count() FROM ({inner}) AS bow_sub"
        )
        return int(rows[0][0]) if rows and rows[0] else 0

    def _range(self, sql: str, cursor: str):
        """(min, max, rows-with-no-value) for the cursor, in one query."""
        from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

        inner = strip_trailing_semicolon(sql)
        col = _identifier(cursor)
        _n, _t, rows = self.client.run_hogql(
            f"SELECT min({col}), max({col}), count() - count({col}) "
            f"FROM ({inner}) AS bow_sub"
        )
        lo, hi, nulls = rows[0]
        return _parse_dt(lo), _parse_dt(hi), int(nulls or 0)

    def _cursor_column(self, sql: str) -> Optional[str]:
        """A date/datetime column of the result to window over, if there is one.

        Chosen from the column *types* the API reports rather than from names,
        via a `LIMIT 0` probe that compiles the query without returning rows.
        `timestamp` wins when present because it is the events table's own
        ordering column and therefore the one with real spread.
        """
        try:
            names, types, _rows = self.client.run_hogql(bounded_hogql(sql, 0))
        except Exception:
            logger.debug("PostHog cursor probe failed", exc_info=True)
            return None

        dated = [
            str(c["name"]) for c in _columns(names, types)
            if c["dtype"] and "date" in c["dtype"].lower()
        ]
        if not dated:
            return None
        for name in dated:
            if name.lower() == "timestamp":
                return name
        return dated[0]

    def _fetch(self, sql: str, cursor: Optional[str], window):
        """One bounded request, optionally restricted to a cursor window."""
        from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

        inner = strip_trailing_semicolon(sql)
        where = ""
        if window is not None and cursor:
            w_lo, w_hi, closed = window
            col = _identifier(cursor)
            where = (
                f" WHERE {col} >= '{_fmt_dt(w_lo)}' "
                f"AND {col} {'<=' if closed else '<'} '{_fmt_dt(w_hi)}'"
            )
        return self.client.run_hogql(
            f"SELECT * FROM ({inner}) AS bow_sub{where} LIMIT {SINGLE_RESPONSE_ROWS}"
        )


def bounded_hogql(sql: str, limit: int) -> str:
    """The admin's SQL bounded to `limit` rows.

    Wrapping rather than appending: the statement goes inside a derived table
    untouched, so a query carrying its own `LIMIT` or `ORDER BY` keeps its
    meaning. PostHog leaves no alternative — an unbounded HogQL query is
    truncated to the server's default, so "send it as written" would mean
    materializing whatever the server felt like returning.
    """
    from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

    return f"SELECT * FROM ({strip_trailing_semicolon(sql)}) AS bow_sub LIMIT {int(limit)}"


def _initial_windows(lo: datetime, hi: datetime, total: int):
    """The windows to start from, sized by the count instead of guessed.

    Starting from the whole range means the first request is *guaranteed* to
    hit the ceiling and be thrown away, and so is the first half, and so on:
    extracting 181,000 rows that way spent five wasted 50,000-row responses
    before it found windows that fit. The count is already in hand, so the
    range is sliced up front for free.

    Slices aim at half the ceiling rather than at it, because event data is
    bursty and a slice sized exactly to the average would tip over on any busy
    stretch. Whatever still overflows is split by the loop as before — this
    only changes how many requests are wasted finding that out.
    """
    span = hi - lo
    target = max(1, SINGLE_RESPONSE_ROWS // 2)
    count = max(1, min(math.ceil(total / target), MAX_WINDOW_REQUESTS // 2))
    if count == 1 or span < MIN_WINDOW * count:
        return [(lo, hi, True)]

    step = span / count
    bounds = [lo + step * i for i in range(count)] + [hi]
    if any(bounds[i] >= bounds[i + 1] for i in range(count)):
        # Not enough resolution to slice cleanly; let the split loop handle it.
        return [(lo, hi, True)]
    return [(bounds[i], bounds[i + 1], i == count - 1) for i in range(count)]


def _verify(got: int, expected: int) -> None:
    """The check the whole strategy rests on: did every counted row arrive?

    `>=` rather than `==`: the count is taken before the range is read, so a
    project that is still ingesting can legitimately deliver a few rows that
    landed between the two and fall inside the window range. Extra rows are a
    fresher snapshot; missing rows are data loss, and only that is refused.
    """
    if got >= expected:
        return
    raise RuntimeError(
        f"Extraction returned {got:,} of {expected:,} rows. PostHog truncates a "
        f"response without saying so, so an incomplete result is refused rather "
        f"than stored; the previously cached data is still being served. Add an "
        f"ORDER BY if the query does not have one, or narrow it."
    )


def _identifier(name: str) -> str:
    """A column reference HogQL will accept — PostHog columns include `$session_id`."""
    return "`" + str(name).replace("`", "``") + "`"


def _parse_dt(value) -> Optional[datetime]:
    """A datetime from what the API sends back ('2026-08-02T05:27:21.077000Z')."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_dt(dt: datetime) -> str:
    """The literal form the API compares correctly against a DateTime column."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _midpoint(lo: datetime, hi: datetime) -> Optional[datetime]:
    """The split point of a window, or None when it cannot be split further."""
    if hi - lo < MIN_WINDOW * 2:
        return None
    mid = lo + (hi - lo) / 2
    if mid <= lo or mid >= hi:
        return None
    return mid


def _columns(names, types) -> List[dict]:
    """(name, dtype) pairs from the API's `columns` / `types`.

    PostHog sends `types` as (name, type) pairs; a flat list of type names is
    also accepted, since the column names are the contract and the types are
    read for the preview and for choosing a cursor.
    """
    out = []
    for i, name in enumerate(names or []):
        t = types[i] if types and i < len(types) else None
        if isinstance(t, (list, tuple)):
            t = t[1] if len(t) > 1 else (t[0] if t else None)
        out.append({"name": str(name), "dtype": str(t) if t else None})
    return out


def _scalar(v):
    """A value DuckDB can store, from a value JSON can carry.

    PostHog sends `properties` as JSON *text* already, but ClickHouse arrays and
    tuples arrive as nested structures. Arrow would infer a type for those from
    whatever one batch happened to contain, so two batches of the same column
    could disagree. They are stored as JSON text, which is stable across batches
    and which DuckDB can read back with its own JSON functions.
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    return str(v)


def _datetime_columns(names, types) -> set:
    """Which columns are dates, according to the types PostHog reports.

    JSON has no date type, so a `DateTime64` arrives as a string like
    "2026-08-02T05:27:21.077000Z" and Arrow infers it as text. Stored that way,
    every agent query against the relation has to `CAST(timestamp AS TIMESTAMP)`
    before it can use `date_trunc`, an interval, or a range comparison — and the
    model has to know to do it. The API already says which columns these are,
    so they are converted rather than left for the agent to deal with.
    """
    return {
        c["name"] for c in _columns(names, types)
        if c["dtype"] and "date" in c["dtype"].lower()
    }


def _typed_column(values, is_datetime: bool):
    """One column's values, as dates when the source says they are dates.

    Falls back to the raw strings if any value does not parse. A column that is
    *mostly* dates is not worth silently nulling the rest of — leaving it as
    text keeps the data, which the agent can still cast.
    """
    if not is_datetime:
        return values
    out = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        parsed = _parse_dt(v)
        if parsed is None:
            return values
        out.append(parsed)
    return out


def _tables(names, types, rows, batch_rows, schema, allow_empty: bool):
    """Rows as pyarrow.Tables of at most `batch_rows`, pinned to one schema.

    `schema` is a single-element list holding the first batch's schema, which
    every later batch is cast to. Without that, a column that happened to be
    all-NULL in one window would be typed differently in the next and the
    append would fail partway through a refresh — the failure mode a windowed
    source has and a cursor does not.
    """
    if not rows:
        if allow_empty:
            # Zero rows: still emit the shape so the relation exists.
            yield _empty_table(names)
        return

    size = max(1, int(batch_rows or SINGLE_RESPONSE_ROWS))
    dates = _datetime_columns(names, types)
    for start in range(0, len(rows), size):
        table = _arrow(names, rows[start:start + size], schema[0], dates)
        schema[0] = table.schema
        yield table


def _arrow(names, rows, schema, dates=frozenset()):
    from app.data_sources.fast.sources import arrow_table

    data = {}
    for i, name in enumerate(names):
        values = [_scalar(r[i]) if i < len(r) else None for r in rows]
        data[str(name)] = _typed_column(values, str(name) in dates)
    try:
        table = arrow_table(data)
    except Exception as e:
        # Arrow's own message names the value and the type it tried; on its own
        # it reaches the admin as an internal error from a failed refresh.
        raise RuntimeError(
            f"PostHog returned a column whose values do not share one type and "
            f"could not be stored: {e}"
        )
    if schema is None:
        return _without_null_columns(table)
    if table.schema.equals(schema):
        return table
    try:
        return table.cast(schema)
    except Exception as e:
        raise RuntimeError(
            f"PostHog returned a different column type partway through the "
            f"extraction and the batch could not be reconciled: {e}"
        )


def _without_null_columns(table):
    """Retype all-NULL columns as strings before they become the pinned schema.

    An all-NULL first batch infers Arrow's `null` type, which DuckDB stores as a
    column that can hold nothing else. Every later batch would then fail to
    cast. String is the honest fallback: it can hold whatever arrives next.
    """
    import pyarrow as pa

    nulls = [f.name for f in table.schema if pa.types.is_null(f.type)]
    if not nulls:
        return table
    logger.info("posthog.extraction.null_column_typed_as_string",
                extra={"columns": nulls})
    for name in nulls:
        i = table.schema.get_field_index(name)
        table = table.set_column(
            i, pa.field(name, pa.string()), table.column(i).cast(pa.string())
        )
    return table


def _empty_table(names: Optional[List[str]]):
    """The shape of a result with no rows, so the relation still exists."""
    import pyarrow as pa

    cols = [str(n) for n in (names or [])]
    schema = pa.schema([(c, pa.string()) for c in cols])
    return pa.Table.from_pydict({c: [] for c in cols}, schema=schema)
