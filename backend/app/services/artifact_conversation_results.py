"""The data results an artifact conversation shows its viewer (``bow.conversations``).

When the agent answers on an app's conversation, each successful
``create_data`` of the answer reaches the app as a result: a capped table and,
only when it validates against the real columns, a chart suggestion taken from
the visualization the agent configured. The stream (``result`` events, before
``completed``) and the history (``results`` on assistant messages) build them
here, the same way, so a reload shows what the answer showed.

* ``shape_result`` (pure): caps (rows, columns, cell and header length,
  bytes), column types without converting any value (only an int beyond
  JavaScript's safe range becomes a string), display redaction (injected),
  and the chart check. ``truncated`` means the data was cut (rows, columns,
  cell text); a chart dropped alone is just ``chart: null``.
* ``result_line`` (pure): the exact NDJSON bytes of a result; every size limit
  is measured on them.
* ``fit_message`` (pure): a message's result bytes stay within one budget;
  ``budget_lost`` says whether that cost a table.
* ``load_redaction``: the org's display redaction, or ``ok=False`` when its PII
  settings cannot be read; then results are withheld and tool titles omitted.
* ``results_for``: the results of the thread's own steps, never another's,
  and which messages' results could not all be produced (the callers' signal
  for ``results_incomplete``).

The message budget is applied in the order the stream sends results, so a
reload cuts the same table: the stream sends each result when its tool's
final block arrives, and that block is emitted after the tool execution's
``completed_at`` is committed, so ``results_for`` orders by ``completed_at``
(then block order: tools of a parallel batch finish out of block order).
Residual: a result whose own lookup failed or was still pending (rows not yet
committed) is sent later, by a poll or the final sweep, after results whose
tools finished after it; if the budget runs out among those, the stream and a
reload may cut a different table (and disagree on ``results_incomplete``).
That needs a lookup failure or a late commit and more than
``MAX_MESSAGE_RESULT_BYTES`` of results in one message.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

MAX_ROWS = 200
MAX_COLUMNS = 30
MAX_CELL_CHARS = 1000
MAX_HEADER_CHARS = 200            # a column's headerName: with the title, a zero-row result always fits
MAX_RESULT_BYTES = 262144          # one result line
MAX_MESSAGE_RESULT_BYTES = 524288  # all result lines of one message (the broker reads 1 MiB per chunk)
RESULT_TOOLS = frozenset({"create_data"})
CHART_TYPES = {"bar_chart": "bar", "line_chart": "line", "area_chart": "area", "pie_chart": "pie",
               "scatter_plot": "scatter"}

_NO_OP_SERIES_AGGREGATIONS = frozenset({None, "sum", "avg", "min", "max"})  # over one value per category
_MAX_SAFE_INT = 2 ** 53 - 1
_NUMERIC_DTYPE = re.compile(r"^(u?int|float)\d*$")  # not "interval[...]", "category", "object"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def result_line(result: dict) -> bytes:
    """The NDJSON line of a result. ASCII only (``ensure_ascii``), so its length
    is both its byte count and the broker's UTF-16 code-unit count."""
    return (json.dumps({"type": "result", **result}, allow_nan=False) + "\n").encode()


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v):
    """Non-finite floats become null, at any depth (strict JSON)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, list):
        return [_finite(x) for x in v]
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    return v


def _deep(v, fn: Callable[[str], str]):
    """``fn`` over every string value at any depth; keys untouched."""
    if isinstance(v, str):
        return fn(v)
    if isinstance(v, list):
        return [_deep(x, fn) for x in v]
    if isinstance(v, dict):
        return {k: _deep(x, fn) for k, x in v.items()}
    return v


def _cut(s: str, limit: int = MAX_CELL_CHARS) -> str:
    return s[:limit] + "…" if len(s) > limit else s


def _column_type(values: list, dtype: str, forced_string: bool) -> str:
    if forced_string:
        return "string"
    d = (dtype or "").lower()
    if _NUMERIC_DTYPE.match(d):
        return "number"
    if d.startswith("bool"):
        return "boolean"
    if d.startswith(("datetime", "date")):
        if any(isinstance(v, str) and not _ISO.match(v) for v in values):
            return "string"
        return "datetime"
    present = [v for v in values if v is not None]
    if not present:
        return "string"
    if all(_is_number(v) for v in present):
        return "number"
    if all(isinstance(v, bool) for v in present):
        return "boolean"
    if all(isinstance(v, str) and _ISO.match(v) for v in present):
        return "datetime"
    return "string"


def _trim(result: dict, max_bytes: int) -> dict:
    """Drop trailing rows until the line fits ``max_bytes`` (the chart goes
    first: a cut table never carries one; dropping only the chart leaves
    ``truncated`` as it was, since the table is whole). May return zero rows
    that still do not fit; the caller decides."""
    size = len(result_line(result))
    if size <= max_bytes:
        return result
    out = {**result, "chart": None}
    rows = out["rows"]
    size = len(result_line(out))
    while size > max_bytes and rows:
        keep = min(len(rows) - 1, math.floor(len(rows) * max_bytes / size * 0.9))
        rows = rows[:max(keep, 0)]
        out = {**out, "rows": rows, "truncated": True}
        size = len(result_line(out))
    return out


def _distinct(rows: list, keys: list[str]) -> bool:
    """Whether each combination of ``keys`` appears once. null and "" are one
    category (BOW's charts bucket with ``String(v ?? '')``)."""
    seen = set()
    for row in rows:
        key = json.dumps(["" if row.get(k) is None else row.get(k) for k in keys], sort_keys=True, default=str)
        if key in seen:
            return False
        seen.add(key)
    return True


def _chart(view: Optional[dict], rows: list, types: dict[str, str]) -> Optional[dict]:
    """The chart the agent configured, if the rows can draw it as they are
    (no filter or top-level aggregation, the value axes numeric, and x too
    for scatter). Else None. A sum/avg/min/max series aggregation
    (``seriesStyles[].aggregation``) is accepted because the categories must
    be distinct anyway, which makes it a no-op over one value; duplicates
    mean None. ``count`` (every series style, or a pie's own aggregation) is
    a counted chart: ``aggregate: "count"``, one value column (any type, it
    is ignored), repeated x and null x allowed; ChatResult counts rows per x
    (and series), not numeric values as EChartsVisual does. Its nulls follow
    EChartsVisual and ``_distinct``: null and "" x are one category, a null
    or "" series is dropped, a pie skips a null category. Count mixed with
    another aggregation, or on a scatter, means None."""
    from app.schemas.view_schema import ViewSchema

    schema = ViewSchema.from_dict(view)
    v = schema.view if schema is not None and schema.version == "v2" else None
    kind = CHART_TYPES.get(getattr(v, "type", None))
    if v is None or kind is None or not rows:
        return None
    if getattr(v, "defaultFilters", None):
        return None
    styles = [getattr(s, "aggregation", None) for s in (getattr(v, "seriesStyles", None) or [])]
    top = getattr(v, "aggregation", None)
    count = (kind == "pie" and top == "count") or (top is None and bool(styles) and all(a == "count" for a in styles))
    if not count and (top or any(a not in _NO_OP_SERIES_AGGREGATIONS for a in styles)):
        return None
    series = None
    if kind == "pie":
        x, y = v.category, [v.value]
    elif kind == "scatter":
        x, y = v.x, [v.y]
    else:
        x, y, series = v.x, ([v.y] if isinstance(v.y, str) else list(v.y)), v.groupBy
    if not y or x not in types or x in y or any(c not in types for c in y):
        return None
    if series is not None and (series not in types or series == x or series in y or len(y) != 1):
        return None
    if count:
        # The rows per x (and series): repeated x is the point, y is ignored.
        if kind == "scatter" or len(y) != 1:
            return None
        return {"type": kind, "x": x, "y": y, "series": series, "aggregate": "count"}
    if any(types[c] != "number" for c in y):
        return None
    if kind == "scatter":
        if types[x] != "number":
            return None
    elif not _distinct(rows, [x] if series is None else [x, series]):
        return None
    return {"type": kind, "x": x, "y": y, "series": series}


def _safe_chart(view: Optional[dict], rows: list, types: dict[str, str], step_id: str) -> Optional[dict]:
    """``_chart``, but a view that cannot be read costs only the chart."""
    try:
        return _chart(view, rows, types)
    except Exception:
        logger.warning("artifact conversation result %s: chart suggestion skipped", step_id, exc_info=True)
        return None


def shape_result(*, step_id: str, block_id: Optional[str], title: str, data: Optional[dict], view: Optional[dict],
                 max_bytes: int = MAX_RESULT_BYTES, redact: Optional[Callable[[str], str]] = None) -> dict:
    """One result from a step's stored grid (``format_df_for_widget`` shape)
    and the view the agent configured. ``truncated`` means the data was cut
    (rows, columns or cell text), never only the chart. Raises ValueError
    when even the zero-row form exceeds ``max_bytes`` (row keys are never
    cut): the grid cannot be shaped."""
    data = data if isinstance(data, dict) else {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    column_info = info.get("column_info") if isinstance(info.get("column_info"), dict) else {}

    columns = []
    for c in data.get("columns") or []:
        if not isinstance(c, dict):
            continue
        field = c.get("field") or c.get("headerName")
        if field:
            columns.append({"field": str(field), "headerName": str(c.get("headerName") or field)})
    column_count = len(columns)
    columns = columns[:MAX_COLUMNS]
    fields = [c["field"] for c in columns]

    raw_rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    stored = [r for r in raw_rows if isinstance(r, dict)]
    total = info.get("total_rows")
    row_count = total if isinstance(total, int) and not isinstance(total, bool) else len(raw_rows)
    rows = [{f: _finite(r.get(f)) for f in fields} for r in stored[:MAX_ROWS]]

    # Ints JavaScript cannot hold exactly: the whole column as strings.
    forced = set()
    for f in fields:
        if any(_is_number(r[f]) and isinstance(r[f], int) and abs(r[f]) > _MAX_SAFE_INT for r in rows):
            forced.add(f)
            for r in rows:
                if isinstance(r[f], int) and not isinstance(r[f], bool):
                    r[f] = str(r[f])

    # After every step that makes a string, before typing and the cut.
    title = title or ""
    if redact is not None:
        rows = [{f: _deep(r[f], redact) for f in fields} for r in rows]
        for c in columns:
            c["headerName"] = redact(c["headerName"])
        title = redact(title)
    for c in columns:
        c["headerName"] = _cut(c["headerName"], MAX_HEADER_CHARS)

    for c in columns:
        dtype = (column_info.get(c["field"]) or {}).get("dtype") if isinstance(column_info.get(c["field"]), dict) else None
        c["type"] = _column_type([r[c["field"]] for r in rows], dtype, c["field"] in forced)

    cells_cut = False

    def cut(v: str) -> str:
        nonlocal cells_cut
        out = _cut(v)
        cells_cut = cells_cut or out is not v
        return out

    rows = [{f: _deep(r[f], cut) for f in fields} for r in rows]
    truncated = cells_cut or len(rows) < row_count or len(columns) < column_count
    chart = None if truncated else _safe_chart(view, rows, {c["field"]: c["type"] for c in columns}, step_id)
    result = {"id": step_id, "block": block_id, "title": _cut(title), "columns": columns, "rows": rows,
              "row_count": row_count, "column_count": column_count, "truncated": truncated, "chart": chart}
    result = _trim(result, max_bytes)
    if len(result["rows"]) < row_count:
        result["truncated"] = True
    if len(result_line(result)) > max_bytes:
        raise ValueError(f"result {step_id}: {len(result_line(result))} bytes without rows, over {max_bytes}")
    return result


def fit_message(results: list[dict], budget: int) -> tuple[list[dict], int]:
    """Keep results in order while their lines fit ``budget``; one that does
    not is cut to the remainder (no chart first, then rows), or dropped if
    even no rows do not fit. Returns (kept, budget left); ``budget_lost`` says
    whether that lost a table."""
    kept = []
    for r in results:
        size = len(result_line(r))
        if size > budget:
            r = _trim(r, budget)
            size = len(result_line(r))
            if size > budget:
                logger.info("artifact conversation result %s dropped: over the message budget", r.get("id"))
                continue
        kept.append(r)
        budget -= size
    return kept, budget


def budget_lost(before: list[dict], kept: list[dict]) -> bool:
    """Whether ``fit_message`` (``before`` -> ``kept``) dropped a result, or
    cut one that had rows to none: the viewer is missing a table."""
    by_id = {k["id"]: k for k in kept}
    return any(r["id"] not in by_id or (r["rows"] and not by_id[r["id"]]["rows"]) for r in before)


@dataclass(frozen=True)
class Redaction:
    """An org's display redaction, loaded once for a call or a stream.
    ``ok`` False: its PII settings could not be read, so what is shown may
    need masking that is not available; results are withheld and tool
    titles omitted (never shown raw). ``redact`` None with ``ok``: nothing
    to mask (unlicensed, disabled, no rules)."""

    redact: Optional[Callable[[str], str]]
    ok: bool
    organization_id: Optional[str] = None

    def title(self, value, tool_execution_id: Optional[str] = None):
        """A tool title for display: masked, or None when it cannot be."""
        if not isinstance(value, str) or not value:
            return value
        if not self.ok:
            return None
        try:
            return self.redact(value) if self.redact is not None else value
        except Exception:
            logger.warning("artifact conversation: tool title redaction failed (org %s, tool execution %s)",
                           self.organization_id, tool_execution_id, exc_info=True)
            return None


async def load_redaction(organization_id: str) -> Redaction:
    """The org's display redaction. A failed load is ``ok=False`` (withhold),
    never "no redaction". The loader warns, with the cause, once per failed
    read (it retries at most once per backoff window), so a caller asking on
    every line or poll during an outage does not repeat it."""
    from app.ai.llm.pii.loader import load_redactor_for_org_checked
    from app.dependencies import async_session_maker

    try:
        redactor, ok = await load_redactor_for_org_checked(organization_id, async_session_maker)
    except Exception:
        logger.warning("artifact conversation: PII redactor load failed for org %s", organization_id, exc_info=True)
        return Redaction(None, False, organization_id)
    if not ok:
        logger.debug("artifact conversation: PII settings unreadable for org %s; results withheld", organization_id)
    return Redaction(redactor.redact_display if redactor is not None else None, ok, organization_id)


async def _views(session, found: list) -> dict[str, Optional[dict]]:
    """Each step's view, from one query: the first of the tool's listed
    visualizations that belongs to the step's query, else that query's
    latest. The listed ids must match the query too, so loading by query
    covers both."""
    from app.models.visualization import Visualization

    query_ids = sorted({str(step.query_id) for _, _, _, step, _ in found})
    by_query: dict[str, list] = {}
    for v in (await session.execute(select(Visualization).where(Visualization.query_id.in_(query_ids)))).scalars():
        by_query.setdefault(str(v.query_id), []).append(v)
    out = {}
    for _, _, te, step, _ in found:
        candidates = by_query.get(str(step.query_id), [])
        listed = {str(v.id): v for v in candidates}
        ids = [str(v) for v in ((te.artifact_refs_json or {}).get("visualizations") or [])]
        viz = next((listed[i] for i in ids if i in listed), None)
        if viz is None and candidates:
            viz = max(candidates, key=lambda v: (v.created_at is not None, v.created_at or 0, str(v.id)))
        out[str(step.id)] = viz.view if viz is not None else None
    return out


async def results_for(session, *, thread_id: str, organization_id: str, completion_ids: Optional[list[str]] = None,
                      tool_execution_ids: Optional[list[str]] = None, redaction: Optional[Redaction] = None,
                      incomplete: Optional[set[str]] = None) -> dict[str, list[dict]]:
    """The results of the thread's own successful ``create_data`` steps, by
    message id, in the order their tools finished (``completed_at``, then
    block order; the stream's order, see the module docstring), each within ``MAX_RESULT_BYTES`` (the message
    budget is the caller's). A step is the thread's only when both its query
    and the run that made it belong to the thread: a tool can point at a step
    elsewhere (a dashboard's, another viewer's thread). Never unfiltered: with
    no filter, or an empty one, returns {} without a query.

    ``redaction`` is loaded here when not given. ``incomplete`` (optional)
    collects the ids of messages whose results could not all be produced:
    withheld (unreadable PII settings), a grid that cannot be shaped, or a
    database error after the main query (charts are then omitted; the tables
    stay). A database error in the main query propagates."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.models.agent_execution import AgentExecution
    from app.models.completion_block import CompletionBlock
    from app.models.query import Query
    from app.models.step import Step
    from app.models.tool_execution import ToolExecution

    if (completion_ids is None and tool_execution_ids is None) or completion_ids == [] or tool_execution_ids == []:
        return {}
    stmt = (
        select(CompletionBlock.completion_id, CompletionBlock.id, ToolExecution, Step, Query)
        .join(ToolExecution, ToolExecution.id == CompletionBlock.tool_execution_id)
        .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
        .join(Step, Step.id == ToolExecution.created_step_id)
        .join(Query, Query.id == Step.query_id)
        .where(ToolExecution.tool_name.in_(RESULT_TOOLS), ToolExecution.status == "success",
               ToolExecution.created_step_id.isnot(None), Step.status == "success",
               AgentExecution.report_id == str(thread_id), Query.report_id == str(thread_id))
        .order_by(ToolExecution.completed_at.is_(None), ToolExecution.completed_at, CompletionBlock.block_index)
    )
    if completion_ids is not None:
        stmt = stmt.where(CompletionBlock.completion_id.in_([str(c) for c in completion_ids]))
    if tool_execution_ids is not None:
        stmt = stmt.where(ToolExecution.id.in_([str(t) for t in tool_execution_ids]))
    rows = (await session.execute(stmt)).all()
    lost = incomplete if incomplete is not None else set()
    found, seen = [], set()
    for row in rows:
        if str(row[3].id) not in seen:
            seen.add(str(row[3].id))
            found.append(row)
    if not found:
        return {}
    messages = sorted({str(row[0]) for row in found})

    if redaction is None:
        redaction = await load_redaction(organization_id)
    if not redaction.ok:
        lost.update(str(row[0]) for row in found)
        return {}

    db_failed = False
    try:
        views = await _views(session, found)
    except SQLAlchemyError:
        # The transaction may be aborted (PostgreSQL): no further queries; it
        # is rolled back once the loaded rows are shaped (a rollback expires them).
        logger.warning("artifact conversation results: visualization lookup failed for thread %s, messages %s; "
                       "charts omitted", thread_id, messages, exc_info=True)
        lost.update(str(row[0]) for row in found)
        views, db_failed = {}, True

    out: dict[str, list[dict]] = {}
    for i, (completion_id, block_id, te, step, query) in enumerate(found):
        try:
            title = step.title or query.title or (te.arguments_json or {}).get("title") or ""
            out.setdefault(str(completion_id), []).append(shape_result(
                step_id=str(step.id), block_id=str(block_id), title=str(title), data=step.data,
                view=views.get(str(step.id)), redact=redaction.redact))
        except SQLAlchemyError:
            # A lazy load on a failed session: every later one would fail too.
            logger.warning("artifact conversation results: database error at step %s; stopped", step.id, exc_info=True)
            lost.update(str(row[0]) for row in found[i:])
            db_failed = True
            break
        except Exception:
            logger.warning("artifact conversation result for step %s skipped", step.id, exc_info=True)
            lost.add(str(completion_id))
    if db_failed:
        # Leave the caller's session usable (history goes on with the request's).
        try:
            await session.rollback()
        except Exception:
            logger.warning("artifact conversation results: rollback after a database error failed (thread %s, "
                           "messages %s)", thread_id, messages, exc_info=True)
    return out
