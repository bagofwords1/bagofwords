"""Materializing from SAP BusinessObjects through the Semantic Layer REST API.

A BusinessObjects query is not text: it is a universe and the result objects
to return from it. A custom table here is therefore a small JSON document,

    {"universe": "Sales", "objects": ["Country", "Year", "Sales revenue"]}

naming the universe as the catalog shows it (or by id) and its objects by
name. Before this source a custom table on BusinessObjects could not be
defined at all: the generic path hands the connector one string, and the
connector needs two.

Everything below follows from the connector's query lifecycle
(clients/businessobjects_client.py): create a query, run it, read
`$metadata`, read `$count`, page the rows with `$skip` / `$top`, delete the
query. **None of it has been run against a live server**; what is assumed
rather than read off the client's code says so.

**Estimating: an exact count, at the cost of running the query.** `$count` is
the result's true row count, and it is what the budget check sees. There is no
count without a result, so an estimate creates and runs the query at the
source, then deletes it.

**Extracting: pages of the one result.** Rows are read `PAGE_ROWS` at a time
from a single created query, so every page comes from the same result rather
than from a re-run. (Assumed: that the server holds one result per query id
across pages, which is what the lifecycle implies.) The connector's own page
size is 50, sized for an agent's small reads; at that size a million rows is
twenty thousand requests, so this source asks for 10,000 a page. (Assumed:
that the server accepts `$top=10000`. If it returns fewer than asked, the next
page starts from however many arrived, so a server-side page cap costs
requests, not rows.)

**Truncation.** The count is taken first. Paging stops only when that many
rows have arrived; a page that comes back empty before then fails the refresh
("ended the result before its reported row count"), and the previous artifact
keeps serving. Nothing short of the count is ever handed over as complete.

**Requests.** One extraction makes at most the organization's request limit
(fast/limits.py). A result that cannot fit in that many pages is refused when
the table is saved, from the count, before any page is read.

**Cancelling.** Stopping means not asking for the next page; the temporary
query is deleted at the source when the read ends, however it ends.

**Refresh windows.** The connector's query carries result objects only, no
filter, so a window cannot be written into it: an incremental table runs its
query whole and the window is taken from the result (see
`incremental.filters_at_source`).

**Security.** The query runs as the connection's own BusinessObjects user, so
that user's universe security profiles decide what is copied. Custom tables
are only offered on connections with shared credentials.
"""

import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from app.data_sources.fast import limits as fast_limits

# Rows asked for per request. Ours; the connector's default (50) is sized for
# an agent's reads, not for copying a result.
PAGE_ROWS = 10_000

_EXAMPLE = '{"universe": "Sales", "objects": ["Country", "Sales revenue"]}'
_ODATA_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")
_EPOCH = datetime(1970, 1, 1)


class BusinessObjectsSource:
    """estimate / preview / stream_batches over a universe query."""

    def __init__(self, client):
        self.client = client

    # -- the protocol -----------------------------------------------------

    def estimate(self, sql: str):
        """The exact row count. Never raises: a failure is `supported=False`."""
        from app.data_sources.fast.extractor import Estimate
        from app.data_sources.fast.sql_dialect import ASSUMED_ROW_WIDTH_BYTES

        try:
            universe_id, objects = self._resolve(sql)
            with self.client.open_universe_query(universe_id, objects) as result:
                rows = int(result["total"])
        except Exception as e:
            return Estimate(supported=False, note=f"BusinessObjects row count failed: {e}")
        est = Estimate(
            rows=rows,
            width_bytes=ASSUMED_ROW_WIDTH_BYTES,
            total_bytes=rows * ASSUMED_ROW_WIDTH_BYTES,
            note="exact row count from the query's $count; row width estimated",
        )
        max_requests = fast_limits.of(self.client).max_requests
        if rows > PAGE_ROWS * max_requests:
            est.source_max_rows = PAGE_ROWS * max_requests
            est.source_max_rows_note = (
                f"This query returns {rows:,} rows. BusinessObjects is read "
                f"{PAGE_ROWS:,} rows per request and one refresh may make "
                f"{max_requests:,} requests, so at most {PAGE_ROWS * max_requests:,} "
                f"rows can be extracted. Select fewer or coarser objects, or have "
                f"an admin raise the request limit in the organization's settings."
            )
        return est

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        """The first `limit` rows: one bounded page."""
        universe_id, objects = self._resolve(sql)
        with self.client.open_universe_query(universe_id, objects) as result:
            want = min(int(limit), int(result["total"]))
            rows = self.client.read_universe_page(result, 0, want) if want else []
            types = result["types"]
        return ([{"name": name, "dtype": None} for name in objects],
                [[_typed(v, t) for v, t in zip(row, types)] for row in rows[:limit]])

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Yield pyarrow.Table batches covering the whole result, or fail."""
        from app.data_sources.fast.normalize import rows_to_arrow

        universe_id, objects = self._resolve(sql)
        max_requests = fast_limits.of(self.client).max_requests
        size = max(1, int(batch_rows or PAGE_ROWS))
        with self.client.open_universe_query(universe_id, objects) as result:
            total, types = int(result["total"]), result["types"]
            if total == 0:
                # The objects are the columns, so an empty result still has a shape.
                yield rows_to_arrow(objects, [])
                return
            read = requests_made = 0
            while read < total:
                requests_made += 1
                if requests_made > max_requests:
                    raise RuntimeError(
                        f"Extraction stopped after {max_requests:,} requests, the limit "
                        f"for one refresh, with {read:,} of {total:,} rows read. Select "
                        f"fewer or coarser objects, or have an admin raise the request "
                        f"limit in the organization's settings."
                    )
                # Never past the count; an empty page before it is raised by
                # the client as a result that ended early.
                page = self.client.read_universe_page(result, read, min(PAGE_ROWS, total - read))
                read += len(page)
                rows = [[_typed(v, t) for v, t in zip(row, types)] for row in page]
                for start in range(0, len(rows), size):
                    yield rows_to_arrow(objects, rows[start:start + size])

    # -- the definition ---------------------------------------------------

    def _resolve(self, spec: str) -> Tuple[str, List[str]]:
        universe, objects = parse_query(spec)
        meta = self.client._resolve_universe(universe)
        if not meta or not meta.get("universe_id"):
            raise ValueError(
                f"Universe '{universe}' was not found. Use its name exactly as the "
                f"connection's catalog shows it."
            )
        return str(meta["universe_id"]), objects


def parse_query(spec) -> Tuple[str, List[str]]:
    """(universe, result objects) from a custom table's definition."""
    try:
        body = json.loads(spec) if isinstance(spec, str) else dict(spec)
    except (TypeError, ValueError):
        body = None
    if not isinstance(body, dict):
        raise ValueError(
            "A BusinessObjects custom table is a JSON object naming a universe and "
            f"the objects to return, for example: {_EXAMPLE}"
        )
    universe = str(body.get("universe") or "").strip()
    objects = body.get("objects")
    if isinstance(objects, str):
        objects = [o.strip() for o in objects.split(",")]
    if not isinstance(objects, list):
        objects = []
    objects = [str(o).strip() for o in objects if str(o).strip()]
    if not universe or not objects:
        raise ValueError(
            f'The query needs a "universe" and at least one name in "objects", '
            f"for example: {_EXAMPLE}"
        )
    if len(set(objects)) != len(objects):
        raise ValueError("Select each result object only once.")
    return universe, objects


def _typed(value, edm):
    """An OData JSON value in the type its column declares.

    OData JSON carries 64-bit integers and decimals as strings and dates as
    `/Date(ms)/` (v2) or ISO text; left alone they would all be stored as
    text. Assumed from the OData formats, not measured against a server. A
    value that does not parse as declared is kept as it arrived.
    """
    if value is None or not isinstance(value, str) or not edm:
        return value
    kind = str(edm).split(".")[-1]
    try:
        if kind in ("Int64", "Int32", "Int16", "Byte", "SByte"):
            return int(value)
        if kind in ("Decimal", "Double", "Single"):
            return float(value)
        if kind in ("DateTime", "DateTimeOffset"):
            m = _ODATA_DATE.match(value)
            if m:
                return _EPOCH + timedelta(milliseconds=int(m.group(1)))
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
    except (TypeError, ValueError, OverflowError):
        return value
    return value
