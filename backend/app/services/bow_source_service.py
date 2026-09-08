"""Tabular BOW source. Every SQL statement starts with the caller's console scope."""
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import case, func, select, cast, String, Integer

from app.core.console_access import resolve_console_scope
from app.models.agent_execution import AgentExecution as AE
from app.models.tool_execution import ToolExecution as TE
from app.models.user import User
from app.models.report import Report
from app.models.data_source import DataSource
from app.models.report_data_source_association import report_data_source_association as assoc
from app.schemas.bow_source_schema import BowQuery, MAX_ROWS, MAX_GROUPS
from app.services.diagnosis.compiler import compile_query, tool_match_conditions, stale_clause
from app.services.diagnosis.service import diagnosis_service, RunQueryParams, _utc_naive
from app.services.diagnosis.rollup import pending_clause


RUN_FIELDS = {
    "run_id": AE.id, "report_id": AE.report_id, "completion_id": AE.completion_id,
    "created_at": AE.created_at, "prompt": AE.prompt_text, "error": AE.error_text,
    "user_id": AE.user_id, "user_name": User.name, "user_email": User.email,
    "report_title": Report.title, "platform": AE.platform,
    "model": AE.primary_model_id, "provider": AE.primary_provider,
    "cost_usd": AE.total_cost_usd, "cost_is_partial": AE.cost_is_partial,
    "tokens": AE.total_tokens, "duration_ms": AE.total_duration_ms,
    "feedback_direction": AE.feedback_direction, "feedback_message": AE.feedback_message,
    "judge_confidence": AE.judge_response_score,
    "judge_instructions": AE.judge_instructions_score, "judge_context": AE.judge_context_score,
    "tool_count": AE.tool_count, "failed_tool_count": AE.failed_tool_count,
    "turn": AE.turn_index, "eval": AE.is_eval_run,
}
TOOL_FIELDS = {
    "call_id": TE.id, "run_id": TE.agent_execution_id, "report_id": AE.report_id,
    "created_at": TE.created_at, "tool": TE.tool_name, "action": TE.tool_action,
    "attempt": TE.attempt_number, "duration_ms": TE.duration_ms,
    "error": TE.error_message, "output_preview": TE.result_summary,
    "step_id": TE.created_step_id, "args_preview": cast(TE.arguments_json, String),
}
NUMERIC = {"cost_usd", "tokens", "duration_ms", "tool_count", "failed_tool_count", "turn",
           "judge_confidence", "judge_instructions", "judge_context", "attempt", "feedback_direction"}
DEFAULT_COLUMNS = {
    "runs": ["run_id", "report_id", "created_at", "agent_names", "user_name", "prompt", "status",
             "cost_usd", "cost_is_partial", "tokens", "duration_ms", "failed_tool_count"],
    "tool_calls": ["call_id", "run_id", "report_id", "created_at", "tool", "action", "status",
                   "attempt", "duration_ms", "error", "output_preview"],
}


def catalog():
    """Static schemas contain no user data or hidden agent names."""
    return {"bow." + dataset: list(fields) + ["status", "agent_id", "agent_name", "day", "hour", "week"]
            + (["agent_ids", "agent_names"] if dataset == "runs" else [])
            for dataset, fields in (("runs", RUN_FIELDS), ("tool_calls", TOOL_FIELDS))}


def column_type(name):
    if name in NUMERIC:
        return "number"
    if name in {"agent_ids", "agent_names"}:
        return "array"
    if name in {"eval", "cost_is_partial"}:
        return "boolean"
    return "datetime" if name == "created_at" else "string"


class BowSourceService:
    async def query(self, db, organization, user, request, *, exclude_report_id=None):
        q = request if isinstance(request, BowQuery) else BowQuery.model_validate(request)
        scope = await resolve_console_scope(db, organization, user)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        r = q.time_range
        if r.relative:
            n = int(r.relative[:-1])
            delta = timedelta(days=n) if r.relative[-1] == "d" else timedelta(hours=n)
            start, end = now - delta, now
        else:
            if r.start.tzinfo is None or r.end.tzinfo is None:
                raise ValueError("Explicit start/end must include a timezone")
            start, end = _utc_naive(r.start), _utc_naive(r.end)
        if not timedelta(0) < end - start <= timedelta(days=366):
            raise ValueError("Time range must be positive and at most 366 days")
        p = RunQueryParams(q=q.query, start=start, end=end, tz_offset_minutes=q.tz_offset_minutes)
        parsed = diagnosis_service._parse(q.query)
        ctx = diagnosis_service._ctx(p)
        base = diagnosis_service._base_where(str(organization.id), scope.data_source_ids, p, parsed.ast)
        from app.services.bow_source_access import visible_reports_clause
        base.append(await visible_reports_clause(db, organization.id, user))
        if exclude_report_id:
            from sqlalchemy import or_
            base.append(or_(AE.report_id.is_(None), AE.report_id != str(exclude_report_id)))
        # Do not let an unindexed rollup disappear behind a filter on its empty columns.
        pending = await db.scalar(diagnosis_service._from(select(func.count())).where(*base, pending_clause(now)))
        if pending:
            raise ValueError("BOW history is still indexing; retry after indexing completes")
        clause = compile_query(parsed.ast, ctx)
        if clause is not None:
            base.append(clause)
        source_reports = (await db.execute(diagnosis_service._from(select(AE.report_id))
            .where(*base, AE.report_id.isnot(None)).distinct().limit(MAX_ROWS + 1))).scalars().all()
        if len(source_reports) > MAX_ROWS:
            raise ValueError("Query spans too many reports; narrow the time window")
        fields = dict(RUN_FIELDS if q.dataset == "runs" else TOOL_FIELDS)
        raw_status = AE.status if q.dataset == "runs" else TE.status
        status = case((raw_status == "completed", "success"), else_=raw_status)
        if q.dataset == "runs":
            status = case((stale_clause(ctx), "stale"), else_=status)
        fields["status"] = status
        fields["agent_id"], fields["agent_name"] = DataSource.id, DataSource.name
        dialect = db.get_bind().dialect.name
        bucket_time = AE.created_at if q.dataset == "runs" else TE.created_at
        shifted = bucket_time + func.make_interval(0, 0, 0, 0, 0, q.tz_offset_minutes) if dialect == "postgresql" else None
        for gran, fmt in (("day", "%Y-%m-%d"), ("hour", "%Y-%m-%dT%H:00:00"), ("week", "%Y-%W")):
            fields[gran] = func.date_trunc(gran, shifted) if shifted is not None else func.strftime(fmt, bucket_time, f"{q.tz_offset_minutes} minutes")
        if dialect != "postgresql":
            local_time = func.datetime(bucket_time, f"{q.tz_offset_minutes} minutes")
            days_since_monday = (cast(func.strftime("%w", local_time), Integer) + 6) % 7
            fields["week"] = func.date(local_time, func.printf("-%d days", days_since_monday))
        # Use portable, stable text buckets rather than driver-dependent datetime objects.
        elif shifted is not None:
            for gran, fmt in (("day", "YYYY-MM-DD"), ("hour", 'YYYY-MM-DD"T"HH24:00:00'), ("week", "YYYY-MM-DD")):
                fields[gran] = func.to_char(func.date_trunc(gran, shifted), fmt)
        requested = q.group_by if q.metrics else (q.columns or DEFAULT_COLUMNS[q.dataset])
        list_fields = {"agent_ids", "agent_names"} if q.dataset == "runs" and not q.metrics else set()
        unknown = set(requested) - set(fields) - list_fields
        if unknown:
            raise ValueError(f"Unknown {q.dataset} columns: {', '.join(sorted(unknown))}")
        if len(set(requested)) != len(requested):
            raise ValueError("Duplicate columns are not supported")
        columns = [fields[k].label(k) for k in requested if k not in list_fields]
        outputs = {k: fields[k] for k in requested if k in fields}
        for metric in q.metrics:
            if metric.name in outputs:
                raise ValueError("Metric names must be unique and different from grouping fields")
            if metric.op == "count":
                if metric.field is not None:
                    raise ValueError("count counts rows; use count_distinct for a field")
                expr = func.count()
            else:
                if metric.field not in fields:
                    raise ValueError("Unknown metric field")
                if metric.op in ("sum", "avg") and metric.field not in NUMERIC:
                    raise ValueError("sum/avg require a numeric field")
                expr = (func.count(func.distinct(fields[metric.field])) if metric.op == "count_distinct"
                        else getattr(func, metric.op)(fields[metric.field]))
            outputs[metric.name] = expr
            columns.append(expr.label(metric.name))
        # Include report identity internally to materialize list-valued agent columns.
        internal_report = bool(list_fields.intersection(requested)) and "report_id" not in requested
        if internal_report:
            columns.append(AE.report_id.label("_report_id"))
        stmt = diagnosis_service._from(select(*columns)).where(*base)
        if q.dataset == "tool_calls":
            stmt = stmt.join(TE, TE.agent_execution_id == AE.id).where(*tool_match_conditions(parsed.ast, ctx))
        expanded = any(k in ("agent_id", "agent_name") for k in requested) or any(m.field in ("agent_id", "agent_name") for m in q.metrics)
        if expanded:
            stmt = stmt.outerjoin(assoc, assoc.c.report_id == AE.report_id).outerjoin(DataSource, DataSource.id == assoc.c.data_source_id)
        if q.metrics:
            stmt = stmt.group_by(*[fields[k] for k in q.group_by]) if q.group_by else stmt
        for sort in q.sort:
            if sort.field not in outputs:
                raise ValueError("Sort fields must be included in the result")
            stmt = stmt.order_by(getattr(outputs[sort.field], sort.direction)())
        if not q.metrics:
            stmt = stmt.order_by(AE.created_at.desc(), AE.id.desc())
            if q.dataset == "tool_calls":
                stmt = stmt.order_by(TE.id)
        cap = MAX_GROUPS if q.metrics else MAX_ROWS
        if q.limit and q.limit > cap:
            raise ValueError(f"Maximum result size is {cap}")
        records = [dict(row) for row in (await db.execute(stmt.limit((q.limit or cap) + 1))).mappings()]
        if not q.limit and len(records) > cap:
            raise ValueError(f"Result exceeds {cap} rows; narrow the query, aggregate, or request an explicit limit")
        if q.limit:
            records = records[:q.limit]
        if list_fields.intersection(requested):
            report_ids = {r.get("report_id", r.get("_report_id")) for r in records}
            agents = {}
            for rid, aid, name in (await db.execute(select(assoc.c.report_id, DataSource.id, DataSource.name)
                .join(DataSource, DataSource.id == assoc.c.data_source_id)
                .where(assoc.c.report_id.in_(report_ids)).order_by(DataSource.name, DataSource.id))):
                agents.setdefault(rid, []).append((str(aid), name))
            for row in records:
                values = agents.get(row.get("report_id", row.pop("_report_id", None)), [])
                if "agent_ids" in requested:
                    row["agent_ids"] = [v[0] for v in values]
                if "agent_names" in requested:
                    row["agent_names"] = [v[1] for v in values]
        for row in records:
            for preview in ("args_preview", "output_preview"):
                if row.get(preview):
                    row[preview] = row[preview][:500]
        names = requested + [m.name for m in q.metrics]
        df = pd.DataFrame(records, columns=names)
        # The typed source owns numeric normalization, including all-null and
        # empty results, so generated code can use aggregates directly.
        for name in (set(names) & NUMERIC) | {m.name for m in q.metrics}:
            df[name] = pd.to_numeric(df[name])
        df.attrs["bow_source"] = {"version": 1, "source_id": "builtin:bow", "organization_id": str(organization.id),
            "report_ids": [str(r) for r in source_reports],
            "scope_ids": scope.data_source_ids, "principal_id": str(user.id), "request": q.model_dump(mode="json"),
            "start": start.isoformat() + "Z", "end": end.isoformat() + "Z", "complete": True}
        return df
