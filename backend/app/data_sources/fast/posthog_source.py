"""Materializing from PostHog through its HogQL query API.

PostHog is the first accelerated source that is not a database connection at
all. There is no cursor and no session: every read is one HTTP POST to
`/api/projects/{id}/query/` that compiles a HogQL statement, runs it on
ClickHouse, and returns the whole result as JSON. That single fact shapes all
three answers this protocol asks for, and each of them differs from every
source before it.

**Estimating — there is nothing to ask.** The query API exposes no planner:
it compiles and executes in one call, and its `explain` option returns a plan
description rather than a cardinality. So `estimate()` reports itself
unsupported, the way SQLite does, and the pre-flight refusal is replaced by
the extractor's mid-flight caps. That is materially safer here than it sounds:
each page is a *completed* HTTP request that PostHog itself bounds, so an
over-large query costs a series of bounded requests that the caps stop, not
one runaway scan holding a connection open on a shared server.

**Paging — the one source where the SQL must be rewritten.** Everywhere else
the extractor bounds the *fetch* and sends the admin's statement untouched,
because wrapping it can change its meaning. PostHog leaves no such choice:
a HogQL query with no `LIMIT` is silently truncated by the server (100 rows by
default), and there is no cursor to keep pulling from. An unwrapped statement
would therefore materialize a *quietly incomplete* relation — the worst
possible outcome, since nothing would look wrong. So the admin's SQL goes
inside a derived table and the bound is expressed as `LIMIT … OFFSET …`,
paging until a page comes back empty.

  Consequence worth stating plainly: `OFFSET` is only meaningful over a defined
  order. A custom query with no `ORDER BY` may repeat or skip rows across page
  boundaries, because ClickHouse is free to return the pages in different
  orders. An accelerated PostHog query should carry an `ORDER BY` over
  something stable — `timestamp`, `uuid` — and the authoring preview is a
  single page, so it never exposes the problem on its own.

  Pages advance by the number of rows actually returned, never by the number
  requested. PostHog clamps an over-large `LIMIT` to its own ceiling, and that
  ceiling has changed between versions; advancing by the request would skip
  every row the server declined to send.

**Cancelling — nothing to cancel.** By the time the extractor decides to stop,
the page it was reading has already been served and paid for. Aborting means
not asking for the next one.

PostHog sits in UNVERIFIED_TYPES until this file has been run against a live
project: response shapes, the server's row ceiling, and its rate-limiting
behavior under a long extraction are exactly the things a fake cannot prove.
"""

import json
import logging
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Rows requested per HTTP page. Deliberately below PostHog's own per-response
# ceiling (50,000 on current versions, 10,000 on older ones) so the request is
# never clamped in a way that would make a short page ambiguous, and small
# enough that one page stays inside the API's request timeout on a heavy query.
PAGE_ROWS = 10_000


class PostHogSource:
    """Extraction over PostHog's HogQL query endpoint."""

    def __init__(self, client):
        self.client = client

    # -- the protocol -----------------------------------------------------

    def estimate(self, sql: str):
        """Unsupported, and says so — PostHog has no pre-flight estimate.

        Reported rather than omitted so the reason reaches the authoring modal
        and the "every accelerable type answers estimate()" contract keeps its
        meaning. The extractor falls back to its row, byte and wall-clock caps.
        """
        from app.data_sources.fast.extractor import Estimate

        return Estimate(
            supported=False,
            note=(
                "PostHog's query API compiles and runs in one call and exposes "
                "no row estimate; the hard caps apply instead"
            ),
        )

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        """First `limit` rows — a single bounded request, never the full result."""
        names, types, rows = self._page(sql, limit, 0)
        return _columns(names, types), [list(r) for r in rows]

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Yield pyarrow.Table batches, one per HTTP page, until the result ends.

        The loop stops on an empty page rather than on a short one: a short page
        means the server clamped the limit, which is normal, while an empty page
        is the only signal that the offset has passed the end of the result.
        """
        page_rows = max(1, min(int(batch_rows or PAGE_ROWS), PAGE_ROWS))
        offset = 0
        schema = None
        emitted = False

        while True:
            names, _types, rows = self._page(sql, page_rows, offset)
            if not rows:
                if not emitted:
                    # Zero rows: still emit the shape so the relation exists.
                    yield _empty_table(names)
                return
            table = _arrow(names, rows, schema)
            schema = table.schema
            emitted = True
            yield table
            offset += len(rows)

    # -- helpers ----------------------------------------------------------

    def _page(self, sql: str, limit: int, offset: int):
        return self.client.run_hogql(paged_hogql(sql, limit, offset))


def paged_hogql(sql: str, limit: int, offset: int) -> str:
    """The admin's SQL as one bounded page.

    The statement goes inside a derived table untouched — appending `LIMIT` to
    it would change the meaning of a query that already has one, and here that
    matters more than usual, since a `LIMIT`-carrying query must still page to
    a natural end.
    """
    from app.data_sources.fast.sql_dialect import strip_trailing_semicolon

    inner = strip_trailing_semicolon(sql)
    return (
        f"SELECT * FROM ({inner}) AS bow_sub "
        f"LIMIT {int(limit)} OFFSET {int(offset)}"
    )


def _columns(names, types) -> List[dict]:
    """(name, dtype) pairs from the API's `columns` / `types`.

    `types` is optional and has been spelled both as a flat list of ClickHouse
    type names and as (name, type) pairs, so it is read defensively: the column
    names are the contract, the types are a nicety for the preview.
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

    PostHog returns JSON columns (`properties`, `person.properties`) and
    ClickHouse arrays as nested structures. Arrow would infer a struct type per
    batch from whatever keys that batch happened to contain, so two pages of the
    same column could disagree about its type. They are stored as JSON text
    instead, which is stable across pages and which DuckDB can read back with
    its own JSON functions.
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    return str(v)


def _arrow(names, rows, schema):
    """A batch as a pyarrow.Table, pinned to the first page's schema.

    Every page after the first is cast to the schema DuckDB's table was created
    with. Without that, a column that happened to be all-NULL on one page would
    be typed differently on the next and the append would fail partway through a
    refresh — the failure mode a paged source has and a cursor does not.
    """
    from app.data_sources.fast.sources import arrow_table

    data = {
        str(name): [_scalar(r[i]) if i < len(r) else None for r in rows]
        for i, name in enumerate(names)
    }
    table = arrow_table(data)
    if schema is None:
        return _without_null_columns(table)
    if table.schema.equals(schema):
        return table
    try:
        return table.cast(schema)
    except Exception as e:
        raise RuntimeError(
            f"PostHog returned a different column type partway through the "
            f"extraction and the page could not be reconciled: {e}"
        )


def _without_null_columns(table):
    """Retype all-NULL columns as strings before they become the pinned schema.

    An all-NULL first page infers Arrow's `null` type, which DuckDB stores as a
    column that can hold nothing else. Every later page would then fail to cast.
    String is the honest fallback: it can hold whatever arrives next.
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
