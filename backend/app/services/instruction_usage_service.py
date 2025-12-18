from typing import Optional, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.instruction_usage_event import InstructionUsageEvent
from app.models.instruction_feedback_event import InstructionFeedbackEvent
from app.models.instruction_stats import InstructionStats
from app.schemas.instruction_usage_schema import (
    InstructionUsageEventCreate,
    InstructionUsageEventSchema,
    InstructionFeedbackEventCreate,
    InstructionFeedbackEventSchema,
    InstructionStatsUpsert,
    InstructionStatsSchema,
)


class InstructionUsageService:
    def __init__(self, role_weights: Optional[dict[str, float]] = None):
        # Default weights; can be overridden by org settings at call sites
        self.role_weights = role_weights or {
            "admin": 1.5,
            "analyst": 1.2,
            "viewer": 0.8,
            "trusted": 1.5,
        }

    async def record_usage_event(
        self, db: AsyncSession, payload: InstructionUsageEventCreate
    ) -> Optional[InstructionUsageEventSchema]:
        """Record a single instruction usage event."""
        role_weight = payload.role_weight
        if role_weight is None and payload.user_role:
            role_weight = self.role_weights.get(payload.user_role.lower(), 1.0)

        event = InstructionUsageEvent(
            org_id=payload.org_id,
            report_id=payload.report_id,
            instruction_id=payload.instruction_id,
            user_id=payload.user_id,
            load_mode=payload.load_mode,
            load_reason=payload.load_reason,
            search_score=payload.search_score,
            search_query_keywords=payload.search_query_keywords,
            source_type=payload.source_type,
            category=payload.category,
            title=payload.title,
            user_role=payload.user_role,
            role_weight=role_weight,
        )

        db.add(event)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            return None

        # Determine load mode deltas
        always_delta = 1 if payload.load_mode == "always" else 0
        intelligent_delta = 1 if payload.load_mode == "intelligent" else 0
        mentioned_delta = 1 if payload.load_reason == "mentioned" else 0

        # Upsert aggregate at org-level (report_id None)
        await self._upsert_stats(
            db=db,
            up=InstructionStatsUpsert(
                org_id=payload.org_id,
                report_id=None,
                instruction_id=payload.instruction_id,
                usage_count_delta=1,
                always_count_delta=always_delta,
                intelligent_count_delta=intelligent_delta,
                mentioned_count_delta=mentioned_delta,
                weighted_usage_delta=role_weight or 1.0,
                unique_user_delta=1 if payload.user_id else 0,
                last_used_at=datetime.utcnow(),
            ),
        )

        await db.refresh(event)
        return InstructionUsageEventSchema.model_validate(event)

    async def record_batch_usage(
        self,
        db: AsyncSession,
        org_id: str,
        report_id: Optional[str],
        user_id: Optional[str],
        items: List[dict],
        user_role: Optional[str] = None,
    ) -> List[InstructionUsageEventSchema]:
        """
        Record usage for multiple instructions at once.

        Args:
            items: List of dicts with keys: id, load_mode, load_reason, source_type,
                   category, title, search_score (optional)
        """
        results = []
        for item in items:
            load_reason = item.get("load_reason")
            search_score = item.get("search_score")
            
            # Extract search score from load_reason if present (e.g., "search_match:0.85")
            if search_score is None and load_reason and load_reason.startswith("search_match:"):
                try:
                    search_score = float(load_reason.split(":")[1])
                except (ValueError, IndexError):
                    pass
            
            payload = InstructionUsageEventCreate(
                org_id=org_id,
                report_id=report_id,
                instruction_id=item.get("id"),
                user_id=user_id,
                load_mode=item.get("load_mode", "always"),
                load_reason=load_reason,
                search_score=search_score,
                search_query_keywords=item.get("search_query_keywords"),
                source_type=item.get("source_type"),
                category=item.get("category"),
                title=item.get("title"),
                user_role=user_role,
            )
            result = await self.record_usage_event(db, payload)
            if result:
                results.append(result)
        return results

    async def record_feedback_event(
        self,
        db: AsyncSession,
        payload: InstructionFeedbackEventCreate,
        *,
        user_role: Optional[str] = None,
        role_weight: Optional[float] = None,
    ) -> Optional[InstructionFeedbackEventSchema]:
        """Record feedback (thumbs up/down) for an instruction."""
        # Determine weight if not provided
        w = role_weight
        if w is None and user_role:
            w = self.role_weights.get(user_role.lower(), 1.0)

        event = InstructionFeedbackEvent(
            org_id=payload.org_id,
            report_id=payload.report_id,
            instruction_id=payload.instruction_id,
            completion_feedback_id=payload.completion_feedback_id,
            feedback_type=payload.feedback_type,
        )
        db.add(event)
        await db.commit()
        await db.refresh(event)

        # Update stats
        pos_delta = 1 if payload.feedback_type == "positive" else 0
        neg_delta = 1 if payload.feedback_type == "negative" else 0
        weighted_pos = (w or 1.0) if pos_delta else 0.0
        weighted_neg = (w or 1.0) if neg_delta else 0.0

        await self._upsert_stats(
            db=db,
            up=InstructionStatsUpsert(
                org_id=payload.org_id,
                report_id=None,
                instruction_id=payload.instruction_id,
                pos_feedback_delta=pos_delta,
                neg_feedback_delta=neg_delta,
                weighted_pos_delta=weighted_pos,
                weighted_neg_delta=weighted_neg,
                last_feedback_at=datetime.utcnow(),
            ),
        )

        return InstructionFeedbackEventSchema.model_validate(event)

    async def _upsert_stats(
        self, db: AsyncSession, up: InstructionStatsUpsert
    ) -> InstructionStatsSchema:
        """Upsert instruction stats row."""
        stmt = select(InstructionStats).where(
            InstructionStats.org_id == up.org_id,
            InstructionStats.report_id == up.report_id,
            InstructionStats.instruction_id == up.instruction_id,
        )
        res = await db.execute(stmt)
        row: InstructionStats = res.scalar_one_or_none()

        if row is None:
            row = InstructionStats(
                org_id=up.org_id,
                report_id=up.report_id,
                instruction_id=up.instruction_id,
                usage_count=max(0, up.usage_count_delta),
                always_count=max(0, up.always_count_delta),
                intelligent_count=max(0, up.intelligent_count_delta),
                mentioned_count=max(0, up.mentioned_count_delta),
                weighted_usage_count=max(0.0, up.weighted_usage_delta),
                pos_feedback_count=max(0, up.pos_feedback_delta),
                neg_feedback_count=max(0, up.neg_feedback_delta),
                weighted_pos_feedback=max(0.0, up.weighted_pos_delta),
                weighted_neg_feedback=max(0.0, up.weighted_neg_delta),
                unique_users=max(0, up.unique_user_delta),
                last_used_at=up.last_used_at,
                last_feedback_at=up.last_feedback_at,
                updated_at_stats=datetime.utcnow(),
            )
            db.add(row)
        else:
            # Incremental updates
            row.usage_count = row.usage_count + up.usage_count_delta
            row.always_count = row.always_count + up.always_count_delta
            row.intelligent_count = row.intelligent_count + up.intelligent_count_delta
            row.mentioned_count = row.mentioned_count + up.mentioned_count_delta
            row.weighted_usage_count = row.weighted_usage_count + up.weighted_usage_delta
            row.pos_feedback_count = row.pos_feedback_count + up.pos_feedback_delta
            row.neg_feedback_count = row.neg_feedback_count + up.neg_feedback_delta
            row.weighted_pos_feedback = row.weighted_pos_feedback + up.weighted_pos_delta
            row.weighted_neg_feedback = row.weighted_neg_feedback + up.weighted_neg_delta
            row.unique_users = row.unique_users + up.unique_user_delta
            row.last_used_at = up.last_used_at or row.last_used_at
            row.last_feedback_at = up.last_feedback_at or row.last_feedback_at
            row.updated_at_stats = datetime.utcnow()

        await db.commit()
        await db.refresh(row)
        return InstructionStatsSchema.model_validate(row)
