"""``run_query`` and friends: the single entry point behind the diagnosis
routes and, later, the ``list_agent_runs`` tool.

Every read anchors on ``agent_executions``, outer-joins ``users`` and
``reports`` by primary key for display, and ANDs the compiled query with the
organization, the caller's agent scope, the time range and the eval default.
Nothing here joins completions, feedback or usage tables.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_execution import AgentExecution as AE
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association as assoc
from app.models.table_usage_event import TableUsageEvent as TUE
from app.models.tool_execution import ToolExecution as TE
from app.models.user import User
from app.services.diagnosis import fields as F
from app.services.diagnosis.compiler import (
    SORT_COLUMNS,
    CompileContext,
    compile_query,
    stale_clause,
    tool_match_conditions,
)
from app.services.diagnosis.constants import STALE_AFTER
from app.services.diagnosis.grammar import QueryError, mentions_field, parse
from app.services.diagnosis.rollup import pending_clause

MAX_LIMIT = 100
DEFAULT_LIMIT = 25
PROMPT_CHARS = 200
ERROR_CHARS = 500
FACET_LIMIT = 20
TOOLS_LIMIT = 12
MAX_BUCKETS = 120
PREVIEW_CHARS = 160


@dataclass
class RunQueryParams:
    q: str = ""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    tz_offset_minutes: int = 0
    cursor: Optional[str] = None
    limit: int = DEFAULT_LIMIT
    sort: str = "created"
    sort_dir: str = "desc"
    include: Set[str] = field(default_factory=lambda: {"items", "summary", "histogram", "tools"})


class BadQuery(Exception):
    """A query that does not parse; carries the parser's structured error."""

    def __init__(self, err: QueryError):
        super().__init__(err.message)
        self.detail = {"code": "bad_query", **err.to_dict()}


class BadRequest(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.detail = {"code": "bad_request", "message": message}


def _utc_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def _dialect(db: AsyncSession) -> str:
    try:
        return db.get_bind().dialect.name
    except Exception:  # pragma: no cover
        return "sqlite"


def _reports_in_scope(scope_ids: Optional[List[str]]):
    """Reports drawing ONLY on agents in scope (mirrors
    ConsoleService._reports_of_data_sources with no caller filter)."""
    if scope_ids is None:
        return None
    return (
        select(assoc.c.report_id)
        .where(
            assoc.c.data_source_id.in_(scope_ids),
            ~assoc.c.report_id.in_(
                select(assoc.c.report_id).where(assoc.c.data_source_id.notin_(scope_ids))
            ),
        )
    )


def _one_line(text: Optional[str], limit: int) -> Optional[str]:
    if not text:
        return None
    flat = " ".join(str(text).split())
    if not flat:
        return None
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _args_preview(arguments) -> Optional[str]:
    """A compact ``key=value, key=value`` line of the call's arguments; long
    values (SQL, code) are cut, and nothing here is ever the encrypted result."""
    if not arguments or not isinstance(arguments, dict):
        return None
    parts = []
    for k, v in arguments.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{k}={_one_line(str(v), 80)}")
    return _one_line(", ".join(parts), PREVIEW_CHARS)


class DiagnosisService:
    # ------------------------------------------------------------------
    # Shared building blocks
    # ------------------------------------------------------------------
    def _parse(self, q: str):
        try:
            return parse(q or "")
        except QueryError as e:
            raise BadQuery(e) from e

    def _base_where(self, org_id: str, scope_ids: Optional[List[str]], p: RunQueryParams, ast: dict) -> List[Any]:
        if p.start is None or p.end is None:
            raise BadRequest("start and end are required")
        start, end = _utc_naive(p.start), _utc_naive(p.end)
        if end < start:
            raise BadRequest("end must be after start")
        clauses: List[Any] = [
            AE.organization_id == str(org_id),
            AE.created_at >= start,
            AE.created_at <= end,
        ]
        reports = _reports_in_scope(scope_ids)
        if reports is not None:
            clauses.append(AE.report_id.in_(reports))
        if not mentions_field(ast, "eval"):
            clauses.append(AE.is_eval_run == False)  # noqa: E712
        return clauses

    def _ctx(self, p: RunQueryParams) -> CompileContext:
        return CompileContext(now=datetime.utcnow(), tz_offset_minutes=int(p.tz_offset_minutes or 0))

    def _from(self, stmt):
        return stmt.select_from(AE).outerjoin(User, User.id == AE.user_id).outerjoin(Report, Report.id == AE.report_id)

    def _matched_ids(self, base: List[Any], q_clause):
        """Subquery of matching run ids. ``correlate(None)`` matters: an enclosing
        query that also selects from agent_executions would otherwise
        auto-correlate this one, dropping the table from its FROM and turning
        every predicate into a no-op cross join."""
        stmt = select(AE.id)
        stmt = self._from(stmt).where(*base)
        if q_clause is not None:
            stmt = stmt.where(q_clause)
        return stmt.correlate(None)

    # ------------------------------------------------------------------
    # run_query
    # ------------------------------------------------------------------
    async def run_query(
        self,
        db: AsyncSession,
        organization_id: str,
        scope_ids: Optional[List[str]],
        p: RunQueryParams,
    ) -> Dict[str, Any]:
        parsed = self._parse(p.q)
        ast = parsed.ast
        ctx = self._ctx(p)
        base = self._base_where(organization_id, scope_ids, p, ast)
        q_clause = compile_query(ast, ctx)
        out: Dict[str, Any] = {"query": {"q": p.q or "", "canonical": parsed.canonical}}

        if "items" in p.include:
            out.update(await self._items(db, base, q_clause, ast, ctx, p))
        if "summary" in p.include:
            out["summary"] = await self._summary(db, base, q_clause)
        if "histogram" in p.include:
            out["histogram"] = await self._histogram(db, base, q_clause, p)
            out["total_in_range"] = sum(b["total"] for b in out["histogram"]["buckets"])
        if "tools" in p.include:
            out["tools"] = await self._tools(db, base, q_clause)
        return out

    # -- items ---------------------------------------------------------
    def _sort(self, p: RunQueryParams):
        col = SORT_COLUMNS.get(p.sort)
        if col is None:
            raise BadRequest(f"Unknown sort {p.sort!r}")
        expr = col if p.sort == "created" else func.coalesce(col, -1)
        return expr, (p.sort_dir or "desc").lower() == "asc"

    @staticmethod
    def _encode_cursor(sort_value, run_id: str) -> str:
        if isinstance(sort_value, datetime):
            sort_value = sort_value.isoformat()
        return base64.urlsafe_b64encode(json.dumps([sort_value, run_id]).encode()).decode()

    @staticmethod
    def _decode_cursor(cursor: str, is_datetime: bool):
        try:
            value, run_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        except Exception as e:  # noqa: BLE001
            raise BadRequest("Invalid cursor") from e
        if is_datetime:
            value = datetime.fromisoformat(value)
        return value, str(run_id)

    async def _items(self, db, base, q_clause, ast, ctx, p: RunQueryParams) -> Dict[str, Any]:
        limit = max(1, min(int(p.limit or DEFAULT_LIMIT), MAX_LIMIT))
        sort_expr, asc = self._sort(p)

        stmt = select(
            AE.id, AE.created_at, AE.status, AE.prompt_text, AE.error_text, AE.platform,
            AE.user_id, User.name.label("user_name"), User.email.label("user_email"),
            AE.report_id, Report.title.label("report_title"), AE.completion_id,
            AE.primary_model_id, AE.primary_provider, AE.tool_count, AE.failed_tool_count,
            AE.total_duration_ms, AE.total_tokens, AE.total_cost_usd, AE.cost_is_partial,
            AE.judge_response_score, AE.judge_instructions_score, AE.judge_context_score,
            AE.feedback_direction, AE.feedback_message, AE.turn_index, AE.is_eval_run, AE.rollup_at,
            func.count().over().label("total"),
        )
        stmt = self._from(stmt).where(*base)
        if q_clause is not None:
            stmt = stmt.where(q_clause)
        if p.cursor:
            value, run_id = self._decode_cursor(p.cursor, p.sort == "created")
            if asc:
                stmt = stmt.where(or_(sort_expr > value, and_(sort_expr == value, AE.id > run_id)))
            else:
                stmt = stmt.where(or_(sort_expr < value, and_(sort_expr == value, AE.id < run_id)))
        order = (sort_expr.asc(), AE.id.asc()) if asc else (sort_expr.desc(), AE.id.desc())
        stmt = stmt.order_by(*order).limit(limit + 1)
        rows = (await db.execute(stmt)).all()

        has_more = len(rows) > limit
        rows = rows[:limit]
        total = int(rows[0].total) if rows else 0
        ids = [str(r.id) for r in rows]
        report_ids = sorted({str(r.report_id) for r in rows if r.report_id})

        agents: Dict[str, List[Dict[str, Any]]] = {}
        turns: Dict[str, int] = {}
        if report_ids:
            agents = await self._agents_for_reports(db, report_ids)
            for rid, n in (await db.execute(
                select(AE.report_id, func.count(AE.id))
                .where(AE.report_id.in_(report_ids), AE.is_eval_run == False)  # noqa: E712
                .group_by(AE.report_id)
            )).all():
                turns[str(rid)] = int(n)

        matched: Dict[str, List[str]] = {}
        conds = tool_match_conditions(ast, ctx)
        if conds and ids:
            for te_id, ae_id in (await db.execute(
                select(TE.id, TE.agent_execution_id).where(TE.agent_execution_id.in_(ids), *conds)
            )).all():
                matched.setdefault(str(ae_id), []).append(str(te_id))

        items = []
        stale_before = ctx.now - STALE_AFTER
        for r in rows:
            fb = r.feedback_direction
            status = "success" if r.status == "completed" else r.status
            if status == "in_progress" and r.created_at is not None and r.created_at < stale_before:
                status = "stale"
            items.append({
                "id": str(r.id),
                "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
                "status": status,
                # False until the finish hook or the startup sweep has written the
                # rollup columns; the table shows dashes, not zeros, meanwhile.
                "indexed": r.rollup_at is not None,
                "prompt": (r.prompt_text or "")[:PROMPT_CHARS],
                "error": (r.error_text or "")[:ERROR_CHARS] or None,
                "platform": r.platform or "web",
                "user": {"id": str(r.user_id) if r.user_id else None, "name": r.user_name, "email": r.user_email},
                "agents": agents.get(str(r.report_id), []),
                "report": {
                    "id": str(r.report_id) if r.report_id else None,
                    "title": r.report_title,
                    "turn": r.turn_index,
                    "turns": turns.get(str(r.report_id)),
                },
                "completion_id": str(r.completion_id) if r.completion_id else None,
                "model": r.primary_model_id,
                "provider": r.primary_provider,
                "tools": {"total": int(r.tool_count or 0), "failed": int(r.failed_tool_count or 0)},
                "duration_ms": r.total_duration_ms,
                "tokens": r.total_tokens,
                "cost_usd": r.total_cost_usd,
                "cost_is_partial": bool(r.cost_is_partial),
                "judge": {
                    "confidence": r.judge_response_score,
                    "instructions": r.judge_instructions_score,
                    "context": r.judge_context_score,
                },
                "feedback": "positive" if fb == 1 else ("negative" if fb == -1 else "none"),
                "feedback_message": r.feedback_message,
                "eval": bool(r.is_eval_run),
                "matched_tool_call_ids": matched.get(str(r.id), []),
            })

        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            sort_value = last.created_at if p.sort == "created" else getattr(last, self._sort_attr(p.sort))
            if p.sort != "created" and sort_value is None:
                sort_value = -1
            next_cursor = self._encode_cursor(sort_value, str(last.id))
        return {"items": items, "next_cursor": next_cursor, "total": total}

    async def _agents_for_reports(self, db, report_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Agents per report as the UI renders them: name plus what the
        DataSourceIcon needs (type of the first connection, connector key,
        custom icon). One query; a data source with several connections
        keeps the first."""
        from app.schemas.data_source_schema import _connector_key_from_config
        rows = (await db.execute(
            select(assoc.c.report_id, DataSource.id, DataSource.name, DataSource.icon, Connection.type, Connection.config)
            .join(DataSource, DataSource.id == assoc.c.data_source_id)
            .outerjoin(domain_connection, domain_connection.c.data_source_id == DataSource.id)
            .outerjoin(Connection, Connection.id == domain_connection.c.connection_id)
            .where(assoc.c.report_id.in_(report_ids))
            .order_by(DataSource.name, Connection.created_at)
        )).all()
        out: Dict[str, List[Dict[str, Any]]] = {}
        seen: Set[Tuple[str, str]] = set()
        for rid, ds_id, name, icon, ctype, config in rows:
            key = (str(rid), str(ds_id))
            if key in seen:
                continue
            seen.add(key)
            try:
                connector_key = _connector_key_from_config(config) if config else None
            except Exception:  # noqa: BLE001
                connector_key = None
            out.setdefault(str(rid), []).append({
                "id": str(ds_id), "name": name, "type": ctype, "connector_key": connector_key, "icon": icon,
            })
        return out

    @staticmethod
    def _sort_attr(sort: str) -> str:
        return {
            "duration": "total_duration_ms", "tokens": "total_tokens", "cost": "total_cost_usd",
            "tools.failed": "failed_tool_count", "tools": "tool_count",
            "judge.confidence": "judge_response_score", "judge.instructions": "judge_instructions_score",
            "judge.context": "judge_context_score", "turn": "turn_index",
        }[sort]

    # -- summary -------------------------------------------------------
    async def _summary(self, db, base, q_clause) -> Dict[str, Any]:
        stmt = select(
            func.count(AE.id),
            func.coalesce(func.sum(case((AE.status == "error", 1), else_=0)), 0),
            func.count(func.distinct(AE.user_id)),
            func.coalesce(func.sum(AE.total_cost_usd), 0.0),
            func.count(AE.total_duration_ms),
        )
        stmt = self._from(stmt).where(*base)
        if q_clause is not None:
            stmt = stmt.where(q_clause)
        matched, errors, users, cost, with_duration = (await db.execute(stmt)).one()
        # Runs in range the sweep has not indexed yet (rollup columns empty).
        unindexed = int((await db.execute(
            select(func.count(AE.id)).where(*base, pending_clause(datetime.utcnow()))
        )).scalar() or 0)
        p50 = None
        if with_duration:
            med = select(AE.total_duration_ms)
            med = self._from(med).where(*base, AE.total_duration_ms.isnot(None))
            if q_clause is not None:
                med = med.where(q_clause)
            med = med.order_by(AE.total_duration_ms.asc()).offset(int(with_duration) // 2).limit(1)
            p50 = (await db.execute(med)).scalar()
        return {
            "matched": int(matched or 0),
            "errors": int(errors or 0),
            "users": int(users or 0),
            "cost_usd": float(cost or 0.0),
            "p50_ms": p50,
            "unindexed": unindexed,
        }

    # -- histogram -----------------------------------------------------
    async def _histogram(self, db, base, q_clause, p: RunQueryParams) -> Dict[str, Any]:
        start, end = _utc_naive(p.start), _utc_naive(p.end)
        span = end - start
        off = int(p.tz_offset_minutes or 0)
        if span <= timedelta(days=2):
            gran, step = "hour", timedelta(hours=1)
        elif span <= timedelta(days=92):
            gran, step = "day", timedelta(days=1)
        else:
            gran, step = "week", timedelta(days=7)

        dialect = _dialect(db)
        # Bucket by local calendar hour/day; weeks are folded from days below.
        if dialect == "postgresql":
            shifted = AE.created_at + func.make_interval(0, 0, 0, 0, 0, off)
            fmt = "YYYY-MM-DD\"T\"HH24" if gran == "hour" else "YYYY-MM-DD"
            bucket = func.to_char(shifted, fmt)
        else:
            fmt = "%Y-%m-%dT%H" if gran == "hour" else "%Y-%m-%d"
            bucket = func.strftime(fmt, AE.created_at, f"{off} minutes")

        matched_case = case((q_clause, 1), else_=0) if q_clause is not None else literal(1)
        matched_err = (
            case((and_(q_clause, AE.status == "error"), 1), else_=0)
            if q_clause is not None else case((AE.status == "error", 1), else_=0)
        )
        stmt = select(
            bucket.label("b"),
            func.count(AE.id),
            func.coalesce(func.sum(matched_case), 0),
            func.coalesce(func.sum(matched_err), 0),
        )
        stmt = self._from(stmt).where(*base).group_by(bucket)
        rows = {r.b: (int(r[1]), int(r[2]), int(r[3])) for r in (await db.execute(stmt)).all()}

        # Fill every step in the range with zeros, in local time.
        local_start = start + timedelta(minutes=off)
        local_end = end + timedelta(minutes=off)
        if gran == "hour":
            cur = local_start.replace(minute=0, second=0, microsecond=0)
            key = lambda d: d.strftime("%Y-%m-%dT%H")  # noqa: E731
            iso = lambda d: d.strftime("%Y-%m-%dT%H:00")  # noqa: E731
        else:
            cur = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
            key = lambda d: d.strftime("%Y-%m-%d")  # noqa: E731
            iso = lambda d: d.strftime("%Y-%m-%d")  # noqa: E731

        buckets = []
        if gran == "week":
            while cur <= local_end and len(buckets) < MAX_BUCKETS:
                t = m = e = 0
                for i in range(7):
                    v = rows.get(key(cur + timedelta(days=i)))
                    if v:
                        t += v[0]; m += v[1]; e += v[2]
                buckets.append({"bucket": iso(cur), "total": t, "matched": m, "matched_errors": e})
                cur += step
        else:
            while cur <= local_end and len(buckets) < MAX_BUCKETS:
                v = rows.get(key(cur), (0, 0, 0))
                buckets.append({"bucket": iso(cur), "total": v[0], "matched": v[1], "matched_errors": v[2]})
                cur += step
        return {"granularity": gran, "buckets": buckets}

    # -- tools strip ---------------------------------------------------
    async def _tools(self, db, base, q_clause) -> List[Dict[str, Any]]:
        matched = self._matched_ids(base, q_clause)
        stmt = (
            select(
                TE.tool_name,
                func.count(TE.id),
                func.coalesce(func.sum(case((TE.status == "error", 1), else_=0)), 0),
                func.avg(TE.duration_ms),
            )
            .where(TE.agent_execution_id.in_(matched))
            .group_by(TE.tool_name)
            .order_by(func.count(TE.id).desc(), TE.tool_name.asc())
            .limit(TOOLS_LIMIT)
        )
        return [
            {"tool": r[0], "calls": int(r[1]), "errors": int(r[2]), "avg_ms": float(r[3]) if r[3] is not None else None}
            for r in (await db.execute(stmt)).all()
        ]

    # ------------------------------------------------------------------
    # tool calls for a page of runs
    # ------------------------------------------------------------------
    async def tool_calls(
        self, db: AsyncSession, organization_id: str, scope_ids: Optional[List[str]], run_ids: Iterable[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        ids = [str(r) for r in run_ids][:MAX_LIMIT]
        if not ids:
            return {}
        visible = select(AE.id).where(AE.organization_id == str(organization_id), AE.id.in_(ids))
        reports = _reports_in_scope(scope_ids)
        if reports is not None:
            visible = visible.where(AE.report_id.in_(reports))
        stmt = (
            select(TE)
            .where(TE.agent_execution_id.in_(visible))
            .order_by(TE.agent_execution_id, TE.started_at.asc(), TE.created_at.asc())
        )
        calls = (await db.execute(stmt)).scalars().all()
        step_ids = sorted({str(te.created_step_id) for te in calls if te.created_step_id})
        tables: Dict[str, List[str]] = {}
        if step_ids:
            for step_id, fqn in (await db.execute(
                select(TUE.step_id, TUE.table_fqn).where(TUE.step_id.in_(step_ids)).order_by(TUE.table_fqn)
            )).all():
                tables.setdefault(str(step_id), []).append(fqn)
        out: Dict[str, List[Dict[str, Any]]] = {i: [] for i in ids}
        for te in calls:
            out.setdefault(str(te.agent_execution_id), []).append({
                "id": str(te.id),
                "tool": te.tool_name,
                "action": te.tool_action,
                "status": "success" if te.status == "completed" else te.status,
                "attempt": te.attempt_number,
                "max_retries": te.max_retries,
                "duration_ms": te.duration_ms,
                "started_at": te.started_at.isoformat() + "Z" if te.started_at else None,
                "error": (te.error_message or "")[:ERROR_CHARS] or None,
                "result_summary": (te.result_summary or "")[:ERROR_CHARS] or None,
                "args_preview": _args_preview(te.arguments_json),
                "output_preview": _one_line(te.result_summary, PREVIEW_CHARS),
                "tables": tables.get(str(te.created_step_id), []) if te.created_step_id else [],
                "step_id": str(te.created_step_id) if te.created_step_id else None,
            })
        return out

    # ------------------------------------------------------------------
    # facets
    # ------------------------------------------------------------------
    async def facets(
        self,
        db: AsyncSession,
        organization_id: str,
        scope_ids: Optional[List[str]],
        field_name: str,
        p: RunQueryParams,
        prefix: str = "",
    ) -> List[Dict[str, Any]]:
        spec = F.resolve(field_name)
        if spec is None or not spec.facetable:
            raise BadRequest(f"{field_name!r} has no facets")
        parsed = self._parse(p.q)
        ast = parsed.ast
        base = self._base_where(organization_id, scope_ids, p, ast)
        q_clause = compile_query(ast, self._ctx(p))
        matched = self._matched_ids(base, q_clause)
        prefix = (prefix or "").strip()

        def _prefix(col):
            if not prefix:
                return None
            from app.services.diagnosis.compiler import _escape_like
            return col.ilike(_escape_like(prefix) + "%", escape="\\")

        name = spec.name
        if name == "user":
            stmt = (
                select(User.name, User.email, func.count(AE.id))
                .select_from(AE).join(User, User.id == AE.user_id)
                .where(AE.id.in_(matched))
                .group_by(User.name, User.email)
            )
            cond = _prefix(User.name)
            if cond is not None:
                stmt = stmt.where(or_(cond, _prefix(User.email)))
            rows = (await db.execute(stmt.order_by(func.count(AE.id).desc()).limit(FACET_LIMIT))).all()
            return [{"value": r[0] or r[1], "label": r[0] or r[1], "count": int(r[2])} for r in rows]
        if name == "agent":
            stmt = (
                select(DataSource.name, func.count(func.distinct(AE.id)))
                .select_from(AE)
                .join(assoc, assoc.c.report_id == AE.report_id)
                .join(DataSource, DataSource.id == assoc.c.data_source_id)
                .where(AE.id.in_(matched))
                .group_by(DataSource.name)
            )
            cond = _prefix(DataSource.name)
            if cond is not None:
                stmt = stmt.where(cond)
            rows = (await db.execute(stmt.order_by(func.count(func.distinct(AE.id)).desc()).limit(FACET_LIMIT))).all()
            return [{"value": r[0], "label": r[0], "count": int(r[1])} for r in rows]
        if name == "table":
            # Tables the matching calls touched, via the step each call created.
            stmt = (
                select(TUE.table_fqn, func.count(func.distinct(TE.agent_execution_id)))
                .select_from(TE)
                .join(TUE, TUE.step_id == TE.created_step_id)
                .where(TE.agent_execution_id.in_(matched), *tool_match_conditions(ast, self._ctx(p)))
                .group_by(TUE.table_fqn)
            )
            if prefix:
                from app.services.diagnosis.compiler import _escape_like
                esc = _escape_like(prefix)
                stmt = stmt.where(or_(TUE.table_fqn.ilike(esc + "%", escape="\\"), TUE.table_fqn.ilike("%." + esc + "%", escape="\\")))
            rows = (await db.execute(stmt.order_by(func.count(func.distinct(TE.agent_execution_id)).desc()).limit(FACET_LIMIT))).all()
            return [{"value": r[0], "label": r[0], "count": int(r[1])} for r in rows]
        if spec.entity == F.TOOL:
            # Values for a tool field count the calls the query's own tool terms
            # describe: "tool:create_data tool.status:" suggests the statuses of
            # create_data calls, not of every call in those runs.
            col = {"tool": TE.tool_name, "tool.action": TE.tool_action, "tool.status": TE.status}[name]
            stmt = (
                select(col, func.count(func.distinct(TE.agent_execution_id)))
                .where(TE.agent_execution_id.in_(matched), col.isnot(None), *tool_match_conditions(ast, self._ctx(p)))
                .group_by(col)
            )
            cond = _prefix(col)
            if cond is not None:
                stmt = stmt.where(cond)
            rows = (await db.execute(stmt.order_by(func.count(func.distinct(TE.agent_execution_id)).desc()).limit(FACET_LIMIT))).all()
            return [{"value": self._norm(name, r[0]), "label": self._norm(name, r[0]), "count": int(r[1])} for r in rows]

        col = {
            "status": case((stale_clause(self._ctx(p)), "stale"), else_=AE.status),
            "platform": AE.platform, "feedback": AE.feedback_direction,
            "model": AE.primary_model_id, "provider": AE.primary_provider, "version": AE.bow_version,
        }[name]
        stmt = select(col, func.count(AE.id)).where(AE.id.in_(matched)).group_by(col)
        if name not in ("status", "platform", "feedback"):
            cond = _prefix(col)
            if cond is not None:
                stmt = stmt.where(cond)
        rows = (await db.execute(stmt.order_by(func.count(AE.id).desc()).limit(FACET_LIMIT))).all()
        merged: Dict[str, int] = {}
        for value, count in rows:
            label = self._norm(name, value)
            if label is None:
                continue
            if prefix and name in ("status", "platform", "feedback") and not label.startswith(prefix.lower()):
                continue
            merged[label] = merged.get(label, 0) + int(count)
        return [
            {"value": k, "label": k, "count": v}
            for k, v in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        ][:FACET_LIMIT]

    @staticmethod
    def _norm(name: str, value) -> Optional[str]:
        if name in ("status", "tool.status"):
            return "success" if value == "completed" else (value or None)
        if name == "platform":
            return value or "web"
        if name == "feedback":
            return "positive" if value == 1 else ("negative" if value == -1 else "none")
        return value

    # ------------------------------------------------------------------
    # fields
    # ------------------------------------------------------------------
    def fields(self) -> Dict[str, Any]:
        return {
            "ast_version": 1,
            "fields": F.public_fields(),
            "quick_filters": [{"id": k, "q": v} for k, v in F.QUICK_FILTERS],
            "sorts": sorted(SORT_COLUMNS.keys()),
        }


diagnosis_service = DiagnosisService()
