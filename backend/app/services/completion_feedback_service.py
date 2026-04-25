from typing import Any, Dict, List, Optional
import asyncio
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
from app.ee.audit.service import audit_service

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

            # Audit log
            try:
                await audit_service.log(
                    db=db,
                    organization_id=str(organization.id),
                    action="completion_feedback.updated",
                    user_id=str(user.id) if user else None,
                    resource_type="completion_feedback",
                    resource_id=str(existing_feedback.id),
                    details={"direction": existing_feedback.direction, "has_message": bool(existing_feedback.message)},
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
            # Fire-and-forget eval-draft on positive feedback. The drafter
            # opens its own DB session because the request session closes
            # before the task runs.
            self._maybe_schedule_eval_draft(
                completion_id=completion_id,
                user=user,
                organization=organization,
                direction=existing_feedback.direction,
            )

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

            # Audit log
            try:
                await audit_service.log(
                    db=db,
                    organization_id=str(organization.id),
                    action="completion_feedback.created",
                    user_id=str(user.id) if user else None,
                    resource_type="completion_feedback",
                    resource_id=str(feedback.id),
                    details={"direction": feedback.direction, "has_message": bool(feedback.message)},
                )
            except Exception:
                pass

            # Emit table and instruction feedback events attributed to the completion's context
            await self._emit_table_feedback(db, organization, completion, feedback, user)
            try:
                await self._emit_instruction_feedback(db, organization, completion, feedback, user)
            except Exception:
                pass

            # Fire-and-forget eval-draft on positive feedback. Mirrors the
            # branch above so freshly created feedback also triggers.
            self._maybe_schedule_eval_draft(
                completion_id=completion_id,
                user=user,
                organization=organization,
                direction=feedback.direction,
            )

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

        # Capture details before deletion for audit
        feedback_id = str(feedback.id)

        await db.delete(feedback)
        await db.commit()

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="completion_feedback.deleted",
                user_id=str(user.id) if user else None,
                resource_type="completion_feedback",
                resource_id=feedback_id,
                details={"completion_id": completion_id},
            )
        except Exception:
            pass

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
            suggest_agent = SuggestInstructions(model=small_model, organization_settings=org_settings)
            
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
                        title=draft.get("title"),
                        category=draft.get("category", "general"),
                        agent_execution_id=str(agent_execution.id),
                        trigger_reason="feedback_triggered",
                        ai_source="feedback",
                        user_id=str(user.id) if user else None,
                        build=None  # No build for feedback-triggered suggestions
                    )
                    suggestions.append({
                        "id": str(inst.id),
                        "title": inst.title,
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

    def _maybe_schedule_eval_draft(
        self,
        *,
        completion_id: str,
        user: Optional[User],
        organization: Organization,
        direction: int,
    ) -> None:
        """Schedule the auto-draft task on positive feedback.

        Cheap predicate up front (direction must be 1 + a user must be
        attached). All other gates run inside the task with a fresh DB
        session — failures in the task are logged and swallowed so they
        never surface to the feedback POST.
        """
        try:
            if direction != 1 or user is None:
                return
            asyncio.create_task(
                self.maybe_draft_eval_from_feedback(
                    completion_id=str(completion_id),
                    user=user,
                    organization=organization,
                )
            )
        except Exception as e:
            logger.debug(f"_maybe_schedule_eval_draft failed: {e}")

    async def maybe_draft_eval_from_feedback(
        self,
        *,
        completion_id: str,
        user: User,
        organization: Organization,
        db: Optional[AsyncSession] = None,
    ) -> Optional[dict]:
        """Auto-draft a TestCase from a positive feedback on a completion
        that successfully ran ``create_data``.

        Always opens its own DB session — the ``db`` kwarg is ignored and
        only kept for API symmetry with other service methods. This is
        because the request session that wrote the feedback row closes
        before the fire-and-forget task runs.

        Gates (all must hold):
        1. Positive feedback exists for the completion (idempotent re-check).
        2. ``auto_suggest_evals`` org setting is on.
        3. User has ``manage_evals``.
        4. The completion's AgentExecution had ≥1 successful ``create_data``.
        5. No existing non-archived TestCase already references this
           completion as ``source_completion_id`` (FK dedupe).
        6. A small-model classifier judges the candidate is NOT a duplicate
           of any existing eval scoped to the candidate's data sources.

        On pass, calls ``CreateEvalTool`` with ``mode="knowledge"`` so the
        case lands as a draft in the org's drafts suite with full
        provenance. Returns ``{"created": case_id, "name": ...}`` or
        ``None`` when any gate fails.
        """
        from app.settings.database import create_async_session_factory

        org_id = str(organization.id)
        user_id = str(user.id) if user else None
        if not user_id:
            return None

        async_session = create_async_session_factory()
        try:
            async with async_session() as session:
                return await self._draft_eval_from_feedback_inner(
                    session, completion_id, user_id, org_id,
                )
        except Exception as e:
            logger.exception(f"Error drafting eval from feedback: {e}")
            return None

    async def _draft_eval_from_feedback_inner(
        self,
        db: AsyncSession,
        completion_id: str,
        user_id: str,
        organization_id: str,
    ) -> Optional[dict]:
        from sqlalchemy import or_
        import json as _json
        from app.ai.tools.implementations.create_eval import CreateEvalTool
        from app.ai.tools.schemas.create_eval import CreateEvalInput, CreateEvalPrompt
        from app.core.permission_resolver import resolve_permissions
        from app.models.eval import (
            TEST_CASE_STATUS_ARCHIVED,
            TestCase,
            TestSuite,
        )
        from app.models.organization import Organization as _Org
        from app.models.user import User as _User
        from app.models.tool_execution import ToolExecution as _TE
        from app.models.agent_execution import AgentExecution as _AE
        from app.services.organization_settings_service import OrganizationSettingsService

        # === Reload everything in this fresh session ===
        organization = await db.get(_Org, organization_id)
        user = await db.get(_User, user_id) if user_id else None
        if not organization or not user:
            return None

        completion_stmt = select(Completion).where(
            Completion.id == completion_id,
            Completion.report.has(organization_id=organization.id),
        )
        completion = (await db.execute(completion_stmt)).scalar_one_or_none()
        if not completion:
            return None

        # Gate 1: positive feedback exists.
        fb_stmt = (
            select(CompletionFeedback)
            .where(CompletionFeedback.completion_id == completion_id)
            .where(CompletionFeedback.user_id == user_id)
            .where(CompletionFeedback.organization_id == organization_id)
            .order_by(CompletionFeedback.updated_at.desc())
            .limit(1)
        )
        feedback = (await db.execute(fb_stmt)).scalar_one_or_none()
        if not feedback or feedback.direction != 1:
            return None

        # Gate 2: org setting on.
        try:
            settings_service = OrganizationSettingsService()
            org_settings = await settings_service.get_settings(db, organization, user)
            cfg = org_settings.get_config("auto_suggest_evals")
            if cfg is not None and cfg.value is False:
                return None
        except Exception:
            return None

        # Gate 3: user has manage_evals.
        try:
            resolved = await resolve_permissions(db, user_id, organization_id)
            if not resolved.has_org_permission("manage_evals"):
                return None
        except Exception:
            return None

        # Gate 4: AgentExecution + ≥1 successful create_data.
        ae_stmt = select(_AE).where(_AE.completion_id == str(completion.id))
        agent_execution = (await db.execute(ae_stmt)).scalar_one_or_none()
        if not agent_execution:
            return None

        te_stmt = (
            select(_TE)
            .where(_TE.agent_execution_id == str(agent_execution.id))
            .where((_TE.success == True) | (_TE.status == "success"))
        )
        all_tes: List[_TE] = list((await db.execute(te_stmt)).scalars().all())
        create_data_tes = [te for te in all_tes if te.tool_name == "create_data"]
        if not create_data_tes:
            return None

        # Distinct tool names actually invoked successfully — used for the
        # ``tool.calls`` set-membership rules.
        tools_used = sorted({te.tool_name for te in all_tes if te.tool_name})

        # Deterministic data-source ids from create_data inputs.
        data_source_ids = self._extract_data_source_ids(create_data_tes)

        # Verbatim user prompt (the head completion's prompt).
        user_prompt = ""
        head_completion = None
        try:
            if completion.parent_id:
                head_completion = await db.get(Completion, str(completion.parent_id))
                if head_completion is not None:
                    pj = head_completion.prompt or {}
                    if isinstance(pj, dict):
                        user_prompt = (pj.get("content") or "")
        except Exception:
            user_prompt = ""
        if not user_prompt:
            return None

        # Gate 5: source_completion_id dedupe (FK lookup, idempotent).
        existing_stmt = (
            select(TestCase.id)
            .join(TestSuite, TestSuite.id == TestCase.suite_id)
            .where(TestSuite.organization_id == organization_id)
            .where(TestCase.source_completion_id == str(completion.id))
            .where(TestCase.status != TEST_CASE_STATUS_ARCHIVED)
            .where(TestCase.deleted_at.is_(None))
            .limit(1)
        )
        if (await db.execute(existing_stmt)).first() is not None:
            logger.info(
                f"draft_eval_from_feedback: completion {completion_id} already has a draft; skipping"
            )
            return None

        # Gate 6: classifier dedupe vs DS-scoped shortlist.
        candidates = await self._fetch_dedupe_shortlist(
            db, organization_id, data_source_ids, limit=50,
        )
        if candidates:
            try:
                duplicate_match = await self._classify_duplicate(
                    db=db,
                    organization=organization,
                    user=user,
                    new_prompt=user_prompt,
                    new_tools=tools_used,
                    candidates=candidates,
                )
            except Exception as cls_err:
                logger.warning(f"draft_eval_from_feedback: classifier failed: {cls_err}")
                duplicate_match = None
            if duplicate_match and duplicate_match.get("duplicate"):
                logger.info(
                    "draft_eval_from_feedback: classifier flagged duplicate "
                    f"matched_id={duplicate_match.get('matched_id')} "
                    f"reason={duplicate_match.get('reason')!r}"
                )
                try:
                    await audit_service.log(
                        db=db,
                        organization_id=organization_id,
                        action="eval.auto_draft_skipped",
                        user_id=user_id,
                        resource_type="completion",
                        resource_id=str(completion.id),
                        details={
                            "reason": "classifier_duplicate",
                            "matched_id": duplicate_match.get("matched_id"),
                            "classifier_reason": duplicate_match.get("reason"),
                        },
                    )
                except Exception:
                    pass
                return None

        # === Build CreateEvalInput ===
        # Name: short, derived from the prompt.
        name = (user_prompt.strip().splitlines() or [user_prompt])[0]
        if len(name) > 80:
            name = name[:77].rstrip() + "…"

        # Templated judge rubric. The human reviewing the draft can
        # sharpen this before promoting; the templated form is honest
        # about what the auto path can deliver without a planner.
        rubric = (
            f"The answer correctly addresses the user's question: {user_prompt}. "
            f"Reject if the data is irrelevant, contradicts the question, or misses the asked metric, "
            f"time window, or filter criteria. Tools used in the original successful run: "
            f"{', '.join(tools_used) if tools_used else 'create_data'}."
        )

        rules: List[Dict[str, Any]] = []
        for tool_name in tools_used:
            rules.append({"type": "tool.calls", "tool": tool_name, "min_calls": 1})
        if not any(r.get("type") == "tool.calls" and r.get("tool") == "create_data" for r in rules):
            rules.append({"type": "tool.calls", "tool": "create_data", "min_calls": 1})
        rules.append({"type": "judge", "prompt": rubric})

        try:
            tool_input = CreateEvalInput(
                name=name,
                prompt=CreateEvalPrompt(content=user_prompt),
                expectations={"spec_version": 1, "rules": rules, "order_mode": "flexible"},
                data_source_ids=data_source_ids,
                tags=["auto", "feedback"],
                # status / suite_id ignored — knowledge mode forces both
                status=None,
                suite_id=None,
            ).model_dump()
        except Exception as build_err:
            logger.warning(f"draft_eval_from_feedback: failed to build CreateEvalInput: {build_err}")
            return None

        # Synthetic runtime_ctx: knowledge mode tells CreateEvalTool to
        # force draft + drafts suite + auto_generated and to populate
        # provenance from head_completion / agent_execution.
        runtime_ctx = {
            "db": db,
            "organization": organization,
            "user": user,
            "head_completion": head_completion,
            "agent_execution_id": str(agent_execution.id) if agent_execution else None,
            "mode": "knowledge",
        }

        tool = CreateEvalTool()
        created_summary: Optional[dict] = None
        async for ev in tool.run_stream(tool_input, runtime_ctx):
            try:
                if ev.type == "tool.end":
                    payload = getattr(ev, "payload", None) or {}
                    output = payload.get("output") or {}
                    if output.get("success"):
                        created_summary = {
                            "case_id": output.get("case_id"),
                            "name": output.get("name"),
                            "suite_id": output.get("suite_id"),
                            "suite_name": output.get("suite_name"),
                            "status": output.get("status"),
                        }
                elif ev.type == "tool.error":
                    logger.warning(
                        f"draft_eval_from_feedback: CreateEvalTool error: {getattr(ev, 'payload', None)}"
                    )
            except Exception:
                continue

        if not created_summary:
            return None

        try:
            await telemetry.capture(
                "eval_draft_auto_created",
                {
                    "completion_id": str(completion.id),
                    "case_id": created_summary.get("case_id"),
                    "tool_set": tools_used,
                    "data_source_count": len(data_source_ids),
                },
                user_id=user_id,
                org_id=organization_id,
            )
        except Exception:
            pass

        try:
            await audit_service.log(
                db=db,
                organization_id=organization_id,
                action="eval.auto_drafted",
                user_id=user_id,
                resource_type="test_case",
                resource_id=created_summary.get("case_id"),
                details={
                    "completion_id": str(completion.id),
                    "tools_used": tools_used,
                    "data_source_count": len(data_source_ids),
                },
            )
        except Exception:
            pass

        return {"created": created_summary.get("case_id"), "name": created_summary.get("name")}

    @staticmethod
    def _extract_data_source_ids(tool_executions) -> List[str]:
        """Walk ``create_data`` inputs and pull the DataSource ids the
        agent actually queried. Mirrors the shape used elsewhere in the
        codebase (``tables_by_source`` as a list of
        ``{data_source_id, tables}``).
        """
        ids: set = set()
        for te in tool_executions:
            args = getattr(te, "arguments_json", None) or {}
            if not isinstance(args, dict):
                continue
            tbs = args.get("tables_by_source")
            if isinstance(tbs, list):
                for entry in tbs:
                    if isinstance(entry, dict) and entry.get("data_source_id"):
                        ids.add(str(entry["data_source_id"]))
            elif isinstance(tbs, dict):
                for ds_id in tbs.keys():
                    if ds_id:
                        ids.add(str(ds_id))
            for ds_id in args.get("data_source_ids", []) or []:
                if ds_id:
                    ids.add(str(ds_id))
        return sorted(ids)

    @staticmethod
    async def _fetch_dedupe_shortlist(
        db: AsyncSession,
        organization_id: str,
        data_source_ids: List[str],
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Pull existing eval cases scoped to the candidate's data sources
        for the dedupe classifier. When the candidate touches no DS, falls
        back to evals with empty/null DS lists. Always excludes archived.
        """
        from app.models.eval import TEST_CASE_STATUS_ARCHIVED, TestCase, TestSuite
        from sqlalchemy import cast, or_, String as SAString

        stmt = (
            select(TestCase, TestSuite.name)
            .join(TestSuite, TestSuite.id == TestCase.suite_id)
            .where(TestSuite.organization_id == str(organization_id))
            .where(TestCase.deleted_at.is_(None))
            .where(TestCase.status != TEST_CASE_STATUS_ARCHIVED)
        )
        if data_source_ids:
            # Coarse JSON-substring filter — portable across SQLite and
            # Postgres. Final dedupe judgment happens in the LLM, so we
            # just need a reasonable bounded shortlist here.
            ors = []
            for ds_id in data_source_ids:
                ors.append(cast(TestCase.data_source_ids_json, SAString).ilike(f"%{ds_id}%"))
            stmt = stmt.where(or_(*ors))
        else:
            # Candidate has no DS — scope to evals that also have empty/null
            # data_source_ids_json so we don't compare apples to oranges.
            stmt = stmt.where(
                or_(
                    TestCase.data_source_ids_json.is_(None),
                    cast(TestCase.data_source_ids_json, SAString).in_(["[]", "null", ""]),
                )
            )

        stmt = stmt.order_by(TestCase.created_at.desc()).limit(limit)
        rows = (await db.execute(stmt)).all()

        out: List[Dict[str, Any]] = []
        for case, suite_name in rows:
            pj = case.prompt_json or {}
            content = pj.get("content") if isinstance(pj, dict) else ""
            rules = (case.expectations_json or {}).get("rules") or []
            tool_names = sorted({
                r.get("tool") for r in rules
                if isinstance(r, dict) and r.get("type") == "tool.calls" and r.get("tool")
            })
            out.append({
                "id": str(case.id),
                "prompt": (content or "")[:400],
                "tools": tool_names,
                "suite_name": suite_name or "",
            })
        return out

    async def _classify_duplicate(
        self,
        *,
        db: AsyncSession,
        organization: Organization,
        user: User,
        new_prompt: str,
        new_tools: List[str],
        candidates: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Single small-model inference: is the new candidate a duplicate
        of any existing eval? Returns
        ``{"duplicate": bool, "matched_id": str|None, "reason": str}``.
        """
        import json as _json
        from app.ai.llm import LLM
        from app.services.llm_service import LLMService

        llm_service = LLMService()
        small_model = await llm_service.get_default_model(db, organization, user, is_small=True)
        if small_model is None:
            return None

        prompt = f"""You are a deduplication classifier for analytics evals.
Decide whether the NEW prompt is essentially the same question as any of the EXISTING evals listed below. "Essentially the same" means: same metric, same time window, same filter intent, same population — even if phrased differently. Surface-level paraphrases are duplicates. Different metrics, different time windows, or different filters are NOT duplicates.

Return ONLY a JSON object on a single line, no prose:
{{"duplicate": true|false, "matched_id": "<id>"|null, "reason": "<short>"}}

NEW:
prompt: {_json.dumps(new_prompt)}
tools_used: {_json.dumps(new_tools)}

EXISTING (id, prompt, tools):
{_json.dumps(candidates, ensure_ascii=False)}
"""

        llm = LLM(small_model)
        try:
            text = llm.inference(
                prompt,
                usage_scope="suggest_eval.dedupe_classifier",
                usage_scope_ref_id=None,
            )
        except Exception as e:
            logger.warning(f"_classify_duplicate inference failed: {e}")
            return None

        # Best-effort JSON extraction — small models occasionally wrap in
        # text or include trailing commentary.
        try:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                # Strip markdown fences if the model used them.
                cleaned = cleaned.strip("`")
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                cleaned = cleaned[start:end + 1]
            parsed = _json.loads(cleaned)
            if not isinstance(parsed, dict):
                return None
            return {
                "duplicate": bool(parsed.get("duplicate")),
                "matched_id": parsed.get("matched_id") if parsed.get("matched_id") else None,
                "reason": str(parsed.get("reason") or "")[:300],
            }
        except Exception:
            return None