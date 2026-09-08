"""AST → SQLAlchemy clause over ``agent_executions``.

Rules that keep this fast and safe:

- Every predicate is a bound parameter. ``LIKE`` values are escaped; ``*``
  becomes ``%`` only after escaping. No ``text()``, no dynamic column names.
- Run fields resolve to columns on ``agent_executions`` (the rollup columns
  included) or, for display-joined names, to ``users`` / ``reports`` which the
  base query outer-joins by primary key.
- ``tool.*`` terms that sit in the same AND group compile to ONE correlated
  ``EXISTS`` on ``tool_executions`` — "runs with a failed create_data call". A
  tool term elsewhere (alone, under OR/NOT) gets its own EXISTS.
- Dates are interpreted in the caller's timezone (``tz_offset_minutes``,
  minutes to add to UTC) and compared against the UTC-naive ``created_at``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.sql import ColumnElement

from app.models.agent_execution import AgentExecution as AE
from app.models.data_source import DataSource
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association as assoc
from app.models.tool_execution import ToolExecution as TE
from app.models.user import User
from app.services.diagnosis import fields as F
from app.services.diagnosis.grammar import QueryError


@dataclass
class CompileContext:
    now: datetime  # UTC naive
    tz_offset_minutes: int = 0


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains(col, s: str):
    return col.ilike("%" + _escape_like(s) + "%", escape="\\")


def _wild(col, s: str):
    return col.ilike(_escape_like(s).replace("*", "%"), escape="\\")


def _ci_eq(col, s: str):
    return func.lower(col) == s.lower()


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------

# Run field → (column, kind) where kind steers text semantics:
#   "exact"     case-sensitive equality (indexed columns with facet-fed values)
#   "ci"        case-insensitive equality
#   "contains"  substring (error messages)
_RUN_COLUMNS: Dict[str, Tuple[Any, str]] = {
    "judge.confidence": (AE.judge_response_score, "num"),
    "judge.instructions": (AE.judge_instructions_score, "num"),
    "judge.context": (AE.judge_context_score, "num"),
    "model": (AE.primary_model_id, "exact"),
    "provider": (AE.primary_provider, "exact"),
    "cost": (AE.total_cost_usd, "num"),
    "tokens": (AE.total_tokens, "num"),
    "tokens.in": (AE.prompt_tokens, "num"),
    "tokens.out": (AE.completion_tokens, "num"),
    "duration": (AE.total_duration_ms, "num"),
    "thinking": (AE.thinking_ms, "num"),
    "first_token": (AE.first_token_ms, "num"),
    "tools": (AE.tool_count, "num"),
    "tools.failed": (AE.failed_tool_count, "num"),
    "turn": (AE.turn_index, "num"),
    "report_id": (AE.report_id, "exact"),
    "run_id": (AE.id, "exact"),
    "version": (AE.bow_version, "exact"),
    "created": (AE.created_at, "date"),
    "eval": (AE.is_eval_run, "bool"),
    "error": (AE.error_text, "contains"),
    "report": (Report.title, "ci"),
}

_TOOL_COLUMNS: Dict[str, Tuple[Any, str]] = {
    "tool": (TE.tool_name, "ci"),
    "tool.action": (TE.tool_action, "ci"),
    "tool.status": (TE.status, "enum"),
    "tool.attempt": (TE.attempt_number, "num"),
    "tool.duration": (TE.duration_ms, "num"),
    "tool.error": (TE.error_message, "contains"),
}

# Sort keys the service accepts (name → column). ``created`` is the default.
SORT_COLUMNS: Dict[str, Any] = {
    "created": AE.created_at,
    "duration": AE.total_duration_ms,
    "tokens": AE.total_tokens,
    "cost": AE.total_cost_usd,
    "tools.failed": AE.failed_tool_count,
    "tools": AE.tool_count,
    "judge.confidence": AE.judge_response_score,
    "judge.instructions": AE.judge_instructions_score,
    "judge.context": AE.judge_context_score,
    "turn": AE.turn_index,
}


def _num(lit: dict) -> float:
    k = lit["kind"]
    if k == "number":
        return float(lit["n"])
    if k == "duration":
        return float(lit["ms"])
    if k == "money":
        return float(lit["usd"])
    raise QueryError(0, f"Expected a number, got {lit['raw']!r}")  # pragma: no cover


def _numeric_clause(col, op: str, values: List[dict]):
    if op == "eq":
        return col == _num(values[0])
    if op == "any":
        return col.in_([_num(v) for v in values])
    if op == "gt":
        return col > _num(values[0])
    if op == "gte":
        return col >= _num(values[0])
    if op == "lt":
        return col < _num(values[0])
    if op == "lte":
        return col <= _num(values[0])
    if op == "range":
        return col.between(_num(values[0]), _num(values[1]))
    raise QueryError(0, f"Unsupported operator {op}")  # pragma: no cover


def _date_bounds(lit: dict, ctx: CompileContext) -> Tuple[datetime, datetime]:
    """[start, end) in UTC-naive for a date literal, in the caller's timezone."""
    off = timedelta(minutes=ctx.tz_offset_minutes)
    local_now = ctx.now + off
    k = lit["kind"]
    if k == "date":
        if lit["gran"] == "day":
            y, m, d = (int(x) for x in lit["s"].split("-"))
            start = datetime(y, m, d)
            end = start + timedelta(days=1)
        else:
            y, m = (int(x) for x in lit["s"].split("-"))
            start = datetime(y, m, 1)
            end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1)
        return start - off, end - off
    if k == "named":
        day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if lit["s"] == "yesterday":
            day -= timedelta(days=1)
        return day - off, day + timedelta(days=1) - off
    if k == "reldate":
        delta = timedelta(days=-lit["n"]) if lit["unit"] == "d" else timedelta(hours=-lit["n"])
        return ctx.now - delta, ctx.now + timedelta(days=36500)
    raise QueryError(0, f"Expected a date, got {lit['raw']!r}")  # pragma: no cover


def _date_clause(col, op: str, values: List[dict], ctx: CompileContext):
    # A relative date is a point in time: "created:>-2d" means after that
    # instant, "created:-2d" means since it. Calendar dates are spans.
    if values[0]["kind"] == "reldate" and op != "range":
        point, _ = _date_bounds(values[0], ctx)
        if op in ("eq", "gte"):
            return col >= point
        if op == "gt":
            return col > point
        if op == "lt":
            return col < point
        return col <= point
    if op == "eq":
        lo, hi = _date_bounds(values[0], ctx)
        return and_(col >= lo, col < hi)
    if op == "range":
        lo, _ = _date_bounds(values[0], ctx)
        _, hi = _date_bounds(values[1], ctx)
        return and_(col >= lo, col < hi)
    lo, hi = _date_bounds(values[0], ctx)
    if op == "gt":
        return col >= hi
    if op == "gte":
        return col >= lo
    if op == "lt":
        return col < lo
    if op == "lte":
        return col < hi
    raise QueryError(0, f"Unsupported operator {op}")  # pragma: no cover


def _text_clause(col, kind: str, op: str, values: List[dict]):
    if op == "has":
        return col.isnot(None)
    if op == "wild":
        return _wild(col, values[0]["s"])
    if kind == "contains":
        parts = [_contains(col, v["s"]) for v in values]
        return parts[0] if len(parts) == 1 else or_(*parts)
    if kind == "exact":
        vals = [v["s"] for v in values]
        return col == vals[0] if len(vals) == 1 else col.in_(vals)
    # ci
    parts = [_ci_eq(col, v["s"]) for v in values]
    return parts[0] if len(parts) == 1 else or_(*parts)


# ---------------------------------------------------------------------------
# Run-field terms
# ---------------------------------------------------------------------------

def _status_values(values: List[dict]):
    out = []
    for v in values:
        s = v["s"]
        out.extend(["success", "completed"] if s == "success" else [s])
    return out


def _run_term(node: dict, ctx: CompileContext):
    field, op, values = node["field"], node["op"], node["values"]

    if field == "status":
        if op == "has":
            return AE.status.isnot(None)
        return AE.status.in_(_status_values(values))

    if field == "user":
        if op == "has":
            return AE.user_id.isnot(None)
        if op == "wild":
            s = values[0]["s"]
            return or_(_wild(User.name, s), _wild(User.email, s))
        parts = [or_(_ci_eq(User.name, v["s"]), _ci_eq(User.email, v["s"])) for v in values]
        return parts[0] if len(parts) == 1 else or_(*parts)

    if field == "agent":
        sub = select(assoc.c.report_id).join(DataSource, DataSource.id == assoc.c.data_source_id)
        if op == "has":
            return AE.report_id.in_(select(assoc.c.report_id))
        if op == "wild":
            sub = sub.where(_wild(DataSource.name, values[0]["s"]))
        else:
            sub = sub.where(or_(*[_ci_eq(DataSource.name, v["s"]) for v in values]))
        return AE.report_id.in_(sub)

    if field == "platform":
        if op == "has":
            return AE.platform.isnot(None)
        vals = [v["s"] for v in values]
        clause = AE.platform.in_(vals)
        if "web" in vals:
            clause = or_(clause, AE.platform.is_(None))
        return clause

    if field == "feedback":
        if op == "has":
            return and_(AE.feedback_direction.isnot(None), AE.feedback_direction != 0)
        parts = []
        for v in values:
            s = v["s"]
            if s == "positive":
                parts.append(AE.feedback_direction == 1)
            elif s == "negative":
                parts.append(AE.feedback_direction == -1)
            else:
                parts.append(or_(AE.feedback_direction == 0, AE.feedback_direction.is_(None)))
        return parts[0] if len(parts) == 1 else or_(*parts)

    col, kind = _RUN_COLUMNS[field]
    if op == "has":
        return col.isnot(None)
    if kind == "num":
        return _numeric_clause(col, op, values)
    if kind == "date":
        return _date_clause(col, op, values, ctx)
    if kind == "bool":
        return col == bool(values[0]["b"])
    return _text_clause(col, kind, op, values)


# ---------------------------------------------------------------------------
# Tool-call terms
# ---------------------------------------------------------------------------

def _tool_condition(node: dict, ctx: CompileContext):
    field, op, values = node["field"], node["op"], node["values"]
    col, kind = _TOOL_COLUMNS[field]
    if op == "has":
        return col.isnot(None)
    if kind == "enum":
        return col.in_(_status_values(values))
    if kind == "num":
        return _numeric_clause(col, op, values)
    return _text_clause(col, kind, op, values)


def _tool_exists(conditions: List[Any]):
    q = select(TE.id).where(TE.agent_execution_id == AE.id)
    for c in conditions:
        q = q.where(c)
    return exists(q.correlate(AE))


def _is_tool_term(node: dict) -> bool:
    return node["t"] == "term" and F.resolve(node["field"]).entity == F.TOOL


# ---------------------------------------------------------------------------
# Bare text
# ---------------------------------------------------------------------------

def _text_search(s: str):
    return or_(
        _contains(AE.prompt_text, s),
        _contains(AE.error_text, s),
        _tool_exists([_contains(TE.error_message, s)]),
    )


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------

def _compile_node(node: dict, ctx: CompileContext):
    t = node["t"]
    if t == "text":
        return _text_search(node["s"])
    if t == "term":
        if _is_tool_term(node):
            return _tool_exists([_tool_condition(node, ctx)])
        return _run_term(node, ctx)
    if t == "not":
        return not_(_compile_node(node["c"], ctx))
    if t == "or":
        return or_(*[_compile_node(c, ctx) for c in node["c"]])
    if t == "and":
        tool_terms = [c for c in node["c"] if _is_tool_term(c)]
        others = [c for c in node["c"] if not _is_tool_term(c)]
        parts = [_compile_node(c, ctx) for c in others]
        if tool_terms:
            parts.append(_tool_exists([_tool_condition(c, ctx) for c in tool_terms]))
        return and_(*parts)
    raise QueryError(0, f"Unknown node {t}")  # pragma: no cover


def compile_query(ast: dict, ctx: CompileContext) -> Optional[ColumnElement]:
    """The WHERE clause for a parsed query, or None for an empty query.

    The caller ANDs this with organization, scope, time range and the eval
    default; the compiler only ever expresses what the query says."""
    root = ast.get("root")
    if not root:
        return None
    return _compile_node(root, ctx)


def tool_match_conditions(ast: dict, ctx: CompileContext) -> List[Any]:
    """Conditions on ``tool_executions`` for the tool terms the query mentions
    positively at the top level — used to mark the matching calls in a run
    row (``matched_tool_call_ids``). Terms under NOT/OR are not "matches"."""
    root = ast.get("root")
    if not root:
        return []
    nodes = root["c"] if root["t"] == "and" else [root]
    return [_tool_condition(n, ctx) for n in nodes if _is_tool_term(n)]
