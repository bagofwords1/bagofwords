from typing import Optional, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.table_usage_event import TableUsageEvent
from app.models.table_feedback_event import TableFeedbackEvent
from app.models.table_stats import TableStats
from app.schemas.table_usage_schema import (
    TableUsageEventCreate,
    TableUsageEventSchema,
    TableFeedbackEventCreate,
    TableFeedbackEventSchema,
    TableStatsUpsert,
    TableStatsSchema,
)


class TableUsageService:
    def __init__(self, role_weights: Optional[dict[str, float]] = None):
        # Default weights; can be overridden by org settings at call sites
        self.role_weights = role_weights or {
            "admin": 1.5,
            "analyst": 1.2,
            "viewer": 0.8,
            "trusted": 1.5,
        }

    async def record_usage_event(self, db: AsyncSession, payload: TableUsageEventCreate) -> TableUsageEventSchema:
        role_weight = payload.role_weight
        if role_weight is None and payload.user_role:
            role_weight = self.role_weights.get(payload.user_role.lower(), 1.0)

        event = TableUsageEvent(
            org_id=payload.org_id,
            report_id=payload.report_id,
            step_id=payload.step_id,
            user_id=payload.user_id,
            table_fqn=payload.table_fqn,
            datasource_table_id=payload.datasource_table_id,
            source_type=payload.source_type,
            columns=payload.columns,
            success=payload.success,
            user_role=payload.user_role,
            role_weight=role_weight,
        )

        db.add(event)
        try:
            await db.commit()
        except Exception:
            # Unique constraint might trip if called twice; ignore duplicates
            await db.rollback()

        # Upsert aggregates for report-level and org-level (report_id None)
        # Always increment total attempts
        await self._upsert_stats(
            db=db,
            up=TableStatsUpsert(
                org_id=payload.org_id,
                report_id=payload.report_id,
                table_fqn=payload.table_fqn,
                datasource_table_id=payload.datasource_table_id,
                usage_count_delta=1,
            ),
        )
        if payload.report_id is not None:
            await self._upsert_stats(
                db=db,
                up=TableStatsUpsert(
                    org_id=payload.org_id,
                    report_id=None,
                    table_fqn=payload.table_fqn,
                    datasource_table_id=payload.datasource_table_id,
                    usage_count_delta=1,
                ),
            )

        if payload.success:
            trusted_flag = (payload.user_role and payload.user_role.lower() in ("admin", "trusted"))
            await self._upsert_stats(
                db=db,
                up=TableStatsUpsert(
                    org_id=payload.org_id,
                    report_id=payload.report_id,
                    table_fqn=payload.table_fqn,
                    datasource_table_id=payload.datasource_table_id,
                    success_count_delta=1,
                    weighted_usage_delta=role_weight or 1.0,
                    unique_user_delta=1 if payload.user_id else 0,
                    admin_usage_delta=1 if trusted_flag else 0,
                    last_used_at=datetime.utcnow(),
                ),
            )

            if payload.report_id is not None:
                await self._upsert_stats(
                    db=db,
                    up=TableStatsUpsert(
                        org_id=payload.org_id,
                        report_id=None,
                        table_fqn=payload.table_fqn,
                        datasource_table_id=payload.datasource_table_id,
                        success_count_delta=1,
                        weighted_usage_delta=role_weight or 1.0,
                        unique_user_delta=1 if payload.user_id else 0,
                        admin_usage_delta=1 if trusted_flag else 0,
                        last_used_at=datetime.utcnow(),
                    ),
                )
        else:
            # Record failure attempts to stats (do not increment usage)
            await self._upsert_stats(
                db=db,
                up=TableStatsUpsert(
                    org_id=payload.org_id,
                    report_id=payload.report_id,
                    table_fqn=payload.table_fqn,
                    datasource_table_id=payload.datasource_table_id,
                    failure_delta=1,
                ),
            )
            if payload.report_id is not None:
                await self._upsert_stats(
                    db=db,
                    up=TableStatsUpsert(
                        org_id=payload.org_id,
                        report_id=None,
                        table_fqn=payload.table_fqn,
                        datasource_table_id=payload.datasource_table_id,
                        failure_delta=1,
                    ),
                )

        await db.refresh(event)
        return TableUsageEventSchema.from_orm(event)

    async def record_feedback_event(self, db: AsyncSession, payload: TableFeedbackEventCreate, *, user_role: Optional[str] = None, role_weight: Optional[float] = None) -> TableFeedbackEventSchema:
        # Determine weight if not provided
        w = role_weight
        if w is None and user_role:
            w = self.role_weights.get(user_role.lower(), 1.0)

        event = TableFeedbackEvent(
            org_id=payload.org_id,
            report_id=payload.report_id,
            step_id=payload.step_id,
            completion_feedback_id=payload.completion_feedback_id,
            table_fqn=payload.table_fqn,
            datasource_table_id=payload.datasource_table_id,
            feedback_type=payload.feedback_type,
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)

        # Update stats for both scopes
        pos_delta = 1 if payload.feedback_type == "positive" else 0
        neg_delta = 1 if payload.feedback_type == "negative" else 0
        weighted_pos = (w or 1.0) if pos_delta else 0.0
        weighted_neg = (w or 1.0) if neg_delta else 0.0

        await self._upsert_stats(
            db=db,
            up=TableStatsUpsert(
                org_id=payload.org_id,
                report_id=payload.report_id,
                table_fqn=payload.table_fqn,
                datasource_table_id=payload.datasource_table_id,
                pos_feedback_delta=pos_delta,
                neg_feedback_delta=neg_delta,
                weighted_pos_delta=weighted_pos,
                weighted_neg_delta=weighted_neg,
                last_feedback_at=datetime.utcnow(),
            ),
        )
        if payload.report_id is not None:
            await self._upsert_stats(
                db=db,
                up=TableStatsUpsert(
                    org_id=payload.org_id,
                    report_id=None,
                    table_fqn=payload.table_fqn,
                    datasource_table_id=payload.datasource_table_id,
                    pos_feedback_delta=pos_delta,
                    neg_feedback_delta=neg_delta,
                    weighted_pos_delta=weighted_pos,
                    weighted_neg_delta=weighted_neg,
                    last_feedback_at=datetime.utcnow(),
                ),
            )

        return TableFeedbackEventSchema.from_orm(event)

    async def _upsert_stats(self, db: AsyncSession, up: TableStatsUpsert) -> TableStatsSchema:
        # Try select first
        stmt = select(TableStats).where(
            TableStats.org_id == up.org_id,
            TableStats.report_id == up.report_id,
            TableStats.table_fqn == up.table_fqn,
        )
        res = await db.execute(stmt)
        row: TableStats = res.scalar_one_or_none()

        if row is None:
            row = TableStats(
                org_id=up.org_id,
                report_id=up.report_id,
                table_fqn=up.table_fqn,
                datasource_table_id=up.datasource_table_id,
                usage_count=max(0, up.usage_count_delta),
                success_count=max(0, getattr(up, 'success_count_delta', 0)),
                weighted_usage_count=max(0.0, up.weighted_usage_delta),
                pos_feedback_count=max(0, up.pos_feedback_delta),
                neg_feedback_count=max(0, up.neg_feedback_delta),
                weighted_pos_feedback=max(0.0, up.weighted_pos_delta),
                weighted_neg_feedback=max(0.0, up.weighted_neg_delta),
                unique_users=max(0, up.unique_user_delta),
                trusted_usage_count=max(0, up.admin_usage_delta),
                failure_count=max(0, up.failure_delta),
                last_used_at=up.last_used_at,
                last_feedback_at=up.last_feedback_at,
                updated_at_stats=datetime.utcnow(),
            )
            db.add(row)
        else:
            # Incremental updates
            row.datasource_table_id = row.datasource_table_id or up.datasource_table_id
            row.usage_count = row.usage_count + up.usage_count_delta
            row.success_count = row.success_count + getattr(up, 'success_count_delta', 0)
            row.weighted_usage_count = row.weighted_usage_count + up.weighted_usage_delta
            row.pos_feedback_count = row.pos_feedback_count + up.pos_feedback_delta
            row.neg_feedback_count = row.neg_feedback_count + up.neg_feedback_delta
            row.weighted_pos_feedback = row.weighted_pos_feedback + up.weighted_pos_delta
            row.weighted_neg_feedback = row.weighted_neg_feedback + up.weighted_neg_delta
            row.unique_users = row.unique_users + up.unique_user_delta
            row.trusted_usage_count = row.trusted_usage_count + up.admin_usage_delta
            row.failure_count = row.failure_count + up.failure_delta
            row.last_used_at = up.last_used_at or row.last_used_at
            row.last_feedback_at = up.last_feedback_at or row.last_feedback_at
            row.updated_at_stats = datetime.utcnow()

        await db.commit()
        await db.refresh(row)
        return TableStatsSchema.from_orm(row)

