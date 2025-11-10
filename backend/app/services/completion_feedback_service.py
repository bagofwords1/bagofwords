from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, update
from fastapi import HTTPException

from app.models.completion_feedback import CompletionFeedback
from app.models.completion import Completion
from app.models.user import User
from app.models.organization import Organization
from app.schemas.completion_feedback_schema import (
    CompletionFeedbackCreate, 
    CompletionFeedbackUpdate, 
    CompletionFeedbackSchema,
    CompletionFeedbackSummary
)
from app.services.table_usage_service import TableUsageService
from app.schemas.table_usage_schema import TableFeedbackEventCreate
from app.models.completion_block import CompletionBlock
from app.models.tool_execution import ToolExecution
from app.models.step import Step
from app.models.table_usage_event import TableUsageEvent
from app.core.telemetry import telemetry


class CompletionFeedbackService:
    
    def __init__(self):
        self.table_usage_service = TableUsageService()

    async def _emit_table_feedback(
        self,
        db: AsyncSession,
        organization: Organization,
        completion: Completion,
        feedback: CompletionFeedback,
        user: Optional[User]
    ) -> None:
        try:
            target_steps: list[Step] = []

            # Support block-scoped feedback if the column exists (forward-compatible)
            block_id = getattr(feedback, 'completion_block_id', None)
            if block_id:
                block = await db.get(CompletionBlock, block_id)
                if block and block.tool_execution_id:
                    te = await db.get(ToolExecution, block.tool_execution_id)
                    if te and te.created_step_id:
                        step = await db.get(Step, te.created_step_id)
                        if step:
                            target_steps.append(step)
            else:
                # Aggregate all steps created by tool executions within this completion's blocks
                te_ids_stmt = select(CompletionBlock.tool_execution_id).where(
                    CompletionBlock.completion_id == completion.id,
                    CompletionBlock.tool_execution_id.isnot(None)
                )
                te_ids_result = await db.execute(te_ids_stmt)
                te_ids = [row[0] for row in te_ids_result.fetchall() if row[0]]

                if te_ids:
                    step_ids_stmt = select(ToolExecution.created_step_id).where(
                        ToolExecution.id.in_(te_ids),
                        ToolExecution.created_step_id.isnot(None)
                    )
                    step_ids_result = await db.execute(step_ids_stmt)
                    step_ids = [row[0] for row in step_ids_result.fetchall() if row[0]]

                    if step_ids:
                        # Deduplicate while preserving order
                        seen = set()
                        uniq_step_ids = []
                        for sid in step_ids:
                            if sid not in seen:
                                seen.add(sid)
                                uniq_step_ids.append(sid)

                        steps_stmt = select(Step).where(Step.id.in_(uniq_step_ids))
                        steps_result = await db.execute(steps_stmt)
                        target_steps = steps_result.scalars().all()

            # Fallback to the completion's step if no block-derived steps found
            if not target_steps and completion.step:
                target_steps = [completion.step]

            if not target_steps:
                return

            direction = 'positive' if feedback.direction == 1 else 'negative'

            for step in target_steps:
                if not step:
                    continue
                
                # Attribute feedback exclusively from recorded table usage for this step (ground truth)
                try:
                    usage_stmt = select(TableUsageEvent).where(
                        TableUsageEvent.step_id == str(step.id),
                        TableUsageEvent.success == True,
                    )
                    usage_res = await db.execute(usage_stmt)
                    usage_rows = usage_res.scalars().all()
                except Exception:
                    usage_rows = []

                if not usage_rows:
                    continue

                # Deduplicate by (data_source_id, table_fqn)
                seen_pairs: set[tuple[str, str]] = set()
                for u in usage_rows:
                    ds_id = getattr(u, "data_source_id", None)
                    table_fqn = (getattr(u, "table_fqn", None) or "").lower()
                    if not ds_id or not table_fqn:
                        continue
                    pair = (ds_id, table_fqn)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)

                    payload = TableFeedbackEventCreate(
                        org_id=str(organization.id),
                        report_id=str(completion.report_id) if completion.report_id else None,
                        data_source_id=ds_id,
                        step_id=str(step.id),
                        completion_feedback_id=str(feedback.id),
                        table_fqn=table_fqn,
                        datasource_table_id=getattr(u, "datasource_table_id", None),
                        feedback_type=direction,
                    )
                    await self.table_usage_service.record_feedback_event(
                        db=db,
                        payload=payload,
                        user_role=getattr(user, 'role', None)
                    )
        except Exception:
            # Never block on attribution failures
            return

    async def create_or_update_feedback(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        feedback_data: CompletionFeedbackCreate, 
        user: User, 
        organization: Organization
    ) -> CompletionFeedbackSchema:
        """Create or update feedback for a completion. If user already has feedback, update it."""
        
        # Verify completion exists and belongs to organization
        completion_stmt = select(Completion).where(
            Completion.id == completion_id,
            Completion.report.has(organization_id=organization.id)
        )
        completion_result = await db.execute(completion_stmt)
        completion = completion_result.scalar_one_or_none()
        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        user_id = user.id if user else None
        
        # Check if user already has feedback for this completion
        existing_feedback_stmt = select(CompletionFeedback).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.user_id == user_id,
            CompletionFeedback.organization_id == organization.id
        )
        existing_result = await db.execute(existing_feedback_stmt)
        existing_feedback = existing_result.scalar_one_or_none()
        
        if existing_feedback:
            # Update existing feedback
            existing_feedback.direction = feedback_data.direction
            existing_feedback.message = feedback_data.message
            await db.commit()
            await db.refresh(existing_feedback)
            # Telemetry: feedback updated
            try:
                await telemetry.capture(
                    "completion_feedback_updated",
                    {
                        "completion_id": str(completion_id),
                        "direction": int(existing_feedback.direction),
                        "has_message": bool(existing_feedback.message),
                    },
                    user_id=user.id if user else None,
                    org_id=organization.id,
                )
            except Exception:
                pass
            # Emit table feedback events reflecting the updated direction
            try:
                await self._emit_table_feedback(db, organization, completion, existing_feedback, user)
            except Exception:
                pass
            return CompletionFeedbackSchema.from_orm(existing_feedback)
        else:
            # Create new feedback
            feedback = CompletionFeedback(
                user_id=user_id,
                completion_id=completion_id,
                organization_id=organization.id,
                direction=feedback_data.direction,
                message=feedback_data.message
            )
            
            db.add(feedback)
            await db.commit()
            await db.refresh(feedback)

            # Telemetry: feedback created
            try:
                await telemetry.capture(
                    "completion_feedback_created",
                    {
                        "completion_id": str(completion_id),
                        "direction": int(feedback.direction),
                        "has_message": bool(feedback.message),
                    },
                    user_id=user.id if user else None,
                    org_id=organization.id,
                )
            except Exception:
                pass

            # Emit table feedback events attributed to the completion's step lineage if available
            await self._emit_table_feedback(db, organization, completion, feedback, user)

            return CompletionFeedbackSchema.from_orm(feedback)
    
    async def get_feedback_summary(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        user: Optional[User], 
        organization: Organization
    ) -> CompletionFeedbackSummary:
        """Get feedback summary for a completion including user's feedback if any."""
        
        # Verify completion exists and belongs to organization
        completion_stmt = select(Completion).where(
            Completion.id == completion_id,
            Completion.report.has(organization_id=organization.id)
        )
        completion_result = await db.execute(completion_stmt)
        completion = completion_result.scalar_one_or_none()
        
        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        # Get aggregated feedback stats
        stats_stmt = select(
            func.count(CompletionFeedback.id).label('total_feedbacks'),
            func.count().filter(CompletionFeedback.direction == 1).label('total_upvotes'),
            func.count().filter(CompletionFeedback.direction == -1).label('total_downvotes'),
            func.sum(CompletionFeedback.direction).label('net_score')
        ).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.organization_id == organization.id
        )
        
        stats_result = await db.execute(stats_stmt)
        stats = stats_result.first()
        
        # Get user's feedback if user is provided
        user_feedback = None
        if user:
            user_feedback_stmt = select(CompletionFeedback).where(
                CompletionFeedback.completion_id == completion_id,
                CompletionFeedback.user_id == user.id,
                CompletionFeedback.organization_id == organization.id
            )
            user_feedback_result = await db.execute(user_feedback_stmt)
            user_feedback_obj = user_feedback_result.scalar_one_or_none()
            if user_feedback_obj:
                user_feedback = CompletionFeedbackSchema.from_orm(user_feedback_obj)
        
        return CompletionFeedbackSummary(
            completion_id=completion_id,
            total_upvotes=stats.total_upvotes or 0,
            total_downvotes=stats.total_downvotes or 0,
            net_score=stats.net_score or 0,
            total_feedbacks=stats.total_feedbacks or 0,
            user_feedback=user_feedback
        )
    
    async def delete_feedback(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        user: User, 
        organization: Organization
    ) -> bool:
        """Delete user's feedback for a completion."""
        
        feedback_stmt = select(CompletionFeedback).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.user_id == user.id,
            CompletionFeedback.organization_id == organization.id
        )
        feedback_result = await db.execute(feedback_stmt)
        feedback = feedback_result.scalar_one_or_none()
        
        if not feedback:
            raise HTTPException(status_code=404, detail="Feedback not found")
        
        await db.delete(feedback)
        await db.commit()
        return True
    
    async def get_completion_feedbacks(
        self, 
        db: AsyncSession, 
        completion_id: str, 
        organization: Organization
    ) -> List[CompletionFeedbackSchema]:
        """Get all feedbacks for a completion."""
        
        # Verify completion exists and belongs to organization
        completion_stmt = select(Completion).where(
            Completion.id == completion_id,
            Completion.report.has(organization_id=organization.id)
        )
        completion_result = await db.execute(completion_stmt)
        completion = completion_result.scalar_one_or_none()
        
        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        feedbacks_stmt = select(CompletionFeedback).where(
            CompletionFeedback.completion_id == completion_id,
            CompletionFeedback.organization_id == organization.id
        )
        feedbacks_result = await db.execute(feedbacks_stmt)
        feedbacks = feedbacks_result.scalars().all()
        
        return [CompletionFeedbackSchema.from_orm(feedback) for feedback in feedbacks]