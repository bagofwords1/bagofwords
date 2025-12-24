from __future__ import annotations

from typing import List, Dict, Optional, Tuple, Set
from datetime import datetime, timedelta, timezone
import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case

from app.models.organization import Organization
from app.models.user import User
from app.models.step import Step
from app.models.table_usage_event import TableUsageEvent
from app.models.table_feedback_event import TableFeedbackEvent
from app.ai.context.sections.code_section import CodeSection


class CodeContextBuilder:
    def __init__(self, db: AsyncSession, organization: Organization, current_user: Optional[User] = None):
        self.db = db
        self.organization = organization
        self.current_user = current_user

    async def build(
        self,
        data_model: Dict,
        *,
        top_k_success: int = 2,
        top_k_failure: int = 2,
        time_window_days: Optional[int] = None,
    ) -> CodeSection:
        """Build a CodeSection with curated success and failure snippets for a data model."""
        successes = await self.get_top_successful_snippets_for_data_model(
            data_model, top_k=top_k_success, time_window_days=time_window_days
        )
        failures = await self.get_top_failed_snippets_for_data_model(
            data_model, top_k=top_k_failure, time_window_days=time_window_days
        )

        lines: list[str] = []
        if successes:
            lines.append("=== SUCCESSFUL EXAMPLES ===")
            for idx, s in enumerate(successes, start=1):
                lines.append(f"[{idx}] step_id={s.get('step_id')} score={s.get('score')} success_rate={s.get('usage',{}).get('success_rate')}")
                if s.get("matched_columns"):
                    lines.append(f"matched_columns: {', '.join(s['matched_columns'])}")
                if s.get("last_used_at"):
                    lines.append(f"last_used_at: {s['last_used_at']}")
                code = s.get("code") or ""
                lines.append(code)
                lines.append("")
        if failures:
            lines.append("=== FAILED/ANTIPATTERN EXAMPLES ===")
            for idx, f in enumerate(failures, start=1):
                lines.append(f"[{idx}] step_id={f.get('step_id')} score={f.get('score')} failure_rate={f.get('usage',{}).get('failure_rate')}")
                if f.get("matched_columns"):
                    lines.append(f"matched_columns: {', '.join(f['matched_columns'])}")
                if f.get("last_used_at"):
                    lines.append(f"last_used_at: {f['last_used_at']}")
                if f.get("error_summary"):
                    lines.append(f"error_summary: {f['error_summary']}")
                code_excerpt = f.get("code_excerpt") or ""
                lines.append(code_excerpt)
                lines.append("")

        return CodeSection(content="\n".join(lines).strip())

    async def get_top_successful_snippets_for_data_model(
        self,
        data_model: Dict,
        *,
        top_k: int = 2,
        time_window_days: Optional[int] = None,
    ) -> List[Dict]:
        """Return top successful code snippets ranked by column-similarity, usage success, feedback, recency."""
        allowed_ds_ids, since_ts, now_utc = await self._get_access_and_time(time_window_days)
        if not allowed_ds_ids:
            return []

        target_cols = self._extract_generated_columns(data_model)
        usage_agg = self._build_usage_agg_subquery(allowed_ds_ids, since_ts)

        step_rows = (
            await self.db.execute(
                select(
                    Step.id,
                    Step.data_model,
                    Step.code,
                    usage_agg.c.last_used_at,
                    usage_agg.c.succ,
                    usage_agg.c.fail,
                    usage_agg.c.attempts,
                )
                .join(usage_agg, usage_agg.c.step_id == Step.id)
                .where(func.lower(Step.status) == "success")
            )
        ).all()
        if not step_rows:
            return []

        fb_map = await self._load_feedback_map(allowed_ds_ids, since_ts)
        ranked: List[Tuple[float, Dict]] = []
        for step_id, step_dm, step_code, last_used_at, succ, fail, attempts in step_rows:
            sid = str(step_id)
            step_cols = self._extract_generated_columns(step_dm or {})
            col_sim = self._jaccard_similarity(target_cols, step_cols)
            pos, neg = fb_map.get(sid, (0, 0))
            feedback_score = float(pos - neg)
            attempts_n = float(attempts or 0)
            success_rate = float(succ or 0) / attempts_n if attempts_n > 0 else 0.0
            recency, last_used_str = self._recency(now_utc, last_used_at)

            score = 0.55 * col_sim + 0.20 * success_rate + 0.20 * feedback_score + 0.05 * recency

            code_str = self._trim_code(step_code, 3000)
            ranked.append((
                score,
                {
                    "step_id": sid,
                    "score": round(score, 6),
                    "column_similarity": round(col_sim, 6),
                    "feedback": {"positive": pos, "negative": neg},
                    "usage": {"success": int(succ or 0), "failure": int(fail or 0), "attempts": int(attempts or 0), "success_rate": round(success_rate, 4)},
                    "last_used_at": last_used_str,
                    "matched_columns": sorted(list(target_cols.intersection(step_cols))),
                    "code": code_str,
                },
            ))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in (ranked[:top_k] if top_k and top_k > 0 else ranked)]

    async def get_top_failed_snippets_for_data_model(
        self,
        data_model: Dict,
        *,
        top_k: int = 2,
        time_window_days: Optional[int] = None,
    ) -> List[Dict]:
        """Return top failed code snippets (anti-patterns) ranked by column similarity,
        recency, failure evidence (usage + negative feedback). Includes raw status_reason.
        """
        allowed_ds_ids, since_ts, now_utc = await self._get_access_and_time(time_window_days)
        if not allowed_ds_ids:
            return []

        target_cols = self._extract_generated_columns(data_model)

        # Candidate steps: have usage on allowed DS and either usage marked unsuccessful or step status != success
        usage_agg = self._build_usage_agg_subquery(allowed_ds_ids, since_ts)

        # Steps that are not successful or had failure usage
        step_stmt = (
            select(
                Step.id,
                Step.data_model,
                Step.code,
                Step.status,
                Step.status_reason,
                usage_agg.c.last_used_at,
                usage_agg.c.had_failure_usage,
                usage_agg.c.succ,
                usage_agg.c.fail,
                usage_agg.c.attempts,
            )
            .join(usage_agg, usage_agg.c.step_id == Step.id)
            .where(
                (func.lower(Step.status) != "success") | (usage_agg.c.had_failure_usage == 1)
            )
        )
        step_rows = (await self.db.execute(step_stmt)).all()
        if not step_rows:
            return []

        # Negative feedback aggregated per step
        fb_map = await self._load_feedback_map(allowed_ds_ids, since_ts)

        tmp_holder: List[Tuple[str, Dict]] = []
        for step_id, step_dm, step_code, status, status_reason, last_used_at, had_failure_usage, succ, fail, attempts in step_rows:
            sid = str(step_id)
            step_cols = self._extract_generated_columns(step_dm or {})
            col_sim = self._jaccard_similarity(target_cols, step_cols)

            error_text = (status_reason or "").strip()

            code_str = self._trim_code(step_code, 1000)

            # Recency
            if last_used_at is None:
                recency = 0.0
                last_used_str = ""
            else:
                last_used_aware = last_used_at if last_used_at.tzinfo else last_used_at.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now_utc - last_used_aware).total_seconds() / 86400.0)
                recency = pow(2.718281828, -age_days / 14.0)
                last_used_str = last_used_aware.isoformat()

            pos, neg = fb_map.get(sid, (0, 0))
            neg_fb = float(neg)
            pos_fb = float(pos)

            attempts_n = float(attempts or 0)
            failure_rate = float(fail or 0) / attempts_n if attempts_n > 0 else 0.0
            tmp_holder.append(
                (
                    sid,
                    {
                        "col_sim": col_sim,
                        "error_message": error_text,
                        "error_summary": self._summarize_error(error_text),
                        "recency": recency,
                        "last_used_at": last_used_str,
                        "matched_columns": sorted(list(target_cols.intersection(step_cols))),
                        "code_excerpt": code_str,
                        "neg_feedback": neg_fb,
                        "pos_feedback": pos_fb,
                        "usage": {"success": int(succ or 0), "failure": int(fail or 0), "attempts": int(attempts or 0), "failure_rate": round(failure_rate, 4)},
                        "status_reason": status_reason or "",
                    },
                )
            )

        ranked: List[Tuple[float, Dict]] = []
        for sid, data in tmp_holder:
            # Failed ranking: prioritize similarity, failure rate proxy (via neg feedback and usage), and recency
            failure_component = 0.5 * data["neg_feedback"] + 0.5 * data["usage"]["failure_rate"]
            feedback_balance_penalty = max(0.0, data.get("pos_feedback", 0.0) - data.get("neg_feedback", 0.0))
            score = 0.60 * data["col_sim"] + 0.20 * data["recency"] + 0.20 * failure_component - 0.05 * feedback_balance_penalty
            ranked.append(
                (
                    score,
                    {
                        "step_id": sid,
                        "score": round(score, 6),
                        "column_similarity": round(data["col_sim"], 6),
                        "error_summary": data["error_summary"],
                        "error_message": data["error_message"],
                        "status_reason": data["status_reason"],
                        "last_used_at": data["last_used_at"],
                        "matched_columns": data["matched_columns"],
                        "code_excerpt": data["code_excerpt"],
                        "feedback": {"positive": int(data.get("pos_feedback", 0)), "negative": int(data["neg_feedback"])},
                        "usage": data["usage"],
                    },
                )
            )

        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in (ranked[:top_k] if top_k and top_k > 0 else ranked)]

    async def get_top_successful_snippets_for_tables(
        self,
        tables_by_source: list[Dict],
        *,
        top_k: int = 2,
        time_window_days: Optional[int] = None,
    ) -> List[Dict]:
        """Return top successful code snippets filtered by targeted tables (and optional ds ids).

        Uses TableUsageEvent to filter steps that successfully used any of the requested tables.
        Ranks primarily by success rate, recency, and positive feedback.
        """
        allowed_ds_ids, since_ts, now_utc = await self._get_access_and_time(time_window_days)
        if not allowed_ds_ids:
            return []
        # Normalize targets: list of (ds_id or None, table_name_lower)
        targets: list[tuple[Optional[str], str]] = []
        for group in (tables_by_source or []):
            if not isinstance(group, dict):
                continue
            ds_id = group.get("data_source_id")
            tbls = group.get("tables") or []
            for t in tbls:
                if isinstance(t, str) and t.strip():
                    targets.append((str(ds_id) if ds_id else None, t.strip().lower()))
        if not targets:
            return []

        # Build filters for TableUsageEvent
        usage_filters = [
            TableUsageEvent.org_id == str(self.organization.id),
            TableUsageEvent.data_source_id.in_(allowed_ds_ids),
        ]
        if since_ts is not None:
            usage_filters.append(TableUsageEvent.used_at >= since_ts)
        # Target matching: (optional ds match) AND table_fqn equals/lower like %.table
        from sqlalchemy import or_, and_
        match_clauses = []
        for ds_id, tname in targets:
            tfqn = func.lower(TableUsageEvent.table_fqn)
            name_eq = (tfqn == tname)
            # SQLite does not support CONCAT; build the pattern in Python
            name_like = tfqn.like(f'%.{tname}')
            name_clause = or_(name_eq, name_like)
            if ds_id:
                match_clauses.append(and_(TableUsageEvent.data_source_id == ds_id, name_clause))
            else:
                match_clauses.append(name_clause)
        if not match_clauses:
            return []

        # Aggregate usage restricted to target tables
        usage_subq = (
            select(
                TableUsageEvent.step_id.label("step_id"),
                func.max(TableUsageEvent.used_at).label("last_used_at"),
                func.sum(case((TableUsageEvent.success == True, 1), else_=0)).label("succ"),
                func.sum(case((TableUsageEvent.success == False, 1), else_=0)).label("fail"),
                func.count().label("attempts"),
            )
            .where(*usage_filters)
            .where(or_(*match_clauses))
            .group_by(TableUsageEvent.step_id)
            .subquery()
        )
        # Candidate successful steps joined with filtered usage
        step_rows = (
            await self.db.execute(
                select(
                    Step.id,
                    Step.code,
                    usage_subq.c.last_used_at,
                    usage_subq.c.succ,
                    usage_subq.c.fail,
                    usage_subq.c.attempts,
                )
                .join(usage_subq, usage_subq.c.step_id == Step.id)
                .where(func.lower(Step.status) == "success")
            )
        ).all()
        if not step_rows:
            return []

        # Feedback map for ranking
        fb_map = await self._load_feedback_map(allowed_ds_ids, since_ts)

        ranked: List[Tuple[float, Dict]] = []
        for step_id, step_code, last_used_at, succ, fail, attempts in step_rows:
            sid = str(step_id)
            pos, neg = fb_map.get(sid, (0, 0))
            attempts_n = float(attempts or 0)
            success_rate = float(succ or 0) / attempts_n if attempts_n > 0 else 0.0
            recency, last_used_str = self._recency(now_utc, last_used_at)
            feedback_score = float(pos - neg)

            # Composite score tuned for table targeting (no column similarity here)
            score = 0.55 * success_rate + 0.35 * recency + 0.10 * feedback_score

            code_str = self._trim_code(step_code, 3000)
            ranked.append((
                score,
                {
                    "step_id": sid,
                    "score": round(score, 6),
                    "success_rate": round(success_rate, 4),
                    "feedback": {"positive": int(pos or 0), "negative": int(neg or 0)},
                    "last_used_at": last_used_str,
                    "code": code_str,
                },
            ))

        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in (ranked[:top_k] if top_k and top_k > 0 else ranked)]


    def _extract_generated_columns(self, data_model: Dict) -> Set[str]:
        try:
            cols = data_model.get("columns", [])
            names = []
            for c in cols:
                name = c.get("generated_column_name") or c.get("name") or ""
                name = name.strip().lower()
                if name:
                    names.append(name)
            return set(names)
        except Exception:
            return set()

    def _jaccard_similarity(self, a: Set[str], b: Set[str]) -> float:
        if not a and not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        if union == 0:
            return 0.0
        return float(inter) / float(union)

    async def _get_access_and_time(self, time_window_days: Optional[int]) -> Tuple[Set[str], Optional[datetime], datetime]:
        # Lazy import to avoid circular dependency
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        allowed_ds = await ds_service.get_active_data_sources(
            db=self.db,
            organization=self.organization,
            current_user=self.current_user,
        )
        allowed_ds_ids: Set[str] = set(str(ds.id) for ds in allowed_ds)
        now_utc = datetime.now(timezone.utc)
        since_ts = now_utc - timedelta(days=time_window_days) if time_window_days and time_window_days > 0 else None
        return allowed_ds_ids, since_ts, now_utc

    def _build_usage_agg_subquery(self, allowed_ds_ids: Set[str], since_ts: Optional[datetime]):
        usage_filters = [
            TableUsageEvent.org_id == str(self.organization.id),
            TableUsageEvent.data_source_id.in_(allowed_ds_ids),
        ]
        if since_ts is not None:
            usage_filters.append(TableUsageEvent.used_at >= since_ts)
        return (
            select(
                TableUsageEvent.step_id.label("step_id"),
                func.max(TableUsageEvent.used_at).label("last_used_at"),
                func.sum(case((TableUsageEvent.success == True, 1), else_=0)).label("succ"),
                func.sum(case((TableUsageEvent.success == False, 1), else_=0)).label("fail"),
                func.count().label("attempts"),
                func.max(case((TableUsageEvent.success == False, 1), else_=0)).label("had_failure_usage"),
            )
            .where(*usage_filters)
            .group_by(TableUsageEvent.step_id)
            .subquery()
        )

    async def _load_feedback_map(self, allowed_ds_ids: Set[str], since_ts: Optional[datetime]) -> Dict[str, Tuple[int, int]]:
        fb_filters = [
            TableFeedbackEvent.org_id == str(self.organization.id),
            TableFeedbackEvent.data_source_id.in_(allowed_ds_ids),
        ]
        if since_ts is not None:
            fb_filters.append(TableFeedbackEvent.created_at_event >= since_ts)
        fb_rows = (
            await self.db.execute(
                select(
                    TableFeedbackEvent.step_id,
                    func.sum(case((TableFeedbackEvent.feedback_type == "positive", 1), else_=0)).label("pos"),
                    func.sum(case((TableFeedbackEvent.feedback_type == "negative", 1), else_=0)).label("neg"),
                )
                .where(*fb_filters)
                .group_by(TableFeedbackEvent.step_id)
            )
        ).all()
        return {str(step_id): (pos or 0, neg or 0) for step_id, pos, neg in fb_rows}

    def _recency(self, now_utc: datetime, last_used_at: Optional[datetime]) -> Tuple[float, str]:
        if last_used_at is None:
            return 0.0, ""
        last_used_aware = last_used_at if last_used_at.tzinfo else last_used_at.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now_utc - last_used_aware).total_seconds() / 86400.0)
        return pow(2.718281828, -age_days / 14.0), last_used_aware.isoformat()

    def _trim_code(self, code: Optional[str], max_len: int) -> str:
        code_str = (code or "").strip()
        return code_str if len(code_str) <= max_len else code_str[:max_len] + "\n# ... trimmed ..."

    def _summarize_error(self, text: str) -> str:
        if not text:
            return ""
        one_line = re.sub(r"\s+", " ", text).strip()
        return one_line[:180]




