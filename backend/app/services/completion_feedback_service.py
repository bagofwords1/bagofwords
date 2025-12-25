from typing import List, Optional
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, update
from fastapi import HTTPException

from app.models.completion_feedback import CompletionFeedback
from app.models.completion import Completion
from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.schemas.completion_feedback_schema import (
    CompletionFeedbackCreate, 
    CompletionFeedbackUpdate, 
    CompletionFeedbackSchema,
    CompletionFeedbackSummary
)
from app.services.table_usage_service import TableUsageService
from app.schemas.table_usage_schema import TableFeedbackEventCreate
from app.services.instruction_usage_service import InstructionUsageService
from app.schemas.instruction_usage_schema import InstructionFeedbackEventCreate
from app.models.completion_block import CompletionBlock
from app.models.tool_execution import ToolExecution
from app.models.step import Step
from app.models.table_usage_event import TableUsageEvent
from app.models.agent_execution import AgentExecution
from app.models.context_snapshot import ContextSnapshot
from app.core.telemetry import telemetry

logger = logging.getLogger(__name__)


class CompletionFeedbackService:
    
    def __init__(self):
        self.table_usage_service = TableUsageService()
        self.instruction_usage_service = InstructionUsageService()

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

    async def _emit_instruction_feedback(
        self,
        db: AsyncSession,
        organization: Organization,
        completion: Completion,
        feedback: CompletionFeedback,
        user: Optional[User]
    ) -> None:
        """Attribute feedback to instructions that were used in the completion's context."""
        try:
            # Find AgentExecution for this completion
            ae_stmt = select(AgentExecution).where(
                AgentExecution.completion_id == str(completion.id)
            )
            ae_result = await db.execute(ae_stmt)
            agent_execution = ae_result.scalar_one_or_none()
            
            if not agent_execution:
                return
            
            # Get the initial context snapshot (contains the instructions used)
            cs_stmt = select(ContextSnapshot).where(
                ContextSnapshot.agent_execution_id == str(agent_execution.id),
                ContextSnapshot.kind == 'initial'
            )
            cs_result = await db.execute(cs_stmt)
            context_snapshot = cs_result.scalar_one_or_none()
            
            if not context_snapshot or not context_snapshot.context_view_json:
                return
            
            # Extract instructions from context_view_json
            context_json = context_snapshot.context_view_json
            instructions_data = []
            
            # Try different possible paths in the context structure
            if isinstance(context_json, dict):
                # Check static.instructions.items path
                static = context_json.get('static', {})
                if static:
                    instructions_section = static.get('instructions', {})
                    if instructions_section:
                        instructions_data = instructions_section.get('items', [])
                
                # Fallback: check instructions_usage if present
                if not instructions_data:
                    instructions_data = context_json.get('instructions_usage', [])
            
            if not instructions_data:
                return
            
            direction = 'positive' if feedback.direction == 1 else 'negative'
            
            # Deduplicate by instruction_id
            seen_ids: set[str] = set()
            for inst in instructions_data:
                if not isinstance(inst, dict):
                    continue
                    
                inst_id = inst.get('id')
                if not inst_id or inst_id in seen_ids:
                    continue
                seen_ids.add(inst_id)
                
                payload = InstructionFeedbackEventCreate(
                    org_id=str(organization.id),
                    report_id=str(completion.report_id) if completion.report_id else None,
                    instruction_id=inst_id,
                    completion_feedback_id=str(feedback.id),
                    feedback_type=direction,
                )
                await self.instruction_usage_service.record_feedback_event(
                    db=db,
                    payload=payload,
                    user_role=getattr(user, 'role', None) if user else None
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
        
        # Determine if we should signal frontend to call suggest-instructions endpoint
        should_suggest = False
        if feedback_data.direction == -1:
            try:
                from app.services.organization_settings_service import OrganizationSettingsService
                settings_service = OrganizationSettingsService()
                org_settings = await settings_service.get_settings(db, organization, user)
                config = org_settings.get_config("suggest_instructions")
                should_suggest = config is None or config.value is not False
            except Exception:
                should_suggest = True  # Default to true if we can't check settings
        
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
            # Emit table and instruction feedback events reflecting the updated direction
            try:
                await self._emit_table_feedback(db, organization, completion, existing_feedback, user)
            except Exception:
                pass
            try:
                await self._emit_instruction_feedback(db, organization, completion, existing_feedback, user)
            except Exception:
                pass
            result = CompletionFeedbackSchema.from_orm(existing_feedback)
            result.should_suggest_instructions = should_suggest
            return result
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

            # Emit table and instruction feedback events attributed to the completion's context
            await self._emit_table_feedback(db, organization, completion, feedback, user)
            try:
                await self._emit_instruction_feedback(db, organization, completion, feedback, user)
            except Exception:
                pass

            result = CompletionFeedbackSchema.from_orm(feedback)
            result.should_suggest_instructions = should_suggest
            return result
    
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

    async def generate_suggestions_from_feedback(
        self,
        db: AsyncSession,
        completion_id: str,
        user: User,
        organization: Organization
    ) -> List[dict]:
        """Generate instruction suggestions based on completion context and user feedback.
        
        This is called after negative feedback to suggest instructions that could
        help prevent similar issues in the future.
        """
        try:
            # Import here to avoid circular imports
            from app.services.organization_settings_service import OrganizationSettingsService
            from app.ai.agents.suggest_instructions import SuggestInstructions
            from app.ai.agents.suggest_instructions.trigger import TriggerCondition
            from app.ai.context import ContextHub
            from app.project_manager import ProjectManager
            
            # Get organization settings
            settings_service = OrganizationSettingsService()
            org_settings = await settings_service.get_settings(db, organization, user)
            
            # Check if suggest_instructions is enabled (gate)
            config = org_settings.get_config("suggest_instructions")
            if config and config.value is False:
                return []
            
            # Load the completion
            completion_stmt = select(Completion).where(
                Completion.id == completion_id,
                Completion.report.has(organization_id=organization.id)
            )
            completion_result = await db.execute(completion_stmt)
            completion = completion_result.scalar_one_or_none()
            if not completion:
                return []
            
            # Get the user's most recent feedback for this completion
            feedback_stmt = select(CompletionFeedback).where(
                CompletionFeedback.completion_id == completion_id,
                CompletionFeedback.user_id == user.id,
                CompletionFeedback.organization_id == organization.id
            ).order_by(CompletionFeedback.updated_at.desc())
            feedback_result = await db.execute(feedback_stmt)
            feedback = feedback_result.scalar_one_or_none()
            
            if not feedback or feedback.direction != -1:
                # Only generate suggestions for negative feedback
                return []
            
            # Find AgentExecution for this completion
            ae_stmt = select(AgentExecution).where(
                AgentExecution.completion_id == str(completion.id)
            )
            ae_result = await db.execute(ae_stmt)
            agent_execution = ae_result.scalar_one_or_none()
            
            if not agent_execution:
                logger.warning(f"No agent execution found for completion {completion_id}")
                return []
            
            # Load the report for context
            report = await db.get(Report, completion.report_id)
            if not report:
                return []
            
            # Build minimal context from the completion's context
            context_hub = ContextHub(
                db=db,
                organization=organization,
                report=report,
                data_sources=getattr(report, 'data_sources', []) or [],
                user=user,
                head_completion=completion,
                widget=None,
                organization_settings=org_settings,
                build_id=getattr(agent_execution, 'build_id', None)
            )
            
            # Prime and refresh context
            await context_hub.prime_static()
            await context_hub.refresh_warm()
            context_view = context_hub.get_view()
            
            # Create the feedback trigger condition
            feedback_condition = TriggerCondition.create_feedback_condition(
                feedback_direction=feedback.direction,
                feedback_message=feedback.message
            )
            
            # Initialize SuggestInstructions agent
            from app.services.llm_service import LLMService
            llm_service = LLMService()
            small_model = await llm_service.get_default_model(db, organization, user, is_small=True)
            suggest_agent = SuggestInstructions(model=small_model)
            
            # Generate suggestions
            suggestions = []
            project_manager = ProjectManager()
            
            async for draft in suggest_agent.stream_suggestions(
                context_view=context_view,
                context_hub=context_hub,
                conditions=[feedback_condition]
            ):
                # Create the instruction in the database
                try:
                    inst = await project_manager.create_instruction_from_draft(
                        db,
                        organization,
                        text=draft.get("text", ""),
                        category=draft.get("category", "general"),
                        agent_execution_id=str(agent_execution.id),
                        trigger_reason="feedback_triggered",
                        ai_source="feedback",
                        user_id=str(user.id) if user else None,
                        build=None  # No build for feedback-triggered suggestions
                    )
                    suggestions.append({
                        "id": str(inst.id),
                        "text": inst.text,
                        "category": inst.category,
                        "status": inst.status,
                        "private_status": inst.private_status,
                        "global_status": inst.global_status,
                        "is_seen": inst.is_seen,
                        "can_user_toggle": inst.can_user_toggle,
                    })
                except Exception as e:
                    logger.warning(f"Failed to create instruction from draft: {e}")
                    continue
            
            logger.info(f"Generated {len(suggestions)} suggestions from feedback for completion {completion_id}")
            return suggestions
            
        except Exception as e:
            logger.error(f"Error generating suggestions from feedback: {e}")
            return []