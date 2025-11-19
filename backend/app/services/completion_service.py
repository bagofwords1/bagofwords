import asyncio
from fastapi.responses import StreamingResponse
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from app.models.plan import Plan
from app.models.completion import Completion
from app.models.report import Report
from app.models.widget import Widget
from app.models.mention import Mention, MentionType
from app.models.organization import Organization
from app.models.step import Step
from app.models.user import User

from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.completion_schema import CompletionSchema, PromptSchema
from app.schemas.completion_v2_schema import CompletionCreate, CompletionContextEstimateSchema
from app.schemas.step_schema import StepSchema
from app.schemas.widget_schema import WidgetSchema
from app.schemas.completion_v2_schema import (
    CompletionV2Schema,
    CompletionBlockV2Schema,
    ToolExecutionUISchema,
    CompletionsV2Response,
)
from app.services.llm_service import LLMService
from app.serializers.completion_v2 import serialize_block_v2
from app.schemas.agent_execution_schema import PlanDecisionSchema
from app.schemas.sse_schema import SSEEvent, format_sse_event
from app.streaming.completion_stream import CompletionEventQueue


from app.services.step_service import StepService
from app.services.widget_service import WidgetService
from app.services.report_service import ReportService
from app.services.mention_service import MentionService
from app.services.memory_service import MemoryService
from app.services.data_source_service import DataSourceService

from app.websocket_manager import websocket_manager
from app.settings.database import create_async_session_factory

from sqlalchemy import select, update, func

from fastapi import BackgroundTasks, HTTPException
from app.core.telemetry import telemetry

from app.ai.agent_v2 import AgentV2
from pydantic import ValidationError

# Models used for v2 assembly
from app.models.completion_block import CompletionBlock
from app.models.plan_decision import PlanDecision
from app.models.tool_execution import ToolExecution
from app.models.agent_execution import AgentExecution
from app.models.instruction import Instruction


async def _get_instruction_suggestions_for_completion(
    db: AsyncSession, 
    completion: Completion, 
    agent_execution: AgentExecution | None
) -> list[dict] | None:
    """Get instruction suggestions for a specific completion if it generated them."""
    if not agent_execution or completion.role != 'system' or completion.status not in ['success', 'completed']:
        return None
        
    # Check if this agent execution created any instructions - get full instruction objects
    instr_stmt = (
        select(Instruction)
        .where(Instruction.agent_execution_id == agent_execution.id)
        .where(Instruction.deleted_at == None)
        .order_by(Instruction.created_at.asc())
    )
    instr_res = await db.execute(instr_stmt)
    instructions = instr_res.scalars().all()
    
    if not instructions:
        return None
    
    # Convert to dict format with all relevant fields
    instructions_data = []
    for instr in instructions:
        if not (instr.text or "").strip():
            continue
            
        instruction_data = {
            "id": str(instr.id),
            "text": instr.text,
            "category": instr.category,
            "status": instr.status,
            "private_status": instr.private_status,
            "global_status": instr.global_status,
            "is_seen": instr.is_seen,
            "can_user_toggle": instr.can_user_toggle,
            "user_id": instr.user_id,
            "organization_id": str(instr.organization_id),
            "agent_execution_id": str(instr.agent_execution_id) if instr.agent_execution_id else None,
            "trigger_reason": instr.trigger_reason,
            "created_at": instr.created_at.isoformat() if instr.created_at else None,
            "updated_at": instr.updated_at.isoformat() if instr.updated_at else None,
            "ai_source": getattr(instr, 'ai_source', None),
        }
        instructions_data.append(instruction_data)
    
    return instructions_data if instructions_data else None


import re

class CompletionService:

    def __init__(self):
        self.step_service = StepService()
        self.widget_service = WidgetService()
        self.report_service = ReportService()
        self.memory_service = MemoryService()
        self.mention_service = MentionService()
        self.llm_service = LLMService()
        self.data_source_service = DataSourceService()

    async def _serialize_completion(self, db: AsyncSession, completion: Completion, current_user: User = None, organization: Organization = None) -> CompletionSchema:
        """Serialize a completion model to a schema following get_completions format"""
        if completion.role == "user":
            prompt = PromptSchema.from_orm(completion.prompt)
            completion_prompt = None
        else: # ai_agent or system
            completion_prompt = PromptSchema.from_orm(completion.completion)
            prompt = None

        if completion.widget_id and current_user and organization:
            widget = await self.widget_service.get_widget_by_id(db, str(completion.widget_id), current_user, organization)
        else:
            widget = None

        if completion.step_id:
            step = await self.step_service.get_step_by_id(db, completion.step_id)
        else:
            step = None

        return CompletionSchema(
            id=completion.id,
            prompt=prompt,
            completion=completion_prompt,
            model=completion.model,
            status=completion.status,
            sigkill=completion.sigkill,
            turn_index=completion.turn_index,
            parent_id=completion.parent_id,
            message_type=completion.message_type,
            role=completion.role,
            report_id=completion.report_id,
            created_at=completion.created_at,
            updated_at=completion.updated_at,
            step_id=completion.step_id,
            step=StepSchema.from_orm(step) if step else None,
            widget=WidgetSchema.from_orm(widget).copy(
                update={"last_step": await self.widget_service._get_last_step(db, widget.id)}
            ) if completion.role == "system" and widget else None
        )

    async def estimate_completion_tokens(
        self,
        db: AsyncSession,
        report_id: str,
        completion_data: CompletionCreate,
        current_user: User,
        organization: Organization,
        external_user_id: str = None,
        external_platform: str = None,
    ) -> CompletionContextEstimateSchema:
        try:
            if not completion_data or not completion_data.prompt or not completion_data.prompt.content:
                raise HTTPException(status_code=400, detail="Prompt content is required for estimation.")

            report_res = await db.execute(select(Report).filter(Report.id == report_id))
            report = report_res.scalar_one_or_none()
            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            if completion_data.prompt.widget_id:
                widget_res = await db.execute(select(Widget).filter(Widget.id == completion_data.prompt.widget_id))
                widget = widget_res.scalar_one_or_none()
                if not widget:
                    raise HTTPException(status_code=404, detail="Widget not found")
            else:
                widget = None

            if completion_data.prompt.step_id:
                step_res = await db.execute(select(Step).filter(Step.id == completion_data.prompt.step_id))
                step = step_res.scalar_one_or_none()
                if not step:
                    raise HTTPException(status_code=404, detail="Step not found")
            else:
                step = None

            if completion_data.prompt.model_id:
                model = await self.llm_service.get_model_by_id(db, organization, current_user, completion_data.prompt.model_id)
            else:
                model = await organization.get_default_llm_model(db)

            if not model:
                raise HTTPException(
                    status_code=400,
                    detail="No default LLM model configured. Please configure a default model in organization settings."
                )

            small_model = await self.llm_service.get_default_model(db, organization, current_user, is_small=True)
            org_settings = await organization.get_settings(db)

            prompt_dict = completion_data.prompt.dict()
            if prompt_dict.get('widget_id'):
                prompt_dict['widget_id'] = str(prompt_dict['widget_id'])

            head_stub = SimpleNamespace(
                id=str(uuid4()),
                prompt=prompt_dict,
                report_id=report.id,
                widget_id=str(widget.id) if widget else None,
                step_id=str(step.id) if step else None,
                user=current_user,
                user_id=current_user.id,
                external_platform=external_platform,
                external_user_id=external_user_id,
            )
            system_stub = SimpleNamespace(
                id=str(uuid4()),
                prompt=None,
                status="in_progress",
            )

            clients = {}
            for data_source in report.data_sources:
                clients[data_source.name] = await self.data_source_service.construct_client(db, data_source, current_user)

            agent = AgentV2(
                db=db,
                organization=organization,
                organization_settings=org_settings,
                model=model,
                small_model=small_model,
                report=report,
                messages=[],
                head_completion=head_stub,
                system_completion=system_stub,
                widget=widget,
                step=step,
                clients=clients,
                mode=completion_data.prompt.mode,
            )

            try:
                estimate = await agent.estimate_prompt_tokens()
            except ValidationError as ve:
                raise HTTPException(status_code=400, detail=f"Unable to build planner input for estimation: {str(ve)}")

            prompt_tokens = estimate.get("prompt_tokens", 0)
            model_limit = estimate.get("model_limit") or getattr(model, "context_window_tokens", None)
            remaining_tokens = estimate.get("remaining_tokens")
            if remaining_tokens is None and model_limit is not None:
                remaining_tokens = max(model_limit - prompt_tokens, 0)
            near_limit = bool(model_limit and prompt_tokens >= 0.9 * model_limit)
            context_usage_pct = None
            if model_limit and model_limit > 0:
                context_usage_pct = round((prompt_tokens / model_limit) * 100, 2)

            return CompletionContextEstimateSchema(
                model_id=getattr(model, "model_id", ""),
                model_name=getattr(model, "name", None),
                prompt_tokens=prompt_tokens,
                model_limit=model_limit,
                remaining_tokens=remaining_tokens,
                near_limit=near_limit,
                context_usage_pct=context_usage_pct,
            )
        except HTTPException as he:
            raise he
        except Exception as e:
            logging.error(f"Unexpected error in estimate_completion_tokens: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected error: {str(e)}"
            )

    async def create_completion(
        self, 
        db: AsyncSession, 
        report_id: str, 
        completion_data: CompletionCreate, 
        current_user: User, 
        organization: Organization, 
        background: bool = False,
        external_user_id: str = None,
        external_platform: str = None,
    ):
        try:
            print("CompletionService: Starting create_completion (v2, non-stream)")

            # Validate report exists
            result = await db.execute(select(Report).filter(Report.id == report_id))
            report = result.scalar_one_or_none()
            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            # Validate widget if provided
            if completion_data.prompt and completion_data.prompt.widget_id:
                result = await db.execute(select(Widget).filter(Widget.id == completion_data.prompt.widget_id))
                widget = result.scalar_one_or_none()
                if not widget:
                    raise HTTPException(status_code=404, detail="Widget not found")
            else:
                widget = None

            # Validate step if provided
            if completion_data.prompt and completion_data.prompt.step_id:
                step = await db.execute(select(Step).filter(Step.id == completion_data.prompt.step_id))
                step = step.scalar_one_or_none()
                if not step:
                    raise HTTPException(status_code=404, detail="Step not found")
            else:
                step = None

            # Get default model - this is critical

            if completion_data.prompt and completion_data.prompt.model_id:
                model = await self.llm_service.get_model_by_id(db, organization, current_user, completion_data.prompt.model_id)
            else:
                model = await organization.get_default_llm_model(db)
            
            small_model = await self.llm_service.get_default_model(db, organization, current_user, is_small=True)
            
            if not model:
                raise HTTPException(
                    status_code=400,
                    detail="No default LLM model configured. Please configure a default model in organization settings."
                )

            # Create user completion (head)
            prompt_dict = completion_data.prompt.dict() if completion_data.prompt else {}
            prompt_dict['widget_id'] = str(prompt_dict['widget_id']) if prompt_dict.get('widget_id') else None
            last_completion = await self.get_last_completion(db, report.id)
            head_completion = Completion(
                prompt=prompt_dict or None,
                model=model.model_id,
                widget_id=str(widget.id) if widget else None,
                report_id=report.id,
                turn_index=last_completion.turn_index + 1 if last_completion else 0,
                message_type="table",
                role="user",
                status="success",
                user_id=current_user.id,
                external_user_id=external_user_id,
                external_platform=external_platform
            )

            try:
                db.add(head_completion)
                await db.commit()
                await db.refresh(head_completion)
            except Exception as e:
                await db.rollback()
                raise HTTPException(status_code=500, detail=f"Failed to save user completion: {str(e)}")

            # Store mentions associated with the user head completion (best-effort)
            try:
                await self.mention_service.create_completion_mentions(db, head_completion)
            except Exception as e:
                logging.error(f"Failed to create mentions for completion {head_completion.id}: {e}")

            # Create system completion to populate with results
            system_completion = Completion(
                prompt=None,
                completion={"content": ""},
                model=model.model_id,
                widget_id=prompt_dict.get('widget_id'),
                report_id=report.id,
                parent_id=head_completion.id,
                turn_index=head_completion.turn_index + 1,
                message_type="table",
                role="system",
                status="in_progress",
                external_platform=external_platform,
                external_user_id=external_user_id
            )

            try:
                db.add(system_completion)
                await db.commit()
                await db.refresh(system_completion)
            except Exception as e:
                await db.rollback()
                raise HTTPException(status_code=500, detail=f"Failed to save system completion: {str(e)}")

            org_settings = await organization.get_settings(db)

            if background:
                logging.info("CompletionService: Scheduling background agent (non-stream API)")

                async def run_agent_task():
                    async_session = create_async_session_factory()
                    async with async_session() as session:
                        try:
                            report_obj = await session.get(Report, report.id)
                            head_obj = await session.get(Completion, head_completion.id)
                            system_obj = await session.get(Completion, system_completion.id)
                            widget_obj = await session.get(Widget, widget.id) if widget else None
                            step_obj = await session.get(Step, step.id) if step else None

                            if not all([report_obj, head_obj, system_obj]):
                                logging.error("Background agent init failed: missing objects")
                                return
                            
                            clients = {}
                            for data_source in report_obj.data_sources:
                                clients[data_source.name] = await self.data_source_service.construct_client(session, data_source, current_user)

                            agent = AgentV2(
                                db=session,
                                organization=organization,
                                organization_settings=org_settings,
                                model=model,
                                small_model=small_model,
                                report=report_obj,
                                messages=[],
                                head_completion=head_obj,
                                system_completion=system_obj,
                                widget=widget_obj,
                                step=step_obj,
                                clients=clients,
                            )
                            await agent.main_execution()
                        except Exception as e:
                            logging.error(f"Agent background execution failed: {e}")
                            try:
                                await session.execute(
                                    update(Completion)
                                    .where(Completion.id == system_completion.id)
                                    .values(status='error', completion={'content': f"Agent failed: {str(e)}", 'error': True})
                                )
                                await session.commit()
                            except Exception:
                                pass

                asyncio.create_task(run_agent_task())
                # Return minimal v2 response with just created placeholders
                v2_list = await self._assemble_v2_for_completion_ids(db, [head_completion.id, system_completion.id])
                return CompletionsV2Response(
                    report_id=report.id,
                    completions=v2_list,
                    total_completions=len(v2_list),
                    total_blocks=sum(len(c.completion_blocks or []) for c in v2_list),
                    total_widgets_created=0,
                    total_steps_created=0,
                    earliest_completion=min((c.created_at for c in v2_list), default=None),
                    latest_completion=max((c.updated_at for c in v2_list), default=None),
                )
            else:
                try:
                    # Foreground execution (wait and return final v2)
                    clients = {}
                    for data_source in report.data_sources:
                        clients[data_source.name] = await self.data_source_service.construct_client(db, data_source, current_user)
                    agent = AgentV2(
                        db=db,
                        organization=organization,
                        organization_settings=org_settings,
                        model=model,
                        small_model=small_model,
                        report=report,
                        messages=[],
                        head_completion=head_completion,
                        system_completion=system_completion,
                        widget=widget,
                        step=step,
                        clients=clients,
                    )
                    await agent.main_execution()

                    # Assemble v2 for the new message pair (user + system children)
                    response_completions = await self._get_response_completions(db, head_completion, current_user, organization)
                    ids = [c.id for c in response_completions]
                    v2_list = await self._assemble_v2_for_completion_ids(db, ids)

                    # Compute aggregates similar to get_completions_v2 but for this set
                    earliest = min((c.created_at for c in v2_list), default=None)
                    latest = max((c.updated_at for c in v2_list), default=None)
                    total_blocks = sum(len(c.completion_blocks or []) for c in v2_list)
                    # Best-effort counts from tool_execution created artifacts
                    total_widgets = 0
                    total_steps = 0
                    for c in v2_list:
                        for b in (c.completion_blocks or []):
                            te = getattr(b, 'tool_execution', None)
                            if te and getattr(te, 'created_widget', None):
                                total_widgets += 1
                            if te and getattr(te, 'created_step', None):
                                total_steps += 1

                    return CompletionsV2Response(
                        report_id=report.id,
                        completions=v2_list,
                        total_completions=len(v2_list),
                        total_blocks=total_blocks,
                        total_widgets_created=total_widgets,
                        total_steps_created=total_steps,
                        earliest_completion=earliest,
                        latest_completion=latest,
                    )
                except Exception as e:
                    await self._create_error_completion(db, head_completion, str(e))
                    raise HTTPException(status_code=500, detail=f"Agent execution failed: {str(e)}")

        except HTTPException as he:
            # Log the error and re-raise HTTP exceptions
            logging.error(f"HTTP Exception in create_completion: {str(he)}")
            raise he
        except Exception as e:
            # Log and convert unexpected errors to HTTP exceptions
            logging.error(f"Unexpected error in create_completion: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected error: {str(e)}"
            )

    async def get_completion_stream(self, db: AsyncSession, completion_id: str, report_id: str):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        return completion


    def _validate_prompt(self, prompt):
        return prompt


    async def get_completions(self, db: AsyncSession, report_id: str, organization: Organization, current_user: User):
        report = await self.report_service.get_report(db, report_id, current_user, organization)

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        completions = await db.execute(select(Completion).where(Completion.report_id == report_id).order_by(Completion.created_at.asc()))
        completions = completions.scalars().all()
        
        response = []
        for completion in completions:
            serialized_completion = await self._serialize_completion(db, completion, current_user, organization)
            response.append(serialized_completion)

        return response


    async def get_memories(self, db: AsyncSession, completion_id: str, organization: Organization):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        report = await self._can_access(db, Report, completion.report_id, organization)

        memories = select(Mention).where(Mention.completion_id == completion_id, Mention.type == MentionType.MEMORY)
        memories = await db.execute(memories)
        memories = memories.scalars().all()
        return memories

    async def get_last_completion(self, db: AsyncSession, report_id: str):
        stmt = select(Completion).where(Completion.report_id == report_id).order_by(Completion.created_at.desc()).limit(1)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_completions_v2(
        self,
        db: AsyncSession,
        report_id: str,
        organization: Organization,
        current_user: User,
        limit: int = 10,
        before: str | None = None,
    ) -> CompletionsV2Response:
        """Assemble v2 completions response efficiently with batched queries.

        Returns the last `limit` completions (user+system) in reverse chronological order,
        then sorted ascending for UI render. If `before` is provided (ISO8601), fetches
        items strictly before that timestamp (cursor pagination).
        """
        # Validate access
        report = await self.report_service.get_report(db, report_id, current_user, organization)
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # 1) Fetch last N completions (user + system) with optional cursor
        completions_stmt = select(Completion).where(Completion.report_id == report_id)
        if before:
            try:
                from datetime import datetime as _dt
                before_dt = _dt.fromisoformat(before)
                completions_stmt = completions_stmt.where(Completion.created_at < before_dt)
            except Exception:
                pass
        # Order newest first, fetch one extra to determine has_more
        completions_stmt = completions_stmt.order_by(Completion.created_at.desc()).limit(limit + 1)
        completions_res = await db.execute(completions_stmt)
        fetched_desc = completions_res.scalars().all()
        has_more = len(fetched_desc) > limit
        if has_more:
            fetched_desc = fetched_desc[:limit]
        # Reverse into chronological order for UI
        all_completions = list(reversed(fetched_desc))

        if not all_completions:
            return CompletionsV2Response(
                report_id=report_id,
                completions=[],
                total_completions=0,
                total_blocks=0,
                total_widgets_created=0,
                total_steps_created=0,
                earliest_completion=None,
                latest_completion=None,
                has_more=False,
                next_before=None,
            )

        completion_ids = [c.id for c in all_completions]
        system_completion_ids = [c.id for c in all_completions if c.role == 'system']

        # 2) Fetch agent executions for these completions (both roles to map quickly)
        ae_stmt = select(AgentExecution).where(AgentExecution.completion_id.in_(completion_ids))
        ae_res = await db.execute(ae_stmt)
        execs = ae_res.scalars().all()
        completion_id_to_exec = {e.completion_id: e for e in execs}
        exec_ids = [e.id for e in execs]

        # 3) Fetch blocks for system completions only, with single joined query to hydrate decision/tool and created artifacts IDs
        blocks: list[CompletionBlock] = []
        pd_map: dict[str, PlanDecision] = {}
        te_map: dict[str, ToolExecution] = {}
        if system_completion_ids:
            blocks_join_stmt = (
                select(
                    CompletionBlock,
                    PlanDecision,
                    ToolExecution,
                )
                .where(CompletionBlock.completion_id.in_(system_completion_ids))
                .outerjoin(PlanDecision, CompletionBlock.plan_decision_id == PlanDecision.id)
                .outerjoin(ToolExecution, CompletionBlock.tool_execution_id == ToolExecution.id)
                .order_by(CompletionBlock.completion_id.asc(), CompletionBlock.block_index.asc())
            )
            join_res = await db.execute(blocks_join_stmt)
            for row in join_res.all():
                b: CompletionBlock = row[0]
                pd: PlanDecision | None = row[1]
                te: ToolExecution | None = row[2]
                blocks.append(b)
                if pd is not None:
                    pd_map[pd.id] = pd
                if te is not None:
                    te_map[te.id] = te

        # 5) Build per-completion block lists and compute aggregates
        completion_id_to_blocks: dict[str, list[CompletionBlockV2Schema]] = {cid: [] for cid in completion_ids}
        total_blocks = 0
        total_widgets = 0
        total_steps = 0

        for b in blocks:
            # Count created artifacts for aggregates using the joined ToolExecution
            if b.tool_execution_id and b.tool_execution_id in te_map:
                te = te_map[b.tool_execution_id]
                if te.created_widget_id:
                    total_widgets += 1
                if te.created_step_id:
                    total_steps += 1

            # Use the enriched serializer so the block includes created_widget (with last_step)
            # and created_step, and strips heavy widget_data from result_json
            block_schema = await serialize_block_v2(db, b)

            completion_id_to_blocks[b.completion_id].append(block_schema)
            total_blocks += 1

        # 6) Assemble completion objects (attach simple per-AE suggestions list outside blocks)
        v2_completions: list[CompletionV2Schema] = []
        for c in all_completions:
            exec_obj = completion_id_to_exec.get(c.id)
            c_blocks = completion_id_to_blocks.get(c.id, [])
            # Sort by seq if present, else by block_index
            c_blocks.sort(key=lambda x: (x.seq if x.seq is not None else 10_000_000, x.block_index))

            summary = {
                "total_blocks": len(c_blocks),
            }

            # Handle completion field - ensure it's a dict, not an empty string
            completion_data = c.completion
            if isinstance(completion_data, str):
                if completion_data == "":
                    completion_data = {}
                else:
                    try:
                        import json
                        completion_data = json.loads(completion_data)
                    except (json.JSONDecodeError, TypeError):
                        completion_data = {"content": completion_data}

            # Get instruction suggestions for this completion if it generated them
            suggestions_list = await _get_instruction_suggestions_for_completion(db, c, exec_obj)

            v2 = CompletionV2Schema(
                id=c.id,
                role=c.role,
                status=c.status,
                model=c.model,
                turn_index=c.turn_index,
                parent_id=c.parent_id,
                report_id=c.report_id,
                agent_execution_id=exec_obj.id if exec_obj else None,
                prompt=c.prompt,
                completion_blocks=c_blocks,
                created_widgets=[],
                created_steps=[],
                summary=summary,
                sigkill=c.sigkill,
                created_at=c.created_at,
                updated_at=c.updated_at,
                instruction_suggestions=suggestions_list,
            )
            v2_completions.append(v2)

        # 7) Global aggregates
        earliest = min((c.created_at for c in all_completions), default=None)
        latest = max((c.updated_at for c in all_completions), default=None)

        return CompletionsV2Response(
            report_id=report_id,
            completions=v2_completions,
            total_completions=len(v2_completions),
            total_blocks=total_blocks,
            total_widgets_created=total_widgets,
            total_steps_created=total_steps,
            earliest_completion=earliest,
            latest_completion=latest,
            has_more=has_more,
            next_before=earliest,
        )

    async def _assemble_v2_for_completion_ids(self, db: AsyncSession, completion_ids: list[str]) -> list[CompletionV2Schema]:
        """Build v2 completion objects for specific completion IDs.

        Mirrors the assembly logic from get_completions_v2 but scoped to a subset.
        """
        if not completion_ids:
            return []

        # Fetch completions preserving created_at order
        completions_stmt = select(Completion).where(Completion.id.in_(completion_ids)).order_by(Completion.created_at.asc())
        completions_res = await db.execute(completions_stmt)
        all_completions = completions_res.scalars().all()

        ids = [c.id for c in all_completions]
        system_ids = [c.id for c in all_completions if c.role == 'system']

        # Agent executions for these completions
        ae_stmt = select(AgentExecution).where(AgentExecution.completion_id.in_(ids))
        ae_res = await db.execute(ae_stmt)
        execs = ae_res.scalars().all()
        completion_id_to_exec = {e.completion_id: e for e in execs}

        # Blocks joined with decision/tool for system completions
        blocks: list[CompletionBlock] = []
        pd_map: dict[str, PlanDecision] = {}
        te_map: dict[str, ToolExecution] = {}
        if system_ids:
            join_stmt = (
                select(
                    CompletionBlock,
                    PlanDecision,
                    ToolExecution,
                )
                .where(CompletionBlock.completion_id.in_(system_ids))
                .outerjoin(PlanDecision, CompletionBlock.plan_decision_id == PlanDecision.id)
                .outerjoin(ToolExecution, CompletionBlock.tool_execution_id == ToolExecution.id)
                .order_by(CompletionBlock.completion_id.asc(), CompletionBlock.block_index.asc())
            )
            join_res = await db.execute(join_stmt)
            for row in join_res.all():
                b: CompletionBlock = row[0]
                pd: PlanDecision | None = row[1]
                te: ToolExecution | None = row[2]
                blocks.append(b)
                if pd is not None:
                    pd_map[pd.id] = pd
                if te is not None:
                    te_map[te.id] = te

        # Build per-completion block lists
        completion_id_to_blocks: dict[str, list[CompletionBlockV2Schema]] = {cid: [] for cid in ids}
        for b in blocks:
            block_schema = await serialize_block_v2(db, b)
            completion_id_to_blocks[b.completion_id].append(block_schema)

        # Assemble v2 objects
        v2_list: list[CompletionV2Schema] = []
        for c in all_completions:
            exec_obj = completion_id_to_exec.get(c.id)
            c_blocks = completion_id_to_blocks.get(c.id, [])
            c_blocks.sort(key=lambda x: (x.seq if x.seq is not None else 10_000_000, x.block_index))

            # Normalize completion payload to dict
            completion_data = c.completion
            if isinstance(completion_data, str):
                if completion_data == "":
                    completion_data = {}
                else:
                    try:
                        completion_data = json.loads(completion_data)
                    except (json.JSONDecodeError, TypeError):
                        completion_data = {"content": completion_data}

            # Get instruction suggestions for this completion if it generated them
            suggestions_list = await _get_instruction_suggestions_for_completion(db, c, exec_obj)

            v2 = CompletionV2Schema(
                id=c.id,
                role=c.role,
                status=c.status,
                model=c.model,
                turn_index=c.turn_index,
                parent_id=c.parent_id,
                report_id=c.report_id,
                agent_execution_id=exec_obj.id if exec_obj else None,
                prompt=c.prompt,
                completion=completion_data,
                completion_blocks=c_blocks,
                created_widgets=[],
                created_steps=[],
                summary={"total_blocks": len(c_blocks)},
                sigkill=c.sigkill,
                created_at=c.created_at,
                updated_at=c.updated_at,
                instruction_suggestions=suggestions_list,
            )
            v2_list.append(v2)

        return v2_list
    
    async def _create_error_completion(self, db: AsyncSession, completion: Completion, error: str):
        error_completion = Completion(
            model=completion.model,
            completion={"content": error, "error": True},
            prompt=None,
            status="error",
            parent_id=completion.id,
            message_type="error",
            role="system",
            report_id=completion.report_id,
            widget_id=completion.widget_id
        )

        db.add(error_completion)
        await db.commit()
        await db.refresh(error_completion)
        return error_completion
    

    async def get_completion_plans(self, db: AsyncSession, current_user: User, organization: Organization, completion_id: str):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")

        plans = await db.execute(select(Plan).where(Plan.completion_id == completion_id))
        plans = plans.scalars().all()

        if not plans:
            raise HTTPException(status_code=404, detail="Plans not found")

        return plans

    async def update_completion_feedback(self, db: AsyncSession, completion_id: str, vote: int):
        """Legacy endpoint - now redirects to new feedback system"""
        from app.services.completion_feedback_service import CompletionFeedbackService
        from app.schemas.completion_feedback_schema import CompletionFeedbackCreate
        
        # For legacy support, we'll create a system feedback (no user)
        feedback_service = CompletionFeedbackService()
        
        # Get the completion and organization for context
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        # Get organization from completion.report
        if not completion.report:
            raise HTTPException(status_code=400, detail="Completion has no associated report")
        
        organization = completion.report.organization
        
        # Create feedback using new system (as system feedback with no user)
        feedback_data = CompletionFeedbackCreate(
            direction=vote,
            message="Legacy feedback"
        )
        
        feedback = await feedback_service.create_or_update_feedback(
            db, completion_id, feedback_data, None, organization
        )
        
        # Update the completion's feedback_score for backward compatibility
        completion.feedback_score = completion.feedback_score + vote
        await db.commit()
        await db.refresh(completion)

        return completion

    async def create_completion_stream(
        self,
        db: AsyncSession,
        report_id: str,
        completion_data: CompletionCreate,
        current_user: User,
        organization: Organization,
        external_user_id: str = None,
        external_platform: str = None,
    ):
        """Create a completion with real-time streaming events via SSE."""
        try:
            # Validate report exists (same as regular create_completion)
            result = await db.execute(select(Report).filter(Report.id == report_id))
            report = result.scalar_one_or_none()
            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            # Validate widget if provided
            if completion_data.prompt.widget_id:
                result = await db.execute(select(Widget).filter(Widget.id == completion_data.prompt.widget_id))
                widget = result.scalar_one_or_none()
                if not widget:
                    raise HTTPException(status_code=404, detail="Widget not found")
            else:
                widget = None
            
            # Validate step if provided
            if completion_data.prompt.step_id:
                step = await db.execute(select(Step).filter(Step.id == completion_data.prompt.step_id))
                step = step.scalar_one_or_none()
                if not step:
                    raise HTTPException(status_code=404, detail="Step not found")
            else:
                step = None

            # Get default model
            if completion_data.prompt and completion_data.prompt.model_id:
                model = await self.llm_service.get_model_by_id(db, organization, current_user, completion_data.prompt.model_id)
            else:
                model = await organization.get_default_llm_model(db)

            if not model:
                raise HTTPException(
                    status_code=400, 
                    detail="No default LLM model configured. Please configure a default model in organization settings."
                )

            small_model = await self.llm_service.get_default_model(db, organization, current_user, is_small=True)

            # Create user completion
            prompt_dict = completion_data.prompt.dict()
            prompt_dict['widget_id'] = str(prompt_dict['widget_id']) if prompt_dict['widget_id'] else None
            last_completion = await self.get_last_completion(db, report.id)
            completion = Completion(
                prompt=prompt_dict,
                model=model.model_id,
                widget_id=str(widget.id) if widget else None,
                report_id=report.id,
                turn_index=last_completion.turn_index + 1 if last_completion else 0,
                message_type="table",
                role="user",
                status="success",
                user_id=current_user.id,
                external_user_id=external_user_id,
                external_platform=external_platform
            )

            try:
                db.add(completion)
                await db.commit()
                await db.refresh(completion)
            except Exception as e:
                await db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to save user completion: {str(e)}"
                )

            # Store mentions associated with the user completion (best-effort)
            try:
                await self.mention_service.create_completion_mentions(db, completion)
            except Exception as e:
                logging.error(f"Failed to create mentions for completion {completion.id}: {e}")

            # Create system completion
            system_completion = Completion(
                prompt=None,
                completion={"content": ""},
                model=model.model_id,
                widget_id=prompt_dict['widget_id'],
                report_id=report.id,
                parent_id=completion.id,
                turn_index=completion.turn_index + 1,
                message_type="table",
                role="system",
                status="in_progress",
                external_platform=external_platform,
                external_user_id=external_user_id
            )

            try:
                db.add(system_completion)
                await db.commit()
                await db.refresh(system_completion)
            except Exception as e:
                await db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to save system completion: {str(e)}"
                )

            org_settings = await organization.get_settings(db)

            # Create event queue for streaming
            event_queue = CompletionEventQueue()

            async def run_agent_with_streaming():
                """Run agent in background and stream events."""
                async_session = create_async_session_factory()
                async with async_session() as session:
                    try:
                        # Re-fetch all database-dependent objects using the new session
                        report_obj = await session.get(Report, report.id)
                        completion_obj = await session.get(Completion, completion.id)
                        system_completion_obj = await session.get(Completion, system_completion.id)
                        widget_obj = await session.get(Widget, widget.id) if widget else None
                        step_obj = await session.get(Step, step.id) if step else None

                        if not all([report_obj, completion_obj, system_completion_obj]):
                            logging.error("Failed to fetch necessary objects for streaming agent.")
                            error_event = SSEEvent(
                                event="completion.error",
                                completion_id=str(system_completion.id),
                                data={"error": "Failed to initialize agent execution"}
                            )
                            await event_queue.put(error_event)
                            return
                        
                        clients = {}
                        for data_source in report_obj.data_sources:
                            clients[data_source.name] = await self.data_source_service.construct_client(session, data_source, current_user)

                        # Create agent with event queue
                        agent = AgentV2(
                            db=session,
                            organization=organization,
                            organization_settings=org_settings,
                            model=model,
                            small_model=small_model,
                            mode=completion_data.prompt.mode,
                            report=report_obj,
                            messages=[],
                            head_completion=completion_obj,
                            system_completion=system_completion_obj,
                            widget=widget_obj,
                            step=step_obj,
                            event_queue=event_queue,  # Pass event queue for streaming
                            clients=clients
                        )
                        
                        # Emit telemetry: stream started
                        try:
                            await telemetry.capture(
                                "completion_stream_started",
                                {
                                    "report_id": str(report.id),
                                    "system_completion_id": str(system_completion.id),
                                    "model_id": model.model_id,
                                    "has_widget": bool(widget_obj is not None),
                                },
                                user_id=current_user.id,
                                org_id=organization.id,
                            )
                        except Exception:
                            pass

                        # Run agent execution
                        await agent.main_execution()
                        
                        # Send completion finished event
                        finished_event = SSEEvent(
                            event="completion.finished",
                            completion_id=str(system_completion.id),
                            data={"status": "success"}
                        )
                        await event_queue.put(finished_event)

                        # Emit telemetry: stream completed
                        try:
                            await telemetry.capture(
                                "completion_stream_completed",
                                {
                                    "report_id": str(report.id),
                                    "system_completion_id": str(system_completion.id),
                                },
                                user_id=current_user.id,
                                org_id=organization.id,
                            )
                        except Exception:
                            pass
                        
                    except Exception as e:
                        logging.error(f"Agent streaming execution failed: {e}")
                        # Send error event
                        error_event = SSEEvent(
                            event="completion.error",
                            completion_id=str(system_completion.id),
                            data={
                                "error": str(e),
                                "error_type": type(e).__name__
                            }
                        )
                        await event_queue.put(error_event)

                        # Emit telemetry: stream failed
                        try:
                            await telemetry.capture(
                                "completion_stream_failed",
                                {
                                    "report_id": str(report.id),
                                    "system_completion_id": str(system_completion.id),
                                    "error_type": type(e).__name__,
                                },
                                user_id=current_user.id,
                                org_id=organization.id,
                            )
                        except Exception:
                            pass
                        
                        # Update completion status in database
                        try:
                            await session.execute(
                                update(Completion)
                                .where(Completion.id == system_completion.id)
                                .values(status='error', completion={'content': f"Agent failed: {str(e)}", "error": True})
                            )
                            await session.commit()
                        except Exception:
                            pass
                    finally:
                        # Mark queue as finished
                        event_queue.finish()

            # Start agent execution in background
            asyncio.create_task(run_agent_with_streaming())

            # Stream events
            async def completion_stream_generator():
                """Generate SSE-formatted events for streaming completion."""
                
                # Send initial event
                start_event = SSEEvent(
                    event="completion.started",
                    completion_id=str(completion.id),
                    data={
                        "system_completion_id": str(system_completion.id),
                        "user_prompt": completion_data.prompt.content,
                    }
                )
                yield format_sse_event(start_event)
                
                # Stream agent events
                async for event in event_queue.get_events():
                    yield format_sse_event(event)
                
                # Send completion event
                finish_event = SSEEvent(
                    event="completion.finished",
                    completion_id=str(completion.id),
                    data={
                        "system_completion_id": str(system_completion.id),
                    }
                )
                yield format_sse_event(finish_event)
                yield "data: [DONE]\n\n"

            # Return streaming response
            return StreamingResponse(
                completion_stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",  # Disable nginx buffering
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                }
            )

        except HTTPException as he:
            # Log the error and re-raise HTTP exceptions
            logging.error(f"HTTP Exception in create_completion_stream: {str(he)}")
            raise he
        except Exception as e:
            # Log and convert unexpected errors to HTTP exceptions
            logging.error(f"Unexpected error in create_completion_stream: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected error: {str(e)}"
            )
    
    async def _get_response_completions(self, db: AsyncSession, head_completion: Completion, current_user: User, organization: Organization):
        response_completions = await db.execute(
            select(Completion)
            .where(Completion.parent_id == head_completion.id)
            .where(Completion.report_id == head_completion.report_id)
            .order_by(Completion.created_at.asc())
        )
        response_completions = response_completions.scalars().all()
        return response_completions
    
    async def update_completion_sigkill(self, db: AsyncSession, completion_id: str):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        completion.sigkill = datetime.now()
        completion.status = 'stopped'
        
        # Also update all in_progress completion blocks to stopped
        from app.models.completion_block import CompletionBlock
        blocks_result = await db.execute(
            select(CompletionBlock).where(
                CompletionBlock.completion_id == completion_id,
                CompletionBlock.status == 'in_progress'
            )
        )
        blocks = blocks_result.scalars().all()
        
        for block in blocks:
            block.status = 'stopped'
            if not block.completed_at:
                block.completed_at = completion.sigkill
            db.add(block)
        
        await db.commit()
        await db.refresh(completion)

        return completion
