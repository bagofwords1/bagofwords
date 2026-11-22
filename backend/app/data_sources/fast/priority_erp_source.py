"""Materializing from Priority ERP: one form, read to the end in pages.

A custom table on a Priority connection is defined the way the connector is
queried: a form name with OData query options,

    ORDERS?$filter=CUSTNAME eq '1011'&$select=ORDNAME,CUSTNAME,CURDATE

Allowed options are `$filter`, `$select`, `$orderby`, `$since`, `$top` and
`$skip` (the last two bound the table as a whole: "the newest 1,000 orders").
Before this source such a table went through the connector's `execute_query`,
which is one request: Priority silently caps one response at its
`MAXAPILINES` constant (2,000 rows by default), so the stored table was
whatever fitted in it.

**Nothing here has been run against a live Priority server.** Each paragraph
says what is read off Priority's public REST documentation
(prioritysoftware.github.io/restapi), what is read off the connector
(clients/priority_erp_client.py), and what is assumed.

**Paging.** Rows are read with `$top` / `$skip`, `PAGE_ROWS` a request.
Documented: `$skip` combined with `$top` is how a collection is paged; a
response holds at most `MAXAPILINES` rows (25.1+, default 2,000;
`MAXFORMLINES` before that) whatever `$top` says, and nothing in the response
marks the cut. So `PAGE_ROWS` is that default, a short page is never taken as
the end, and reading stops only at an empty page (or at the definition's own
`$top`). A server with a lower constant costs requests, not rows: the next
page starts from however many arrived. Assumed: that `$skip` reaches past
`MAXAPILINES`, which is what "implement pagination" in the documentation
implies. If it does not, a counted result fails (see Truncation) and an
uncounted one would stop early; that case cannot be told from here.

**Order.** Offset paging needs the same order on every request. A
definition's `$orderby` is kept. Where the indexed catalog knows the form's
key (it does for a form with indexed sub-forms) and the definition selects
it, the key is added as the order, or as the tie-breaker after the admin's.
Otherwise the form's default order is used. Assumed: that the default order
is stable between requests. Rows added or deleted in the form *while it is
being read* can shift a later page under any offset paging; a refresh in
merge mode collapses a repeated row, and a row lost that way is caught by the
count below only when the result is counted.

**Estimating: `$count`, when the server answers it.** `FORM/$count` with the
definition's `$filter` / `$since` is standard OData v4 and is one request
that builds no rows. Priority's documentation does not mention `$count`, so
it is tried, never relied on: an integer answer is the exact row count the
budget check and the progress bar see; anything else is "no count", said as
such. A count is never guessed.

**Truncation.** When there is a count, fewer rows than it fail the refresh
("ended before its row count") and the previous artifact keeps serving. With
no count, the empty page is the only end there is. A response longer than
asked for (a server ignoring `$top`) also fails, since offsets would then
repeat rows.

**Rate limit and the request ceiling.** Documented for Priority Cloud: 100
calls a minute per tenant, 10 running plus 5 queued, a request dropped after
3 minutes, HTTP 429 when throttled. The connector's limiter paces every
request to the connection's calls-per-minute setting. A 429 is waited out
(`Retry-After` if sent, else `RATE_LIMIT_WAIT_SECONDS`) and the page retried,
`RATE_LIMIT_RETRIES` times, then the refresh fails. Each request has the
connector's 120-second timeout, under the 3-minute ceiling; a page that times
out is retried at half the size, down to `MIN_PAGE_ROWS`. Every request,
retries included, counts against the organization's request limit
(fast/limits.py); a counted result too large for it is refused when saved.

**Cancelling.** One batch is yielded per page, so the loader's cancellation
and time checks, and the first-load progress, run between pages. A wait for
the rate limit is not interruptible; it is at most a minute.

**Refresh windows.** Written into the definition's `$filter` on the cursor
column (`UDATE ge 2026-01-01T00:00:00Z`), or placed by the admin with
`{{since}}` / `{{until}}`, which is how Priority's own `$since` is used:
`ORDERS?$since={{since}}`. A bound is written exactly, fractional seconds
included, because the loader replaces stored rows from that same bound.
A definition with its own `$top` / `$skip` is not filtered at the source:
those apply after `$filter`, so they would bound each window instead of the
table. It is read whole and the window taken from the result
(`filters_query_at_source`), and placeholders are refused in it. Documented:
`$since` works only on forms with business process management, and UTC with
a `Z` suffix is the recommended form; calculated columns cannot be filtered
on. Assumed: 1988-01-01 as the "everything" lower bound (Priority's date
epoch), and that a literal with fractional seconds is accepted (Priority's
own values are whole minutes, so one arises only from an admin-typed start).

**Sub-forms are not supported in this version.** `$expand`, a sub-form table
named directly (the connector answers it through its parent's `$expand`) and
a navigation path are refused when the table is saved. Priority serves lines
only nested in their parent, with no count of lines and no page inside a
parent; an unbounded `$expand` is one response of every parent with every
line, which is the request the connector itself refuses past 500 parents.

**Types.** Priority's JSON carries numbers as numbers and dates as ISO text
with an offset. A column whose every value in a page is such a date is stored
as a UTC timestamp, so it sorts, compares and can be a cursor. Assumed from
the documented response format.

**Security.** Rows are read as the connection's API user. Priority documents
that REST requests are not subject to the data privileges of its interface,
so that user sees whole forms. Custom tables are only offered on connections
with shared credentials.
"""

import math
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import quote

from app.data_sources.fast import limits as fast_limits

# Rows asked for per request: Priority's default MAXAPILINES. Asking for more
# returns this many on a default server anyway.
PAGE_ROWS = 2_000
# A page that times out is retried at half the size, no smaller than this.
MIN_PAGE_ROWS = 100
# HTTP 429: how often one request is retried, and how long to wait when the
# server does not say (`Retry-After`). Never longer than the cap.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_WAIT_SECONDS = 20
RATE_LIMIT_MAX_WAIT_SECONDS = 60

ALLOWED_OPTIONS = ("$filter", "$select", "$orderby", "$since", "$top", "$skip")
_EXAMPLE = "ORDERS?$filter=CUSTNAME eq '1011'&$select=ORDNAME,CUSTNAME,CURDATE"

_SAFE = "()$,;=:'/@!*-._~"          # as the connector encodes its own requests
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_SINCE = re.compile(r"\{\{\s*since\s*\}\}", re.IGNORECASE)
_UNTIL = re.compile(r"\{\{\s*until\s*\}\}", re.IGNORECASE)
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$")
_SAFE_TEXT = re.compile(r"^[0-9A-Za-z:\-+. _/]*$")

# The bounds a placeholder gets when a window is open on that side.
_FLOOR = datetime(1988, 1, 1, tzinfo=UTC)
_CEILING = datetime(2999, 12, 31, tzinfo=UTC)

# Replaced in tests; a wait for the rate limit must not slow the suite.
_sleep = time.sleep


@dataclass(frozen=True)
class Definition:
    """A custom table's definition: one form and its query options."""

    form: str
    filter: str | None = None
    select: tuple[str, ...] | None = None
    orderby: str | None = None
    since: str | None = None
    top: int | None = None
    skip: int = 0

    def text(self) -> str:
        """The definition as an admin would write it (not URL-encoded)."""
        return self._render(self.top, self.skip, encode=False)

    def page_path(self, top: int, skip: int, orderby: str | None = None) -> str:
        return replace(self, orderby=orderby or self.orderby)._render(top, skip, encode=True)

    def count_path(self) -> str:
        parts = self._conditions()
        return f"{self.form}/$count" + ("?" + "&".join(quote(p, safe=_SAFE) for p in parts) if parts else "")

    def bounded(self, matched: int) -> int:
        """Rows this definition returns when `matched` rows meet its filter."""
        rows = max(0, matched - self.skip)
        return rows if self.top is None else min(rows, self.top)

    def _conditions(self) -> list[str]:
        parts = []
        if self.filter:
            parts.append("$filter=" + self.filter)
        if self.since:
            parts.append("$since=" + self.since)
        return parts

    def _render(self, top, skip, encode: bool) -> str:
        parts = self._conditions()
        if self.select:
            parts.append("$select=" + ",".join(self.select))
        if self.orderby:
            parts.append("$orderby=" + self.orderby)
        if top is not None:
            parts.append(f"$top={int(top)}")
        if skip:
            parts.append(f"$skip={int(skip)}")
        if not parts:
            return self.form
        return f"{self.form}?" + "&".join(quote(p, safe=_SAFE) if encode else p for p in parts)


def parse_definition(text) -> Definition:
    """A custom table's definition, or ValueError saying what to write instead."""
    from app.data_sources.clients.priority_odata_query import parse_query_string

    target = str(text or "").strip().lstrip("/")
    if not target:
        raise ValueError(f"A Priority custom table is a form name with OData options, for example: {_EXAMPLE}")
    if _SQL.match(target):
        raise ValueError(
            "Priority is queried with OData, not SQL. Define the table as a form name "
            f"with OData options, for example: {_EXAMPLE}"
        )
    path, _, query_string = target.partition("?")
    path = path.strip()
    if not _NAME.match(path):
        raise ValueError(
            f"'{path}' is not a form name. A Priority custom table reads one form from the "
            f"service root, so a single record (FORM('key')) or a path into a sub-form "
            f"cannot be cached. For example: {_EXAMPLE}"
        )
    opts = parse_query_string(query_string)
    if "$expand" in opts:
        raise ValueError(
            "$expand (sub-forms) is not supported in a Priority custom table in this "
            "version: Priority returns lines only nested inside their parent, in one "
            "response that cannot be counted or read in pages. Cache the parent form "
            "without $expand."
        )
    unknown = [k for k in opts if k not in ALLOWED_OPTIONS]
    if unknown:
        raise ValueError(
            f"{', '.join(unknown)} cannot be used in a Priority custom table. "
            f"Supported options: {', '.join(ALLOWED_OPTIONS)}."
        )
    select = tuple(c.strip() for c in (opts.get("$select") or "").split(",") if c.strip())
    if len({c.lower() for c in select}) != len(select):
        raise ValueError("$select names a column more than once.")
    return Definition(
        form=path,
        filter=opts.get("$filter") or None,
        select=select or None,
        orderby=opts.get("$orderby") or None,
        since=opts.get("$since") or None,
        top=_whole(opts, "$top"),
        skip=_whole(opts, "$skip") or 0,
    )


def _whole(opts: dict, key: str) -> int | None:
    raw = opts.get(key)
    if raw in (None, ""):
        return None
    if not str(raw).isdigit():
        raise ValueError(f"{key} must be a whole number, not '{raw}'.")
    return int(raw)


class _Stopped(RuntimeError):
    """The extraction may make no further request (request limit, or the rate
    limit never cleared). Not a reason to carry on without a count."""


class _Requests:
    """The requests one estimate, preview or extraction makes: counted against
    the organization's limit, and a 429 waited out and retried."""

    def __init__(self, client, session, max_requests: int):
        self.client, self.session, self.max_requests = client, session, max_requests
        self.made = 0

    def get(self, path: str, accept: str = "application/json", progress: str = ""):
        from app.data_sources.clients.priority_erp_client import PriorityRateLimited

        throttled = 0
        while True:
            self.made += 1
            if self.made > self.max_requests:
                raise _Stopped(
                    f"Extraction stopped after {self.max_requests:,} requests, the limit for "
                    f"one refresh{progress}. Narrow the $filter or $select fewer columns, or "
                    f"have an admin raise the request limit in the organization's settings."
                )
            try:
                return self.client._get(self.session, path, accept=accept)
            except PriorityRateLimited as e:
                throttled += 1
                if throttled > RATE_LIMIT_RETRIES:
                    raise _Stopped(
                        f"Priority kept answering HTTP 429 (rate limit) after {RATE_LIMIT_RETRIES} "
                        f"retries{progress}. Other work is using this tenant's call budget; "
                        f"refresh at a quieter time or lower the connection's Max Calls Per Minute."
                    ) from e
                wait = e.retry_after if e.retry_after else RATE_LIMIT_WAIT_SECONDS
                _sleep(max(1.0, min(float(wait), RATE_LIMIT_MAX_WAIT_SECONDS)))


class PriorityErpSource:
    """estimate / preview / stream_batches over one Priority form."""

    def __init__(self, client):
        self.client = client

    # -- the protocol -----------------------------------------------------

    def estimate(self, sql: str):
        """The exact row count where Priority gives one. Never raises."""
        from app.data_sources.fast.extractor import Estimate
        from app.data_sources.fast.sql_dialect import ASSUMED_ROW_WIDTH_BYTES

        max_requests = fast_limits.of(self.client).max_requests
        try:
            definition = self._definition(sql)
            with self.client.connect() as session:
                matched = self._count(_Requests(self.client, session, max_requests), definition)
        except Exception as e:
            return Estimate(supported=False, note=f"Priority row count failed: {e}")
        if matched is None:
            return Estimate(
                supported=False,
                note=f"This Priority server did not answer $count for {definition.form}, "
                     f"so the result's size is known only once it has been read.",
            )
        rows = definition.bounded(matched)
        est = Estimate(
            rows=rows,
            width_bytes=ASSUMED_ROW_WIDTH_BYTES,
            total_bytes=rows * ASSUMED_ROW_WIDTH_BYTES,
            note="exact row count from the form's $count; row width estimated",
        )
        # One request for the count, one per page, and one for the empty page
        # that ends the read, unless the definition's own $top ends it first.
        closing = 0 if definition.top is not None and rows >= definition.top else 1
        if 1 + math.ceil(rows / PAGE_ROWS) + closing > max_requests:
            capacity = PAGE_ROWS * max(0, max_requests - 1 - closing)
            est.source_max_rows = capacity
            est.source_max_rows_note = (
                f"This query returns {rows:,} rows. Priority is read {PAGE_ROWS:,} rows per "
                f"request and one refresh may make {max_requests:,} requests, so at most "
                f"{capacity:,} rows can be extracted. Narrow the $filter, or have an admin "
                f"raise the request limit in the organization's settings."
            )
        return est

    def preview(self, sql: str, limit: int) -> tuple[list[dict], list[list]]:
        """The first `limit` rows: one bounded request."""
        definition = self._definition(sql)
        want = int(limit) if definition.top is None else min(int(limit), definition.top)
        with self.client.connect() as session:
            requests = _Requests(self.client, session, fast_limits.of(self.client).max_requests)
            records = self._records(requests, definition.page_path(want, definition.skip, self._stable_order(definition)))[:want] if want else []
            columns = _columns_of(records) or self._shape(requests, definition)
        rows = _typed(columns, records)
        return [{"name": name, "dtype": None} for name in columns], rows

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Yield one pyarrow.Table per page until the form is read, or fail."""
        import requests as http

        from app.data_sources.fast.normalize import rows_to_arrow

        definition = self._definition(sql)
        size = max(1, int(batch_rows or PAGE_ROWS))
        orderby = self._stable_order(definition)
        with self.client.connect() as session:
            requests = _Requests(self.client, session, fast_limits.of(self.client).max_requests)
            matched = self._count(requests, definition)
            expected = None if matched is None else definition.bounded(matched)

            columns: list[str] = []
            read, page = 0, PAGE_ROWS
            remaining = definition.top
            while remaining is None or remaining > 0:
                want = page if remaining is None else min(page, remaining)
                progress = f", with {read:,}" + (f" of {expected:,}" if expected is not None else "") + " rows read"
                try:
                    records = self._records(
                        requests, definition.page_path(want, definition.skip + read, orderby), progress)
                except http.Timeout:
                    if want <= MIN_PAGE_ROWS:
                        raise RuntimeError(
                            f"Priority did not answer a page of {want:,} {definition.form} rows in "
                            f"time{progress}. $select fewer columns (calculated ones are the slow "
                            f"ones) or narrow the $filter."
                        ) from None
                    page = max(MIN_PAGE_ROWS, want // 2)
                    continue
                if not records:
                    break
                if len(records) > want:
                    raise RuntimeError(
                        f"Priority returned {len(records):,} rows to a request for {want:,}, so "
                        f"{definition.form} cannot be read in pages without repeating rows."
                    )
                read += len(records)
                if remaining is not None:
                    remaining -= len(records)
                for name in _columns_of(records):
                    if name not in columns:
                        columns.append(name)
                rows = _typed(columns, records)
                for start in range(0, len(rows), size):
                    yield rows_to_arrow(columns, rows[start:start + size])

            if expected is not None and read < expected:
                raise RuntimeError(
                    f"Priority ended the result before its row count: {read:,} of {expected:,} "
                    f"{definition.form} rows arrived. Nothing was stored; the previous data is "
                    f"still being served. If rows are deleted from this form while it is read, "
                    f"refresh at a quieter time."
                )
            if not read:
                # Zero rows: still emit the shape so the relation exists.
                yield rows_to_arrow(self._shape(requests, definition), [])

    # -- requests ---------------------------------------------------------

    def _definition(self, sql: str) -> Definition:
        definition = parse_definition(sql)
        refuse_subform(definition, getattr(self.client, "_subforms", None) or {})
        return definition

    def _records(self, requests: _Requests, path: str, progress: str = "") -> list[dict]:
        payload = requests.get(path, progress=progress).json()
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(value, list):
            raise RuntimeError("Priority answered without a list of rows.")
        return [r for r in value if isinstance(r, dict)]

    def _count(self, requests: _Requests, definition: Definition) -> int | None:
        """Rows matching the definition's filter, or None when the server does
        not answer `$count` with a number. The request limit still applies."""
        try:
            resp = requests.get(definition.count_path(), accept="text/plain, application/json;q=0.9")
        except _Stopped:
            raise
        except Exception:
            return None
        text = (getattr(resp, "text", "") or "").strip().lstrip("\ufeff")
        if text.isdigit():
            return int(text)
        try:
            payload = resp.json()
        except Exception:
            return None
        if isinstance(payload, dict):
            for key in ("@odata.count", "value"):
                found = payload.get(key)
                if isinstance(found, int) and not isinstance(found, bool) and found >= 0:
                    return found
        return None

    def _shape(self, requests: _Requests, definition: Definition) -> list[str]:
        """The columns of a result that has no rows to show them."""
        if definition.select:
            return list(definition.select)
        columns = _columns_of(self._records(requests, Definition(definition.form).page_path(1, 0)))
        if not columns:
            raise RuntimeError(
                f"{definition.form} returned no rows, so its columns are not known and the "
                f"table has no shape to be created with. Name the columns with $select."
            )
        return columns

    def _stable_order(self, definition: Definition) -> str | None:
        """The definition's order with the form's key as tie-breaker, where the
        catalog knows the key and the definition returns it."""
        keys = self._keys(definition.form)
        if not keys:
            return None
        if definition.select and not {k.lower() for k in keys} <= {c.lower() for c in definition.select}:
            return None
        ordered = {part.split()[0].lower() for part in (definition.orderby or "").split(",") if part.strip()}
        extra = [k for k in keys if k.lower() not in ordered]
        return ",".join(([definition.orderby] if definition.orderby else []) + extra) or None

    def _keys(self, form: str) -> list[str]:
        for meta in (getattr(self.client, "_subforms", None) or {}).values():
            path = meta.get("path") if isinstance(meta, dict) else None
            root = path[0] if isinstance(path, list) and path and isinstance(path[0], dict) else None
            if root and str(root.get("form", "")).upper() == form.upper():
                return [str(k) for k in root.get("keys") or [] if _NAME.match(str(k))]
        return []


# -- rows -------------------------------------------------------------------

def _columns_of(records: list[dict]) -> list[str]:
    """Column names in the order they first appear, OData annotations aside."""
    names: list[str] = []
    for record in records:
        for key in record:
            if "@odata" not in key and key not in names:
                names.append(key)
    return names


def _typed(columns: list[str], records: list[dict]) -> list[list]:
    """Rows in column order, with date text turned into UTC timestamps for a
    column where every value in the page is one."""
    rows = [[record.get(name) for name in columns] for record in records]
    for i in range(len(columns)):
        values = [row[i] for row in rows if row[i] is not None]
        if values and all(isinstance(v, str) and _DATETIME.match(v) for v in values):
            parsed = [_parse_datetime(row[i]) for row in rows]
            if all(p is not None or row[i] is None for p, row in zip(parsed, rows, strict=True)):
                for row, value in zip(rows, parsed, strict=True):
                    row[i] = value
    return rows


def _parse_datetime(text):
    if not isinstance(text, str):
        return None
    value = text[:-1] + "+00:00" if text.endswith("Z") else text
    if re.search(r"[+-]\d{4}$", value):
        value = value[:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


# -- refresh windows --------------------------------------------------------

def odata_literal(value) -> str:
    """A cursor value as an OData literal, exactly: date-times in UTC with a
    `Z` suffix, with their fractional seconds when they have any. The bound
    sent to Priority must be the bound the loader replaces from, or rows
    between the two are read again and stored twice."""
    if isinstance(value, bool) or value is None:
        raise ValueError("unsupported cursor value")
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        aware = aware.astimezone(UTC)
        return aware.strftime("%Y-%m-%dT%H:%M:%S.%fZ" if aware.microsecond else "%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return f"{value.isoformat()}T00:00:00Z"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ValueError("unsupported cursor value")
        return repr(value)
    text = str(value)
    if not _SAFE_TEXT.match(text):
        raise ValueError("The cursor column holds text that cannot be used as a checkpoint safely.")
    return f"'{text}'"


def is_bounded(query) -> bool:
    """Whether a definition carries its own `$top` / `$skip`.

    Those apply after `$filter`, so a refresh window written into the filter
    would change which rows the bounds keep ("skip the first ten" of the
    window instead of the form). Such a table is refreshed by reading its
    definition whole and taking the window from the result.
    """
    try:
        definition = parse_definition(query)
    except ValueError:
        return False
    return definition.top is not None or bool(definition.skip)


def check_definition(text, tables=None) -> None:
    """Raise ValueError for a definition a custom table cannot be built from.

    `tables` is the connection's indexed catalog (`{"name", "metadata_json"}`
    rows) when the caller has it: a sub-form named as a table is only
    recognisable from there.
    """
    definition = parse_definition(text)
    if (_SINCE.search(str(text)) or _UNTIL.search(str(text))) and (definition.top is not None or definition.skip):
        raise ValueError(
            "{{since}} / {{until}} cannot be combined with $top or $skip: the bounds would "
            "apply to each refresh window instead of the whole table. Remove $top / $skip, "
            "or remove the placeholders and choose a cursor column (the table is then "
            "read whole on each refresh)."
        )
    subforms = {}
    for table in tables or []:
        meta = ((table or {}).get("metadata_json") or {}).get("priority_erp") or {}
        if (table or {}).get("name") and (meta.get("path") or meta.get("parents")):
            subforms[str(table["name"]).upper()] = meta
    refuse_subform(definition, subforms)


def refuse_subform(definition: Definition, subforms: dict) -> None:
    meta = (subforms or {}).get(definition.form.upper())
    if meta is None:
        return
    parent = meta.get("subform_of") or ", ".join(meta.get("parents") or []) or "its parent form"
    raise ValueError(
        f"{definition.form} is a sub-form of {parent}. Sub-forms are not supported in a "
        f"Priority custom table in this version: Priority returns their rows only nested "
        f"inside the parent, which cannot be counted or read in pages. Cache {parent} instead."
    )


def render_window(query: str, column: str, kind, window) -> str:
    """`query` restricted to rows whose `column` is in the window.

    With `{{since}}` / `{{until}}` in the definition they are filled in (which
    is how `$since={{since}}` is written). Otherwise the condition is and-ed
    onto the definition's `$filter`.
    """
    from app.data_sources.fast.incremental import IncrementalConfigError

    def lit(value):
        try:
            return odata_literal(value)
        except ValueError as e:
            raise IncrementalConfigError(str(e)) from None

    if _SINCE.search(query) or _UNTIL.search(query):
        try:
            check_definition(query)
        except ValueError as e:
            raise IncrementalConfigError(str(e)) from None
        like = window.lo if window.lo is not None else window.hi
        number = kind == "number" or (isinstance(like, (int, float, Decimal)) and not isinstance(like, bool))
        if kind == "text" and window.lo is None:
            lo_text = "''"
        else:
            lo_text = lit(window.lo if window.lo is not None else (-(2 ** 63) if number else _FLOOR))
        out = _SINCE.sub(lambda _m: lo_text, query)
        if _UNTIL.search(out):
            if kind == "text":
                raise IncrementalConfigError("{{until}} is not available for a text cursor; use {{since}} only.")
            hi = window.hi if window.hi is not None else (2 ** 63 - 1 if number else _CEILING)
            out = _UNTIL.sub(lambda _m: lit(hi), out)
        return out

    if window.lo is None and window.hi is None:
        return query
    if is_bounded(query):
        # Read whole; the window is taken from the result (see `is_bounded`).
        return query
    if not _NAME.match(column or ""):
        raise IncrementalConfigError(
            f"'{column}' cannot be used as a Priority cursor: it must be a column of the "
            f"form, such as UDATE."
        )
    try:
        definition = parse_definition(query)
    except ValueError as e:
        raise IncrementalConfigError(str(e)) from None
    parts = []
    if window.lo is not None:
        parts.append(f"{column} ge {lit(window.lo)}")
    if window.hi is not None:
        parts.append(f"{column} lt {lit(window.hi)}")
    predicate = " and ".join(parts)
    if window.include_nulls:
        predicate = f"({predicate}) or {column} eq null"
    combined = f"({definition.filter}) and ({predicate})" if definition.filter else predicate
    return replace(definition, filter=combined).text()
