"""Materializing from Power BI through the `executeQueries` REST API.

Power BI is the second accelerated source that is not a database. A custom
table here is a DAX query (`EVALUATE <table expression>`) against one semantic
model, and every read is one HTTP POST to `/datasets/{id}/executeQueries` that
runs it and returns the rows as JSON. Everything below follows from what that
endpoint does, measured against a live tenant rather than read off the docs.

**A DAX query does not say which model it targets.** The endpoint is addressed
by dataset id, but the text only names tables. The target is resolved from the
tables the DAX references, matched against the connection's indexed catalog
(`Dataset/Table` rows carrying `powerbi.datasetId`); an admin can also pin the
semantic model explicitly, which wins. A reference that matches tables in two
models is refused rather than guessed.

**One response carries at most 100,000 rows or 1,000,000 values, and says
nothing when it truncates.** `EVALUATE GENERATESERIES(1, 150000)` returned
exactly 100,000 rows with HTTP 200; a 12-column table came back at 83,333 rows,
which is one million values, with no flag and no error. Materializing that as
if it were complete is the worst outcome available here, because nothing about
it looks wrong afterwards.

**`WINDOW` cannot page a base table.** DAX's window functions are the obvious
paging primitive, and they work on a distinct result, but on a plain table the
engine answers "WINDOW's Relation parameter may have duplicate rows. This is
not allowed." — and a fact table with no key is exactly the table most worth
caching. So paging is not by position.

**Estimating — an exact count.** `ROW("n", COUNTROWS(<expr>))` is one request
and returns the true row count. This is the number that makes truncation
detectable, and it is what the budget check sees.

**Extracting — value windows over a numeric or date column, verified against
the count.** Under the ceiling the whole result is one request. Above it, the
expression is fetched in windows `FILTER(<expr>, col >= lo && col < hi)` over
a column chosen from a sample: numeric or datetime, with the most spread. Only
the last window includes its top, so the windows partition the rows and a tie
on the cursor can neither be skipped nor duplicated. A window that comes back
at the ceiling — by rows, by values, or by response bytes — is split and
retried rather than trusted; rows whose cursor is blank fall outside every
window and are fetched on their own. Then the total is compared against the
count and a shortfall aborts the refresh, leaving the previous artifact in
service.

**Cancelling — nothing to cancel.** A request that has been answered is over;
stopping means not asking for the next window.
"""

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Measured ceilings of one executeQueries response. Both truncate silently.
MAX_RESPONSE_ROWS = 100_000
MAX_RESPONSE_VALUES = 1_000_000
# The documented cap on a response body is 15 MB. A response this large is
# treated as possibly truncated and split, before finding out the hard way.
MAX_RESPONSE_BYTES = 12 * 1024 * 1024

# Rows sampled to pick a window column and learn the result's width.
SAMPLE_ROWS = 200

# A windowed extraction that needs more requests than this has stopped
# converging. Bounded so a refresh fails with a reason rather than spending the
# tenant's rate budget (~120 requests/min) on nothing.
MAX_WINDOW_REQUESTS = 512

_ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$")
_INTERNAL_COL_RE = re.compile(r"RowNumber-[0-9A-Fa-f-]{36}")


class PowerBISource:
    """Extraction over Power BI's executeQueries endpoint."""

    def __init__(self, client):
        self.client = client

    # -- the protocol -----------------------------------------------------

    def estimate(self, sql: str):
        """The exact row count, and whether this result can be materialized.

        Never raises: a failure downgrades to `supported=False` and the
        extractor falls back to its hard caps, same as every other source.
        """
        from app.data_sources.fast.extractor import Estimate
        from app.data_sources.fast.sql_dialect import ASSUMED_ROW_WIDTH_BYTES

        try:
            q = parse_dax(sql)
            target = self._target(sql)
            rows = self._count(target, q)
        except Exception as e:
            return Estimate(supported=False, note=f"Power BI COUNTROWS() failed: {e}")

        est = Estimate(
            rows=rows,
            width_bytes=ASSUMED_ROW_WIDTH_BYTES,
            total_bytes=rows * ASSUMED_ROW_WIDTH_BYTES,
            note="exact row count from COUNTROWS(); row width estimated",
        )
        if rows == 0:
            return est
        try:
            sample = self._sample(target, q)
        except Exception as e:
            est.note += f"; sample failed: {e}"
            return est
        ceiling = response_ceiling(len(sample.columns))
        if rows > ceiling and sample.cursor is None:
            # One response cannot carry the result and there is nothing to
            # window on. Refusing at save time beats discovering it on the
            # first refresh.
            est.source_max_rows = ceiling
            est.source_max_rows_note = (
                f"Power BI returns at most {ceiling:,} rows per query for a result "
                f"this wide (100,000 rows or 1,000,000 values, whichever comes "
                f"first) and cannot page through more, unless the result carries "
                f"a numeric or date column to fetch it in windows. Add one to the "
                f"result, aggregate or filter the query."
            )
        return est

    def preview(self, sql: str, limit: int) -> Tuple[List[dict], List[list]]:
        """First `limit` rows — one bounded request, never the full result."""
        q = parse_dax(sql)
        target = self._target(sql)
        # TOPN picks the rows; the query's own ORDER BY (when it was simple
        # enough to feed TOPN) is kept so the sample is also *shown* in that
        # order rather than in whatever order the engine returned it.
        rows, _nbytes = self._run(
            target,
            q.statement(preview_expr(q, limit), q.order_by if q.order_items else ""),
        )
        # TOPN keeps ties, so it may hand back more than asked for.
        rows = rows[:limit]
        names = _names(rows)
        clean = _clean_names(names)
        return [{"name": c, "dtype": None} for c in clean], [
            [_scalar(r.get(n)) for n in names] for r in rows
        ]

    def stream_batches(self, sql: str, batch_rows: int) -> Iterator["object"]:
        """Yield pyarrow.Table batches covering the whole result, or fail.

        The count is taken first and checked last; what the caller is promised
        is that a batch sequence which reaches the end is complete.
        """
        q = parse_dax(sql)
        target = self._target(sql)
        total = self._count(target, q)
        if total == 0:
            # The API returns no column names for an empty result, so there is
            # no shape to create the relation with.
            raise RuntimeError(
                "This query returns no rows. A custom table needs at least one "
                "row to learn its columns; widen the filter or check the model."
            )

        sample = self._sample(target, q)
        ceiling = response_ceiling(len(sample.columns))
        schema = [None]

        if total < ceiling:
            rows, nbytes = self._run(target, q.statement(q.body))
            if nbytes >= MAX_RESPONSE_BYTES and len(rows) < total:
                raise RuntimeError(
                    f"The result is too large for one Power BI response "
                    f"({nbytes / (1024 * 1024):.0f} MB) and only {len(rows):,} of "
                    f"{total:,} rows arrived. Narrow the query or select fewer columns."
                )
            _verify(len(rows), total)
            yield from _tables(rows, batch_rows, schema)
            return

        yield from self._windowed(target, q, sample, total, ceiling, batch_rows, schema)

    # -- windowed extraction ----------------------------------------------

    def _windowed(self, target, q, sample, total, ceiling, batch_rows, schema):
        cursor = sample.cursor
        if cursor is None:
            raise RuntimeError(
                f"This query returns {total:,} rows and Power BI serves at most "
                f"{ceiling:,} per request for a result this wide, with no way to "
                f"page through the rest. Extracting it in windows needs a numeric "
                f"or date column in the result; add one, or aggregate or filter "
                f"the query."
            )
        kind = sample.kinds[cursor]
        lo, hi, blanks = self._range(target, q, cursor, kind)
        if lo is None or hi is None:
            raise RuntimeError(f"Could not read the range of {cursor}.")

        emitted = 0
        requests_made = 0

        if blanks:
            # A blank cursor falls outside every window; fetch those rows on
            # their own rather than lose them or refuse the extraction.
            if blanks >= ceiling:
                raise RuntimeError(
                    f"{blanks:,} rows have no {cursor} value, more than one Power "
                    f"BI response can carry. Filter them out or window on another "
                    f"column."
                )
            requests_made += 1
            rows, _n = self._run(
                target, q.statement(f"FILTER({q.body}, ISBLANK({cursor}))")
            )
            emitted += len(rows)
            yield from _tables(rows, batch_rows, schema)

        windows = list(reversed(_initial_windows(lo, hi, total, kind, ceiling)))
        while windows:
            w_lo, w_hi, closed = windows.pop()
            requests_made += 1
            if requests_made > MAX_WINDOW_REQUESTS:
                raise RuntimeError(
                    f"Extraction did not converge after {MAX_WINDOW_REQUESTS} "
                    f"requests. This usually means the query returns different "
                    f"rows each time it runs, or that too many rows share one "
                    f"{cursor} value."
                )
            rows, nbytes = self._run(
                target,
                q.statement(
                    f"FILTER({q.body}, {cursor} >= {_literal(kind, w_lo)} && "
                    f"{cursor} {'<=' if closed else '<'} {_literal(kind, w_hi)})"
                ),
            )
            if len(rows) >= ceiling or nbytes >= MAX_RESPONSE_BYTES:
                # At the ceiling: there may be more in this window and the
                # response cannot say. Split rather than trust it.
                mid = _midpoint(kind, w_lo, w_hi)
                if mid is None:
                    raise RuntimeError(
                        f"More than {ceiling:,} rows share a single {cursor} "
                        f"value, so the extraction cannot be split any further. "
                        f"Aggregate the query or window on another column."
                    )
                windows.append((mid, w_hi, closed))
                windows.append((w_lo, mid, False))
                continue
            emitted += len(rows)
            yield from _tables(rows, batch_rows, schema)

        _verify(emitted, total)

    # -- talking to the API -----------------------------------------------

    def _target(self, sql: str) -> dict:
        return resolve_target(self.client, sql)

    def _run(self, target: dict, dax: str):
        """One executeQueries request: (rows as dicts, response bytes)."""
        return self.client.execute_dax_rows(
            target.get("workspaceId"), target["datasetId"], dax
        )

    def _count(self, target: dict, q) -> int:
        rows, _n = self._run(target, q.statement(f'ROW("n", COUNTROWS({q.body}))'))
        if not rows:
            return 0
        value = next(iter(rows[0].values()), None)
        return int(value or 0)

    def _sample(self, target: dict, q):
        rows, _n = self._run(target, q.statement(f"TOPN({SAMPLE_ROWS}, {q.body})"))
        return _Sample(rows)

    def _range(self, target: dict, q, cursor: str, kind: str):
        """(min, max, rows-with-no-value) for the cursor, in one query."""
        rows, _n = self._run(
            target,
            q.statement(
                f'ROW("lo", MINX({q.body}, {cursor}), "hi", MAXX({q.body}, {cursor}), '
                f'"blanks", COUNTROWS(FILTER({q.body}, ISBLANK({cursor}))))'
            ),
        )
        if not rows:
            return None, None, 0
        vals = list(rows[0].values())
        lo, hi = _parse_value(kind, vals[0]), _parse_value(kind, vals[1])
        blanks = int(vals[2] or 0) if len(vals) > 2 else 0
        return lo, hi, blanks


# --------------------------------------------------------------------------
# Which semantic model a DAX text targets
# --------------------------------------------------------------------------

def resolve_target(client, dax: str) -> dict:
    """The {datasetId, workspaceId, datasetName} this DAX runs against.

    An explicit target set on the client wins. Otherwise the tables the DAX
    references are matched against the catalog attached to the client; the
    model that owns the most of them is the target, and a tie is refused.
    """
    explicit = getattr(client, "extraction_target", None) or {}
    if explicit.get("datasetId"):
        return {
            "datasetId": str(explicit["datasetId"]),
            "workspaceId": explicit.get("workspaceId"),
            "datasetName": explicit.get("datasetName"),
        }

    catalog = getattr(client, "_table_metadata_map", None) or {}
    if not catalog:
        raise RuntimeError(
            "The connection's catalog is empty, so the semantic model this DAX "
            "targets cannot be resolved. Index the connection first, or choose "
            "the semantic model explicitly."
        )

    models: dict = {}
    for name, meta in catalog.items():
        ds_id = str(meta.get("datasetId") or "")
        if not ds_id:
            continue
        m = models.setdefault(ds_id, {
            "datasetId": ds_id,
            "workspaceId": meta.get("workspaceId"),
            "datasetName": meta.get("datasetName") or name.split("/", 1)[0],
            "tables": set(),
        })
        table = str(meta.get("tableName") or name.split("/", 1)[-1]).strip().lower()
        if table:
            m["tables"].add(table)

    refs = client._dax_table_references(dax)
    scored = sorted(
        ((len(refs & m["tables"]), m) for m in models.values()),
        key=lambda t: (-t[0], t[1]["datasetName"] or ""),
    )
    if not scored or scored[0][0] == 0:
        raise RuntimeError(
            "Could not tell which semantic model this DAX targets: none of the "
            "tables it references is in the connection's catalog. Use the table "
            "names exactly as the model spells them, or choose the semantic "
            "model explicitly."
        )
    best = scored[0][0]
    tied = [m for score, m in scored if score == best]
    if len(tied) > 1:
        names = ", ".join(sorted(str(m["datasetName"]) for m in tied))
        raise RuntimeError(
            f"This DAX matches tables in {len(tied)} semantic models ({names}). "
            f"Choose the semantic model explicitly."
        )
    m = tied[0]
    return {
        "datasetId": m["datasetId"],
        "workspaceId": m["workspaceId"],
        "datasetName": m["datasetName"],
    }


# --------------------------------------------------------------------------
# Taking a DAX query apart and putting it back together
# --------------------------------------------------------------------------

class ParsedDax:
    """A DAX query split into its DEFINE block, its table expression and its
    ORDER BY / START AT tail, so each derived statement can rebuild it."""

    def __init__(self, define: str, body: str, order_by: str, order_items):
        self.define = define
        self.body = body
        self.order_by = order_by          # "ORDER BY ..." verbatim, or ""
        self.order_items = order_items    # [(expr, "ASC"|"DESC")] or None

    def statement(self, expr: str, order_by: str = "") -> str:
        parts = []
        if self.define:
            parts.append(self.define)
        parts.append(f"EVALUATE {expr}")
        if order_by:
            parts.append(order_by)
        return "\n".join(parts)


def parse_dax(dax: str) -> ParsedDax:
    """Split `dax` around its single EVALUATE.

    Uses the client's own lexer, so comments, string literals and bracketed
    names never look like keywords. Exactly one EVALUATE is allowed: the
    endpoint accepts one query per request, and a custom table is one relation.
    """
    from app.data_sources.clients.powerbi_client import PowerBIClient

    # Comments go first, for a concrete reason: the body is spliced into
    # generated statements (`COUNTROWS(<body>)`), and a line comment at the
    # end of the admin's text would swallow the closing parenthesis.
    text = PowerBIClient._DAX_TOKEN_RE.sub(
        lambda m: " " if m.lastgroup in ("block_comment", "line_comment") else m.group(),
        dax or "",
    )
    text = text.strip().rstrip(";").strip()
    if not text:
        raise RuntimeError("Query cannot be empty")

    tokens = [
        (m.lastgroup, m.group(), m.start(), m.end())
        for m in PowerBIClient._DAX_TOKEN_RE.finditer(text)
    ]
    evals = [i for i, t in enumerate(tokens) if t[0] == "word" and t[1].upper() == "EVALUATE"]
    if not evals:
        raise RuntimeError(
            "A Power BI custom table is a DAX query and must contain EVALUATE, "
            "for example: EVALUATE Sales"
        )
    if len(evals) > 1:
        raise RuntimeError("Use a single EVALUATE: a custom table is one result.")

    ei = evals[0]
    define = text[: tokens[ei][2]].strip()
    body_start = tokens[ei][3]
    body_end = len(text)
    order_start = None

    depth = 0
    for i in range(ei + 1, len(tokens)):
        kind, tok, start, _end = tokens[i]
        if kind == "other":
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
            continue
        if kind == "word" and depth == 0:
            up = tok.upper()
            nxt = tokens[i + 1][1].upper() if i + 1 < len(tokens) else ""
            if (up == "ORDER" and nxt == "BY") or (up == "START" and nxt == "AT"):
                order_start = start
                body_end = start
                break

    body = text[body_start:body_end].strip()
    if not body:
        raise RuntimeError("EVALUATE needs a table expression, for example: EVALUATE Sales")
    order_by = text[order_start:].strip() if order_start is not None else ""
    return ParsedDax(define, body, order_by, _order_items(order_by))


def _order_items(order_by: str):
    """[(expr, dir)] from an ORDER BY clause, or None when it is not simple.

    Only `<expr> [ASC|DESC]` items separated by top-level commas are read; a
    START AT tail, or anything else, makes the clause opaque and the preview
    falls back to an unordered TOPN.
    """
    if not order_by:
        return None
    m = re.match(r"^ORDER\s+BY\s+(.*)$", order_by, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    rest = m.group(1)
    if re.search(r"\bSTART\s+AT\b", rest, re.IGNORECASE):
        return None
    items = []
    for raw in _split_top_level(rest):
        item = raw.strip()
        if not item:
            return None
        direction = "ASC"
        dm = re.match(r"^(.*?)\s+(ASC|DESC)$", item, re.IGNORECASE | re.DOTALL)
        if dm:
            item, direction = dm.group(1).strip(), dm.group(2).upper()
        items.append((item, direction))
    return items or None


def _split_top_level(s: str) -> List[str]:
    out, depth, cur, in_str, in_q = [], 0, [], False, False
    for ch in s:
        if ch == '"' and not in_q:
            in_str = not in_str
        elif ch == "'" and not in_str:
            in_q = not in_q
        elif not in_str and not in_q:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def preview_expr(q: ParsedDax, limit: int) -> str:
    """The admin's expression bounded to `limit` rows.

    TOPN rather than a client-side cut, because an unbounded EVALUATE of a
    large model would pull the whole (truncated) result down the wire for a
    preview. The query's own ORDER BY, when it is simple, becomes TOPN's order
    so the sample is the top of what the admin asked for, not an arbitrary
    slice. Ties can make TOPN return more than `limit`; the caller cuts.
    """
    n = int(limit)
    if q.order_items:
        order = ", ".join(f"{expr}, {d}" for expr, d in q.order_items)
        return f"TOPN({n}, {q.body}, {order})"
    return f"TOPN({n}, {q.body})"


# --------------------------------------------------------------------------
# Windows over a value column
# --------------------------------------------------------------------------

KIND_INT = "int"
KIND_FLOAT = "float"
KIND_DATETIME = "datetime"


class _Sample:
    """What a TOPN sample says about the result: its columns, and which of
    them could carry a window."""

    def __init__(self, rows: List[dict]):
        self.rows = rows
        self.columns = [c for c in _names(rows)]
        self.kinds: dict = {}
        self.cursor: Optional[str] = None
        self._classify()

    def _classify(self):
        best = None
        for col in self.columns:
            values = [r.get(col) for r in self.rows if r.get(col) is not None]
            if not values:
                continue
            kind = _kind_of(values)
            if kind is None:
                continue
            self.kinds[col] = kind
            distinct = len({_parse_value(kind, v) for v in values})
            if distinct < 2:
                continue
            # Most spread wins; a date beats a number on a tie, since dates are
            # what fact tables are usually ordered by.
            key = (distinct, 1 if kind == KIND_DATETIME else 0)
            if best is None or key > best[0]:
                best = (key, col)
        self.cursor = best[1] if best else None


def _kind_of(values) -> Optional[str]:
    if all(isinstance(v, bool) for v in values):
        return None
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return KIND_INT
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return KIND_FLOAT
    if all(isinstance(v, str) and _ISO_DT_RE.match(v) for v in values):
        return KIND_DATETIME
    return None


def _parse_value(kind: str, v):
    if v is None:
        return None
    if kind == KIND_DATETIME:
        return v if isinstance(v, datetime) else _parse_dt(v)
    if kind == KIND_INT:
        try:
            return int(v)
        except (TypeError, ValueError):
            return float(v)
    return float(v)


def _literal(kind: str, v) -> str:
    """A DAX literal that compares correctly against a column of `kind`."""
    if kind == KIND_DATETIME:
        return 'dt"' + v.strftime("%Y-%m-%dT%H:%M:%S") + '"'
    if kind == KIND_INT and float(v).is_integer():
        return str(int(v))
    return repr(float(v))


def _initial_windows(lo, hi, total: int, kind: str, ceiling: int):
    """The windows to start from, sized by the count instead of guessed.

    Aiming at half the ceiling per window, because data is rarely uniform
    along its cursor and a window sized to the average tips over on any dense
    stretch. Whatever still overflows is split by the loop.
    """
    target = max(1, ceiling // 2)
    count = max(1, min(math.ceil(total / target), MAX_WINDOW_REQUESTS // 2))
    if count == 1:
        return [(lo, hi, True)]
    if kind == KIND_DATETIME:
        span = hi - lo
        if span < timedelta(seconds=count):
            return [(lo, hi, True)]
        step = span / count
        bounds = [lo + step * i for i in range(count)] + [hi]
        bounds = [b.replace(microsecond=0) for b in bounds[:-1]] + [hi]
    else:
        span = float(hi) - float(lo)
        if kind == KIND_INT and span < count:
            return [(lo, hi, True)]
        step = span / count
        bounds = [lo + step * i for i in range(count)] + [hi]
        if kind == KIND_INT:
            bounds = [int(b) for b in bounds[:-1]] + [hi]
    if any(bounds[i] >= bounds[i + 1] for i in range(count)):
        return [(lo, hi, True)]
    return [(bounds[i], bounds[i + 1], i == count - 1) for i in range(count)]


def _midpoint(kind: str, lo, hi):
    """The split point of a window, or None when it cannot be split further."""
    if kind == KIND_DATETIME:
        if hi - lo < timedelta(seconds=2):
            return None
        mid = (lo + (hi - lo) / 2).replace(microsecond=0)
    elif kind == KIND_INT:
        if hi - lo < 2:
            return None
        mid = lo + (hi - lo) // 2
    else:
        mid = lo + (hi - lo) / 2
    if mid <= lo or mid >= hi:
        return None
    return mid


def response_ceiling(columns: int) -> int:
    """Rows one response can carry for a result `columns` wide."""
    return max(1, min(MAX_RESPONSE_ROWS, MAX_RESPONSE_VALUES // max(1, columns)))


def _verify(got: int, expected: int) -> None:
    """The check the whole strategy rests on: did every counted row arrive?

    `>=` rather than `==`: the count is taken first, and a model refreshed
    between the count and the last window can legitimately deliver a few more.
    Missing rows are data loss, and only that is refused.
    """
    if got >= expected:
        return
    raise RuntimeError(
        f"Extraction returned {got:,} of {expected:,} rows. Power BI truncates a "
        f"response without saying so, so an incomplete result is refused rather "
        f"than stored; the previously cached data is still being served. Narrow "
        f"the query, or add a numeric or date column it can be fetched by."
    )


# --------------------------------------------------------------------------
# Rows → Arrow
# --------------------------------------------------------------------------

def _names(rows: List[dict]) -> List[str]:
    """Column names in the order the API sent them, minus the engine's hidden
    row-number column, which some models leak into a bare EVALUATE."""
    if not rows:
        return []
    return [c for c in rows[0].keys() if not _INTERNAL_COL_RE.search(str(c))]


def _clean_names(names: List[str]) -> List[str]:
    from app.data_sources.clients.powerbi_client import PowerBIClient

    return PowerBIClient._clean_dax_columns(list(names))


def _parse_dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not _ISO_DT_RE.match(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _scalar(v):
    """A value DuckDB can store, from a value JSON can carry."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    import json

    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    return str(v)


def _typed_column(values):
    """Dates as dates when every value in the column is one.

    JSON has no date type, so a Power BI datetime arrives as
    "2026-06-09T00:00:00" and Arrow would store it as text — leaving every agent
    query to CAST before it can use date_trunc or a range. Converted only when
    the whole column parses; a column that is *mostly* dates is left as text
    rather than silently nulling the rest.
    """
    present = [v for v in values if v is not None]
    if not present or not all(isinstance(v, str) and _ISO_DT_RE.match(v) for v in present):
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


def _tables(rows: List[dict], batch_rows: int, schema):
    """Rows as pyarrow.Tables of at most `batch_rows`, pinned to one schema.

    `schema` is a single-element list holding the first batch's schema, which
    every later batch is cast to; without that a column all-NULL in one window
    would be typed differently in the next and the append would fail partway
    through a refresh.
    """
    if not rows:
        return
    names = _names(rows)
    clean = _clean_names(names)
    size = max(1, int(batch_rows or MAX_RESPONSE_ROWS))
    for start in range(0, len(rows), size):
        table = _arrow(names, clean, rows[start:start + size], schema[0])
        schema[0] = table.schema
        yield table


def _arrow(names, clean, rows, schema):
    from app.data_sources.fast.sources import arrow_table

    data = {}
    for raw, name in zip(names, clean):
        data[name] = _typed_column([_scalar(r.get(raw)) for r in rows])
    try:
        table = arrow_table(data)
    except Exception as e:
        raise RuntimeError(
            f"Power BI returned a column whose values do not share one type and "
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
            f"Power BI returned a different column type partway through the "
            f"extraction and the batch could not be reconciled: {e}"
        )


def _without_null_columns(table):
    """Retype all-NULL columns as strings before they become the pinned schema."""
    import pyarrow as pa

    nulls = [f.name for f in table.schema if pa.types.is_null(f.type)]
    for name in nulls:
        i = table.schema.get_field_index(name)
        table = table.set_column(i, pa.field(name, pa.string()), table.column(i).cast(pa.string()))
    return table
