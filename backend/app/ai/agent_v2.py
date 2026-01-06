import asyncio
import json
import logging
from typing import Dict, Optional
from pydantic import ValidationError

logger = logging.getLogger(__name__)

from app.ai.agents.planner import PlannerV2
from app.ai.context import ContextHub, ContextBuildSpec
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.registry import ToolRegistry, ToolCatalogFilter
from app.schemas.ai.planner import PlannerInput, ToolDescriptor
from app.schemas.sse_schema import SSEEvent
from app.serializers.completion_v2 import serialize_block_v2
from app.schemas.completion_v2_schema import ArtifactChangeSchema
from app.streaming.text_streamer import PlanningTextStreamer
from app.streaming.completion_stream import CompletionEventQueue
from app.websocket_manager import websocket_manager
from app.ai.runner.tool_runner import ToolRunner
from app.ai.runner.policies import RetryPolicy, TimeoutPolicy
from app.project_manager import ProjectManager
from app.models.step import Step
from app.models.widget import Widget
from app.models.completion import Completion
from app.models.report import Report
from app.ai.agents.reporter.reporter import Reporter
from sqlalchemy import select, func
from app.models.tool_execution import ToolExecution
from app.models.agent_execution import AgentExecution
from app.ai.agents.judge.judge import Judge
from app.ai.agents.suggest_instructions import SuggestInstructions, InstructionTriggerEvaluator
from app.settings.database import create_async_session_factory
from app.dependencies import async_session_maker
from app.core.telemetry import telemetry
from app.ai.utils.token_counter import count_tokens
from app.services.instruction_usage_service import InstructionUsageService

INDEX_LIMIT = 1000  # Number of tables to include in the index


class AgentV2:
    """Enhanced orchestrator with intelligent research/action flow."""

    def __init__(self, db=None, organization=None, organization_settings=None, report=None,
                 model=None, small_model=None, mode=None, messages=None, head_completion=None, system_completion=None, widget=None, step=None, event_queue=None, clients=None, build_id=None):
        self.db = db
        self.build_id = build_id
        self.organization = organization
        self.organization_settings = organization_settings

        # Safe retrieval of configuration values with null checks
        # Prevent AttributeError if get_config() returns None
        if organization_settings:
            top_k_schema_config = organization_settings.get_config("top_k_schema")
            self.top_k_schema = top_k_schema_config.value if top_k_schema_config else 10

            top_k_metadata_config = organization_settings.get_config("top_k_metadata_resources")
            self.top_k_metadata_resources = top_k_metadata_config.value if top_k_metadata_config else 5

            # Safe access to nested config dictionary
            self.ai_analyst_name = (
                organization_settings.config.get('general', {}).get('ai_analyst_name', "AI Analyst")
                if hasattr(organization_settings, 'config') and organization_settings.config
                else "AI Analyst"
            )
        else:
            # Default values when organization_settings is None
            self.top_k_schema = 10
            self.top_k_metadata_resources = 5
            self.ai_analyst_name = "AI Analyst"

        self.mode = mode

        self.report = report
        self.report_type = getattr(report, 'report_type', 'regular')
        self.model = model
        self.small_model = small_model
        self.head_completion = head_completion
        self.system_completion = system_completion
        self.widget = widget
        self.step = step

        # Initialize data sources and clients (mirror agent.py pattern)
        if report:
            # Handle case where data_sources or files might be None
            self.data_sources = getattr(report, 'data_sources', []) or []
            self.clients = clients
            self.files = getattr(report, 'files', []) or []
        else:
            self.data_sources = []
            self.clients = {}
            self.files = []

        self.sigkill_event = asyncio.Event()
        websocket_manager.add_handler(self._handle_completion_update)
        
        # SSE event queue for streaming
        self.event_queue = event_queue
        
        # Agent execution tracking
        self.project_manager = ProjectManager()
        self.current_execution = None
        
        # Widget/step state management
        self.current_widget = None
        self.current_step = None
        self.current_step_id = None
        self.current_widget_title = None  # Store widget title for progressive creation

        self.current_query = None

        # create_dashboard streaming state (in-memory, no layout persistence)
        self._dashboard_blocks: list[dict] = []
        self._dashboard_block_sigs: set[str] = set()

        # Streaming text state per block_id
        self._block_text_cache: dict[str, dict[str, str]] = {}

        # Initialize ContextHub for centralized context management
        self.context_hub = ContextHub(
            db=self.db,
            organization=self.organization,
            report=self.report,
            data_sources=self.data_sources,
            user=getattr(self.head_completion, 'user', None) if self.head_completion else None,
            head_completion=self.head_completion,
            widget=self.widget,
            organization_settings=self.organization_settings,
            build_id=build_id
        )
        # Enhanced registry with metadata-driven filtering
        self.registry = ToolRegistry()
        
        # Start with all available tools for the planner to see
        all_catalog_dicts = self.registry.get_catalog_for_plan_type("action", self.organization)
        all_catalog_dicts.extend(self.registry.get_catalog_for_plan_type("research", self.organization))
        
        # Remove duplicates (for tools with category="both")
        seen_tools = set()
        unique_catalog = []
        for tool in all_catalog_dicts:
            if tool['name'] not in seen_tools:
                unique_catalog.append(tool)
                seen_tools.add(tool['name'])
        
        tool_catalog = [ToolDescriptor(**tool) for tool in unique_catalog]
        self.planner = PlannerV2(
            model=self.model,
            tool_catalog=tool_catalog,
            usage_session_maker=async_session_maker,
        )
        
        # Tool runner with enhanced policies
        self.tool_runner = ToolRunner(
            retry=RetryPolicy(max_attempts=2, backoff_ms=500, backoff_multiplier=2.0, jitter_ms=200),
            timeout=TimeoutPolicy(start_timeout_s=5, idle_timeout_s=30, hard_timeout_s=120),
        )
        
        # Initialize Reporter for title generation
        self.reporter = Reporter(model=self.small_model, usage_session_maker=async_session_maker)
        # Initialize Judge using ContextHub's instruction builder
        self.judge = Judge(
            model=self.small_model,
            organization_settings=self.organization_settings,
            instruction_context_builder=self.context_hub.instruction_builder,
        )

        # Initialize SuggestInstructions agent for post-analysis suggestions
        self.suggest_instructions = SuggestInstructions(model=self.small_model)

    async def estimate_prompt_tokens(self) -> dict:
        """Approximate the total planner prompt tokens without executing tools."""
        try:
            await self.context_hub.prime_static()
            await self.context_hub.refresh_warm()
            try:
                await self.context_hub.build_context()
            except Exception:
                pass
            prompt_text = await self._build_planner_prompt_text()
            prompt_tokens = count_tokens(prompt_text, getattr(self.model, "model_id", None))

            model_limit = getattr(self.model, "context_window_tokens", None)
            remaining_tokens = None
            if model_limit is not None:
                remaining_tokens = max(model_limit - prompt_tokens, 0)

            return {
                "prompt_tokens": prompt_tokens,
                "model_limit": model_limit,
                "remaining_tokens": remaining_tokens,
            }
        finally:
            try:
                websocket_manager.remove_handler(self._handle_completion_update)
            except Exception:
                pass

    async def _run_early_scoring_background(self, planner_input: PlannerInput):
        """Run instructions/context scoring in a fresh DB session to avoid concurrency conflicts."""
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    # Use a new Judge instance (stateless) and score from the same planner input
                    # Cache get_config() result to avoid redundant calls and prevent race conditions
                    enable_llm_judgement_config = (
                        self.organization_settings.get_config("enable_llm_judgement")
                        if self.organization_settings else None
                    )
                    enable_llm_judgement = (
                        enable_llm_judgement_config.value
                        if enable_llm_judgement_config else False
                    )

                    if enable_llm_judgement and self.report_type == 'regular':
                        judge = Judge(model=self.model, organization_settings=self.organization_settings)
                        instructions_score, context_score = await judge.score_instructions_and_context_from_planner_input(planner_input)
                    else:
                        instructions_score = 3
                        context_score = 3
                    # Re-fetch completion to avoid using objects from another session
                    completion = await session.get(Completion, str(self.head_completion.id))
                    if completion is not None:
                        await self.project_manager.update_completion_scores(session, completion, instructions_score, context_score)
                except Exception:
                    pass
        except Exception as e:

            pass

    async def _run_late_scoring_background(self, messages_context: str, observation_data: dict):
        """Run response scoring in a fresh DB session to avoid concurrency conflicts."""
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    # Cache get_config() result to avoid redundant calls and prevent race conditions
                    enable_llm_judgement_config = (
                        self.organization_settings.get_config("enable_llm_judgement")
                        if self.organization_settings else None
                    )
                    enable_llm_judgement = (
                        enable_llm_judgement_config.value
                        if enable_llm_judgement_config else False
                    )

                    if enable_llm_judgement and self.report_type == 'regular':
                        judge = Judge(model=self.model, organization_settings=self.organization_settings)
                        # Safe access to nested prompt dictionary with proper null checks
                        original_prompt = ""
                        if self.head_completion and hasattr(self.head_completion, "prompt") and self.head_completion.prompt:
                            original_prompt = self.head_completion.prompt.get("content", "")

                        response_score = await judge.score_response_quality(original_prompt, messages_context, observation_data=observation_data)
                    else:
                        response_score = 3
                    completion = await session.get(Completion, str(self.head_completion.id))
                    if completion is not None:
                        await self.project_manager.update_completion_response_score(session, completion, response_score)
                except Exception:
                    pass
        except Exception as e:
            pass

    async def _stream_suggestions_inline(self, prev_tool_name: Optional[str], conditions: list):
        """Stream instruction suggestions inline before the SSE stream closes.
        
        This runs in the main execution flow using the existing DB session,
        ensuring SSE events reach the frontend before the stream ends.
        """
        try:
            # Use existing context view
            view_for_suggest = self.context_hub.get_view()

            try:
                seq_si = await self.project_manager.next_seq(self.db, self.current_execution)
                await self._emit_sse_event(SSEEvent(
                    event="instructions.suggest.started",
                    completion_id=str(self.system_completion.id),
                    agent_execution_id=str(self.current_execution.id),
                    seq=seq_si,
                    data={}
                ))
            except Exception:
                pass

            try:
                if self.suggest_instructions is not None and self.report_type == 'regular':
                    drafts = []
                    # Format trigger reason from conditions for persistence
                    trigger_reason = "; ".join(c.get("name", "") for c in conditions) if conditions else ""
                    
                    # === Build System Integration ===
                    # Create a single build for all AI-suggested instructions in this execution
                    ai_build = None
                    try:
                        from app.services.build_service import BuildService
                        build_service = BuildService()
                        ai_build = await build_service.get_or_create_draft_build(
                            self.db,
                            str(self.organization.id),
                            source='ai',
                            user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None
                        )
                        logger.debug(f"Created AI build {ai_build.id} for agent execution {self.current_execution.id}")
                    except Exception as build_error:
                        logger.warning(f"Failed to create AI build: {build_error}")
                    
                    async for draft in self.suggest_instructions.stream_suggestions(
                        context_view=view_for_suggest, 
                        context_hub=self.context_hub, 
                        conditions=conditions
                    ):
                        # Persist immediately and stream back full instruction object
                        inst = await self.project_manager.create_instruction_from_draft(
                            self.db,
                            self.organization,
                            text=draft.get("text", ""),
                            category=draft.get("category", "general"),
                            agent_execution_id=str(self.current_execution.id),
                            trigger_reason=trigger_reason,
                            ai_source="completion",
                            user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                            build=ai_build  # Pass build for batching
                        )
                        # Append a minimal client payload
                        draft_payload = {
                            "id": str(inst.id),
                            "text": inst.text,
                            "category": inst.category,
                            "status": inst.status,
                            "private_status": inst.private_status,
                            "global_status": inst.global_status,
                            "is_seen": inst.is_seen,
                            "can_user_toggle": inst.can_user_toggle,
                            "user_id": inst.user_id,
                            "organization_id": str(inst.organization_id),
                            "agent_execution_id": str(inst.agent_execution_id) if getattr(inst, 'agent_execution_id', None) else None,
                            "trigger_reason": inst.trigger_reason,
                            "created_at": inst.created_at.isoformat() if getattr(inst, 'created_at', None) else None,
                            "updated_at": inst.updated_at.isoformat() if getattr(inst, 'updated_at', None) else None,
                            "ai_source": getattr(inst, 'ai_source', None),
                        }
                        drafts.append(draft_payload)

                        try:
                            seq_si_p = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(
                                event="instructions.suggest.partial",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=seq_si_p,
                                data={"instruction": draft_payload}
                            ))
                        except Exception:
                            pass
                    
                    # === Finalize Build ===
                    # Submit the AI build for admin approval (don't auto-publish)
                    if ai_build and len(drafts) > 0:
                        try:
                            from app.services.build_service import BuildService
                            build_service = BuildService()
                            await build_service.submit_build(self.db, ai_build.id)
                            # AI-created builds require admin review - don't auto-approve or promote
                            logger.info(f"Submitted AI build {ai_build.id} for approval with {len(drafts)} instructions")
                        except Exception as finalize_error:
                            logger.warning(f"Failed to submit AI build for approval: {finalize_error}")

                    try:
                        seq_si_f = await self.project_manager.next_seq(self.db, self.current_execution)
                        await self._emit_sse_event(SSEEvent(
                            event="instructions.suggest.finished",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq_si_f,
                            data={"instructions": drafts}
                        ))
                    except Exception:
                        pass
            except Exception:
                try:
                    seq_si_e = await self.project_manager.next_seq(self.db, self.current_execution)
                    await self._emit_sse_event(SSEEvent(
                        event="instructions.suggest.finished",
                        completion_id=str(self.system_completion.id),
                        agent_execution_id=str(self.current_execution.id),
                        seq=seq_si_e,
                        data={"instructions": []}
                    ))
                except Exception:
                    pass
        except Exception:
            # Best-effort; don't fail on suggestion errors
            pass

    async def _generate_title_background(self, messages_context: str, plan_info: list):
        """Generate report title in background after completion.finished is sent."""
        import logging
        logger = logging.getLogger(__name__)
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    title = await self.reporter.generate_report_title(messages_context, plan_info)
                    if not title or not title.strip():
                        logger.warning("Title generation returned empty result")
                        return
                    # Re-fetch report using select query (more reliable than session.get with UUID)
                    stmt = select(Report).where(Report.id == self.report.id)
                    result = await session.execute(stmt)
                    report = result.scalar_one_or_none()
                    if report:
                        await self.project_manager.update_report_title(session, report, title)
                        logger.info(f"Report title updated to: {title}")
                    else:
                        logger.warning(f"Report not found for title update: {self.report.id}")
                except Exception as e:
                    logger.error(f"Failed to generate/update report title: {e}")
        except Exception as e:
            logger.error(f"Failed to create session for title generation: {e}")

    def _build_slim_context_snapshot(self, view, top_k_schema: int = 10) -> dict:
        """
        Build a slim context snapshot that only includes usage tracking data.
        
        Excludes full schemas and instructions to avoid redundant storage.
        Only saves what was actually sent to the LLM.
        """
        # Start with full view but we'll replace large sections
        data = view.model_dump()
        
        try:
            # Replace full schemas with usage tracking only
            if view.static.schemas:
                schemas_usage = view.static.schemas.get_usage_snapshot(top_k_per_ds=top_k_schema)
                data["schemas_usage"] = schemas_usage.model_dump()
                # Remove full schemas to save space
                if "static" in data and "schemas" in data["static"]:
                    data["static"]["schemas"] = None
            
            # Replace full instructions with usage tracking only
            if view.static.instructions and view.static.instructions.items:
                data["instructions_usage"] = [
                    item.model_dump() for item in view.static.instructions.items
                ]
                # Remove full instructions to save space
                if "static" in data and "instructions" in data["static"]:
                    data["static"]["instructions"] = None
        except Exception:
            pass  # Usage tracking is optional, don't fail if it errors
        
        return data

    async def _save_context_snapshot_background(self, kind: str, context_view_json: dict, prompt_text: str = ""):
        """Save context snapshot in background to avoid blocking main execution flow."""
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    # Re-fetch agent execution in this session
                    agent_execution = await session.get(type(self.current_execution), self.current_execution.id)
                    if agent_execution:
                        await self.project_manager.save_context_snapshot(
                            session,
                            agent_execution=agent_execution,
                            kind=kind,
                            context_view_json=context_view_json,
                            prompt_text=prompt_text,
                        )
                except Exception:
                    pass
        except Exception:
            pass

    async def _record_instruction_usage_background(self, instruction_items: list):
        """Record instruction usage events in background to avoid blocking main execution flow."""
        if not instruction_items:
            return
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    service = InstructionUsageService()
                    items_data = []
                    for item in instruction_items:
                        # Handle both Pydantic models and dicts
                        if hasattr(item, 'model_dump'):
                            item_dict = item.model_dump()
                        elif hasattr(item, 'dict'):
                            item_dict = item.dict()
                        elif isinstance(item, dict):
                            item_dict = item
                        else:
                            continue
                        items_data.append(item_dict)
                    
                    if items_data:
                        user_id = str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None
                        await service.record_batch_usage(
                            db=session,
                            org_id=str(self.organization.id),
                            report_id=str(self.report.id) if self.report else None,
                            user_id=user_id,
                            items=items_data,
                            user_role=None,  # Role not easily accessible here
                        )
                except Exception:
                    pass
        except Exception:
            pass

    async def _handle_completion_update(self, message: str):
        # Mirror existing sigkill behavior
        try:
            import json
            data = json.loads(message)
            if (
                data.get("event") == "update_completion"
                and data.get("completion_id") == str(self.system_completion.id)
                and data.get("sigkill") is not None
            ):
                self.sigkill_event.set()
        except Exception:
            pass

    async def _persist_partial_decision_text(self, reasoning_text: str | None, content_text: str | None):
        """Persist partial reasoning/content into the current decision block for resilience on stop."""
        try:
            if not self.current_execution or not self.system_completion:
                return
            # Fetch latest decision block and update fields if present
            from sqlalchemy import select
            from app.models.completion_block import CompletionBlock
            stmt = select(CompletionBlock).where(
                CompletionBlock.agent_execution_id == self.current_execution.id
            ).order_by(CompletionBlock.block_index.desc())
            block = (await self.db.execute(stmt)).scalar_one_or_none()
            if not block:
                return
            updated = False
            if content_text is not None and content_text.strip():
                block.content = content_text
                updated = True
            if reasoning_text is not None and reasoning_text.strip():
                block.reasoning = reasoning_text
                updated = True
            if updated:
                self.db.add(block)
                await self.db.commit()
        except Exception:
            # Best-effort; ignore persistence failures
            pass

    async def _capture_telemetry_background(self, event_name: str, properties: dict):
        """Capture telemetry in background to avoid blocking main execution."""
        try:
            await telemetry.capture(
                event_name,
                properties,
                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                org_id=str(self.organization.id) if self.organization else None,
            )
        except Exception:
            pass

    async def _update_context_token_metadata_background(self, view):
        """Update context token metadata in background."""
        try:
            await self._update_context_token_metadata(view)
        except Exception:
            pass

    async def main_execution(self):
        try:
            # Start agent execution tracking
            self.current_execution = await self.project_manager.start_agent_execution(
                self.db,
                completion_id=str(self.system_completion.id),
                organization_id=str(self.organization.id),
                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                report_id=str(self.report.id) if self.report else None,
                build_id=self.build_id,
            )

            # Telemetry in background (non-blocking)
            asyncio.create_task(self._capture_telemetry_background(
                "agent_execution_started",
                {
                    "agent_execution_id": str(self.current_execution.id),
                    "report_id": str(self.report.id) if self.report else None,
                    "model_id": self.model.model_id if self.model else None,
                },
            ))

            # Extract user prompt early for intelligent instruction search
            prompt_text = self.head_completion.prompt.get("content", "") if self.head_completion.prompt else ""
            
            # Prime static and refresh warm in parallel for faster startup
            # Pass prompt_text to enable intelligent instruction search
            await asyncio.gather(
                self.context_hub.prime_static(query=prompt_text),
                self.context_hub.refresh_warm(),
            )
            view = self.context_hub.get_view()
            # Token metadata update in background (non-blocking)
            asyncio.create_task(self._update_context_token_metadata_background(view))
            
            # Record instruction usage in background (non-blocking)
            if view.static.instructions and view.static.instructions.items:
                asyncio.create_task(self._record_instruction_usage_background(view.static.instructions.items))
            
            # Build slim context snapshot with only usage tracking (excludes full schemas/instructions)
            context_view_data = self._build_slim_context_snapshot(view, top_k_schema=self.top_k_schema)
            
            asyncio.create_task(self._save_context_snapshot_background(
                kind="initial",
                context_view_json=context_view_data,
                prompt_text=prompt_text,
            ))
            
            # Use cached schemas from prime_static() - no duplicate build
            schemas_ctx = view.static.schemas
            try:
                schemas_excerpt = schemas_ctx.render_combined(top_k_per_ds=self.top_k_schema, index_limit=INDEX_LIMIT) if schemas_ctx else ""
            except Exception:
                schemas_excerpt = schemas_ctx.render() if schemas_ctx else ""
            
            # Use cached resources from prime_static() - no duplicate build
            resources_ctx = view.static.resources
            try:
                resources_combined = resources_ctx.render_combined(top_k_per_repo=self.top_k_metadata_resources, index_limit=INDEX_LIMIT) if resources_ctx else ""
            except Exception:
                resources_combined = resources_ctx.render() if resources_ctx else ""
            
            # History summary based on observation context only
            history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

            # Compute previous tool call before this user message (DB-based, robust)
            prev_tool_name_before_last_user = None
            try:
                report_id = str(self.report.id) if self.report else None
                completion_created_at = getattr(self.system_completion, "created_at", None)
                if report_id:
                    stmt = (
                        select(ToolExecution.tool_name, ToolExecution.started_at)
                        .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                        .where(AgentExecution.report_id == report_id)
                    )
                    if completion_created_at is not None:
                        # Only consider tool executions strictly before this system completion
                        stmt = stmt.where(
                            (ToolExecution.started_at == None) | (ToolExecution.started_at < completion_created_at)
                        )
                    stmt = stmt.order_by(ToolExecution.started_at.desc()).limit(1)
                    res = await self.db.execute(stmt)
                    row = res.first()
                    if row is not None:
                        prev_tool_name_before_last_user = row[0]
            except Exception:
                prev_tool_name_before_last_user = None

            # Use cached instructions from prime_static() - no duplicate build
            inst_section = view.static.instructions
            instructions = inst_section.render() if inst_section else ""

            observation: Optional[dict] = None
            step_limit = 10

            current_plan_decision = None
            invalid_retry_count = 0
            max_invalid_retries = 2
            
            # Circuit breaker for repeated tool failures
            failed_tool_count = {}
            max_tool_failures = 3
            
            # Circuit breaker for repeated successful actions (infinite success loop)
            successful_tool_actions = []
            max_repeated_successes = 2
            
            # Track whether completion.finished has been emitted to avoid duplicates
            completion_finished_emitted = False
            
            # Early scoring will be launched as a background task using an isolated session

            for loop_index in range(step_limit):
                if self.sigkill_event.is_set():
                    break

                # Refresh warm context (skip on first loop - already done above)
                if loop_index > 0:
                    await self.context_hub.refresh_warm()
                    view = self.context_hub.get_view()
                    await self._update_context_token_metadata(view)
                
                # Save pre-tool context snapshot in background (skip first loop - initial snapshot already saved)
                if loop_index > 0:
                    pre_tool_view_data = self._build_slim_context_snapshot(view, top_k_schema=self.top_k_schema)
                    asyncio.create_task(self._save_context_snapshot_background(
                        kind="pre_tool",
                        context_view_json=pre_tool_view_data,
                    ))

                # Build enhanced planner input with validation and retry on failure
                try:
                    # Get messages context for detailed conversation history
                    # On first loop, use cached messages from refresh_warm(); rebuild on subsequent loops
                    if loop_index == 0 and view.warm.messages:
                        messages_section = view.warm.messages
                    else:
                        messages_section = await self.context_hub.message_builder.build(max_messages=20)
                    messages_context = messages_section.render() if messages_section else ""
                    # Use cached resources from prime_static() - static, no need to rebuild
                    resources_section = view.static.resources
                    resources_context = resources_section.render() if resources_section else ""
                    # Smaller combined excerpt to control tokens per-iteration
                    try:
                        resources_combined_small = resources_section.render_combined(top_k_per_repo=10, index_limit=200) if resources_section else ""
                    except Exception:
                        resources_combined_small = resources_context
                    # Files context (uploaded files schemas/metadata) - use cached
                    files_context = view.static.files.render() if getattr(view.static, "files", None) else ""
                    # Mentions context (current user turn mentions)
                    mentions_context = (view.warm.mentions.render() if getattr(view.warm, "mentions", None) else "")
                    # Entities context (catalog entities relevant to this turn)
                    entities_context = (view.warm.entities.render() if getattr(view.warm, "entities", None) else "")

                    planner_input = PlannerInput(
                        organization_name=self.organization.name,
                        organization_ai_analyst_name=self.ai_analyst_name,
                        instructions=instructions,
                        user_message=self.head_completion.prompt["content"],
                        schemas_excerpt=None,
                        schemas_combined=schemas_excerpt,
                        schemas_names_index=None,
                        files_context=files_context,
                        mentions_context=mentions_context,
                        entities_context=entities_context,
                        history_summary=history_summary,
                        messages_context=messages_context,
                        resources_context=resources_context,
                        resources_combined=(resources_combined_small if 'resources_combined' not in locals() else resources_combined),
                        last_observation=observation,
                        past_observations=self.context_hub.observation_builder.tool_observations,
                        external_platform=getattr(self.head_completion, "external_platform", None),
                        tool_catalog=self.planner.tool_catalog,
                        mode=self.mode
                    )
                    # Kick off early scoring in background without blocking the loop (isolated DB session)
                    asyncio.create_task(self._run_early_scoring_background(planner_input))
                except ValidationError as ve:
                    if invalid_retry_count >= max_invalid_retries:
                        # Too many retries, exit loop
                        break
                    observation = {
                        "summary": "Planner input invalid; retrying",
                        "error": {"code": "input_validation_error", "message": str(ve)},
                    }
                    invalid_retry_count += 1
                    try:
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        await self._emit_sse_event(SSEEvent(
                            event="planner.retry",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq,
                            data={
                                "reason": "input_validation_error",
                                "attempt": invalid_retry_count,
                            }
                        ))
                    except Exception:
                        pass
                    # Retry next loop iteration
                    continue

                # PLAN: pre-create a skeleton planning block so tokens can stream immediately
                analysis_done = False
                current_block_id = None
                token_accumulator = {"reasoning": "", "content": ""}
                plan_streamer = None
                # Stable sequence for the entire planner decision lifespan
                decision_seq = None

                # Pre-create a placeholder PlanDecision and corresponding block for this loop
                try:
                    pre_seq = await self.project_manager.next_seq(self.db, self.current_execution)
                    # Default to action plan type for skeleton; will be updated on real decision
                    pre_pd = await self.project_manager.save_plan_decision(
                        self.db,
                        agent_execution=self.current_execution,
                        seq=pre_seq,
                        loop_index=loop_index,
                        plan_type="action",
                        analysis_complete=False,
                        reasoning=None,
                        assistant=None,
                        final_answer=None,
                        action_name=None,
                        action_args_json=None,
                        metrics_json=None,
                        context_snapshot_id=None,
                    )
                    pre_block = await self.project_manager.upsert_block_for_decision(
                        self.db, self.system_completion, self.current_execution, pre_pd
                    )
                    current_block_id = str(pre_block.id)
                    # Pin the decision sequence so partial/final frames upsert the same row
                    decision_seq = pre_seq
                    # Emit initial block snapshot
                    try:
                        block_schema = await serialize_block_v2(self.db, pre_block)
                        await self._emit_sse_event(SSEEvent(
                            event="block.upsert",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=pre_seq,
                            data={"block": block_schema.model_dump()}
                        ))
                    except Exception:
                        pass
                except Exception:
                    # If pre-create fails, we will fallback to buffering tokens until a block exists
                    current_block_id = None
                else:
                    # Initialize throttled text streamer for this planning block
                    async def _next_seq():
                        return await self.project_manager.next_seq(self.db, self.current_execution)
                    plan_streamer = PlanningTextStreamer(
                        emit=self._emit_sse_event,
                        seq_fn=_next_seq,
                        completion_id=str(self.system_completion.id),
                        agent_execution_id=str(self.current_execution.id),
                        block_id=current_block_id,
                    )
                
                async for evt in self.planner.execute(planner_input, self.sigkill_event):
                    if self.sigkill_event.is_set():
                        break

                    # Handle typed events
                    if evt.type == "planner.tokens":
                        # Do not forward raw JSON tokens; deltas will be emitted from decision partials
                        continue
                        
                    elif evt.type == "planner.decision.partial":
                        decision = evt.data  # Already validated PlannerDecision from planner_v2
                        
                        # Get next sequence number for SSE event ordering (in-memory, no DB)
                        event_seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        
                        # Save partial decision (Pydantic model) using stable decision_seq
                        if decision_seq is None:
                            decision_seq = event_seq
                        current_plan_decision = await self.project_manager.save_plan_decision_from_model(
                            self.db,
                            agent_execution=self.current_execution,
                            seq=decision_seq,
                            loop_index=loop_index,
                            planner_decision_model=decision,
                        )
                        # Persist partial content/reasoning to the decision block so a stop retains text
                        try:
                            await self.project_manager.upsert_block_for_decision(
                                self.db, self.system_completion, self.current_execution, current_plan_decision
                            )
                        except Exception:
                            pass
                        # Ensure a block exists if pre-creation failed; emit one snapshot once
                        if current_block_id is None:
                            try:
                                block = await self.project_manager.upsert_block_for_decision(self.db, self.system_completion, self.current_execution, current_plan_decision)
                                await self.project_manager.rebuild_completion_from_blocks(self.db, self.system_completion, self.current_execution)
                                current_block_id = str(block.id)
                                try:
                                    block_schema = await serialize_block_v2(self.db, block)
                                    await self._emit_sse_event(SSEEvent(
                                        event="block.upsert",
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=event_seq,
                                        data={"block": block_schema.model_dump()}
                                    ))
                                except Exception:
                                    pass
                                # Initialize or update streamer with block id
                                if plan_streamer is None:
                                    async def _next_seq():
                                        return await self.project_manager.next_seq(self.db, self.current_execution)
                                    plan_streamer = PlanningTextStreamer(
                                        emit=self._emit_sse_event,
                                        seq_fn=_next_seq,
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        block_id=current_block_id,
                                    )
                                else:
                                    plan_streamer.set_block(current_block_id)
                            except Exception:
                                pass

                        # Emit incremental, throttled token deltas for reasoning/content
                        try:
                            new_reasoning = getattr(current_plan_decision, "reasoning", None) or ""
                            new_content = getattr(current_plan_decision, "assistant", None) or ""
                            if plan_streamer:
                                await plan_streamer.update(new_reasoning, new_content)
                        except Exception:
                            pass
                        
                        # Emit SSE event only if there is content in reasoning or assistant
                        reasoning_text = (getattr(decision, "reasoning_message", None) or "").strip()
                        assistant_text = (getattr(decision, "assistant_message", None) or "").strip()
                        if reasoning_text or assistant_text:
                            await self._emit_sse_event(SSEEvent(
                                event="decision.partial",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=event_seq,
                                data={
                                    "plan_type": decision.plan_type,
                                    "reasoning": decision.reasoning_message,
                                    "assistant": decision.assistant_message,
                                    "action": decision.action.model_dump() if decision.action else None,
                                }
                            ))
                    
                    elif evt.type == "planner.decision.final":
                        decision = evt.data  # Already validated PlannerDecision from planner_v2
                        # Track whether analysis is complete
                        analysis_done = bool(getattr(decision, "analysis_complete", False))
                        
                        # Retry flow: invalid planner output
                        if getattr(decision, "error", None):
                            if invalid_retry_count >= max_invalid_retries:
                                # Too many retries, treat as final error
                                analysis_done = True
                                break
                            observation = {
                                "summary": "Planner output invalid; retrying",
                                "error": {
                                    "code": getattr(decision.error, "code", "validation_error"),
                                    "message": getattr(decision.error, "message", "Invalid planner output"),
                                },
                            }
                            invalid_retry_count += 1
                            # Emit retry event
                            try:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="planner.retry",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "reason": "invalid_output",
                                        "attempt": invalid_retry_count,
                                    }
                                ))
                            except Exception:
                                pass
                            # Stop streaming loop; outer loop will attempt again
                            break
                        
                        # Get next sequence number for SSE event ordering (in-memory, no DB)
                        event_seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        
                        # Save final decision (Pydantic model) using stable decision_seq
                        if decision_seq is None:
                            decision_seq = event_seq
                        current_plan_decision = await self.project_manager.save_plan_decision_from_model(
                            self.db,
                            agent_execution=self.current_execution,
                            seq=decision_seq,
                            loop_index=loop_index,
                            planner_decision_model=decision,
                        )
                        # Upsert completion block for decision and rebuild transcript
                        try:
                            block = await self.project_manager.upsert_block_for_decision(self.db, self.system_completion, self.current_execution, current_plan_decision)
                            await self.project_manager.rebuild_completion_from_blocks(self.db, self.system_completion, self.current_execution)
                            
                            # Store block ID for token streaming
                            current_block_id = str(block.id)
                            
                            # Emit a v2-shaped block snapshot
                            try:
                                block_schema = await serialize_block_v2(self.db, block)
                                await self._emit_sse_event(SSEEvent(
                                    event="block.upsert",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=event_seq,
                                    data={"block": block_schema.model_dump()}
                                ))
                                # Finalize field streaming (snapshots + completion markers)
                                try:
                                    if plan_streamer:
                                        await plan_streamer.complete()
                                except Exception:
                                    pass
                            except Exception:
                                pass
                        except Exception:
                            pass
                        
                        # Emit SSE event
                        await self._emit_sse_event(SSEEvent(
                            event="decision.final",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=event_seq,
                            data={
                                "analysis_complete": decision.analysis_complete,
                                "final_answer": decision.final_answer,
                                "metrics": decision.metrics.model_dump() if decision.metrics else None,
                            }
                        ))
                        
                        # IMPORTANT: Check for action FIRST before checking analysis_complete.
                        # The LLM sometimes sets analysis_complete=true when it means "this is the 
                        # final step" rather than "no action needed". If there's an action, execute it.
                        action = decision.action
                        
                        # Only treat analysis_complete as terminal if there's NO action
                        if decision.analysis_complete and not action:
                            # Final answer path (no tool to execute)
                            invalid_retry_count = 0
                            
                            # === IMMEDIATE: Emit completion.finished so UI updates instantly ===
                            # This unblocks thumbs up/debug icons and stop→submit button
                            if self.system_completion and not completion_finished_emitted:
                                await self.project_manager.update_completion_status(
                                    self.db, 
                                    self.system_completion, 
                                    'success'
                                )
                                if self.event_queue:
                                    await self.event_queue.put(SSEEvent(
                                        event="completion.finished",
                                        completion_id=str(self.system_completion.id),
                                        data={"status": "success"}
                                    ))
                                completion_finished_emitted = True
                            
                            # === INLINE: Post-analysis tasks (suggestions) ===
                            # Run suggestions inline so SSE events reach the frontend before stream closes
                            res = await self._should_suggest_instructions(prev_tool_name_before_last_user)
                            should_trigger_suggestions = res.get("decision", False)
                            conditions = res.get("conditions", [])

                            if should_trigger_suggestions:
                                await self._stream_suggestions_inline(
                                    prev_tool_name_before_last_user,
                                    conditions
                                )
                            break
                        # Retry flow: action plan with missing action
                        if (getattr(decision, "plan_type", None) == "action") and not action:
                            if invalid_retry_count >= max_invalid_retries:
                                # Too many retries, exit
                                break
                            observation = {
                                "summary": "Planner chose action plan but returned no tool/action; retrying",
                                "error": {"code": "missing_action", "message": "Choose a tool and arguments"},
                            }
                            invalid_retry_count += 1
                            # Emit retry event
                            try:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="planner.retry",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "reason": "missing_action",
                                        "attempt": invalid_retry_count,
                                    }
                                ))
                            except Exception:
                                pass
                            # End streaming loop so outer loop can retry
                            break
                        if not action:
                            continue

                        tool_name = action.name
                        tool_input = action.arguments

                        # Validate tool availability for chosen plan_type
                        if not self._validate_tool_for_plan_type(tool_name, decision.plan_type):
                            observation = {
                                "summary": f"Tool '{tool_name}' not available for plan_type '{decision.plan_type}'",
                                "error": {"code": "resolve_error", "message": "tool/plan_type mismatch"},
                            }
                            continue  # Continue to next iteration with error observation

                        tool = self.registry.get(tool_name)
                        if not tool:
                            observation = {
                                "summary": f"Tool '{tool_name}' unavailable",
                                "error": {"code": "resolve_error", "message": "not registered"},
                            }
                            continue  # Continue to next iteration with error observation

                        # Reset artifact state for tools that can create/update steps/visualizations
                        try:
                            if tool_name in [
                                "create_widget",
                                "create_data",
                                "create_and_execute_code",
                                "describe_entity",
                            ]:
                                self.current_query = None
                                self.current_step = None
                                self.current_step_id = None
                                self.current_visualization = None
                        except Exception:
                            pass

                        # Start tool execution tracking
                        tool_execution = await self.project_manager.start_tool_execution_from_models(
                            self.db,
                            agent_execution=self.current_execution,
                            plan_decision_id=current_plan_decision.id if current_plan_decision else None,
                            tool_name=tool_name,
                            tool_action=action.type,
                            tool_input_model=tool_input,
                        )
                        # Telemetry: tool started
                        try:
                            await telemetry.capture(
                                "agent_tool_started",
                                {
                                    "agent_execution_id": str(self.current_execution.id),
                                    "tool_name": tool_name,
                                    "tool_action": action.type,
                                },
                                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                                org_id=str(self.organization.id) if self.organization else None,
                            )
                        except Exception:
                            pass
                        
                        # Emit tool start event
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        await self._emit_sse_event(SSEEvent(
                            event="tool.started",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq,
                            data={
                                "tool_name": tool_name,
                                "arguments": tool_input,
                            }
                        ))
                        
                        # Refresh warm context to include the latest planner decision blocks in messages
                        try:
                            await self.context_hub.refresh_warm()
                            view = self.context_hub.get_view()
                        except Exception:
                            pass
                        try:
                            schemas_ctx = await self.context_hub.schema_builder.build(
                                with_stats=True,
                            )
                            schemas_excerpt = schemas_ctx.render_combined(top_k_per_ds=10, index_limit=200)
                        except Exception:
                            schemas_excerpt = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""
                        # Refresh history summary with updated context
                        history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

                        # RUN TOOL with enhanced context tracking
                        runtime_ctx = {
                            "db": self.db,
                            "organization": self.organization,
                            "settings": self.organization_settings,
                            "report": self.report,
                            "head_completion": self.head_completion,
                            "system_completion": self.system_completion,
                            "widget": self.widget,
                            "step": self.step,
                            "current_widget": self.current_widget,
                            "current_query": self.current_query,
                            "current_step": self.current_step,
                            "current_step_id": self.current_step_id,
                            "project_manager": self.project_manager,
                            "model": self.model,
                            "sigkill_event": self.sigkill_event,
                            "observation_context": self.context_hub.observation_builder.to_dict(),
                            "context_view": view,
                            "context_hub": self.context_hub,
                            "ds_clients": self.clients,
                            "excel_files": self.files
                        }

                        # Emit generic output event for tools that stream results (inspect_data, answer_question)
                        if tool_name == "inspect_data":
                            # Ensure streaming stdout is enabled by default for this tool
                            pass

                        async def emit(ev: dict):
                            # Handle streaming side-effects
                            await self._handle_streaming_event(tool_name, ev, tool_input)
                            # Forward events to UI
                            if ev.get("type") in ["tool.progress", "tool.error", "tool.partial", "tool.stdout"]:
                                seq_ev = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event=ev.get("type", "tool.progress"),
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq_ev,
                                    data={
                                        "tool_name": tool_name,
                                        "payload": ev.get("payload", {}),
                                    }
                                ))

                        tool_result = await self.tool_runner.run(tool, tool_input, runtime_ctx, emit)

                        # Extract observation and output from tool result
                        if isinstance(tool_result, dict) and "observation" in tool_result:
                            observation = tool_result["observation"]
                            tool_output = tool_result.get("output")
                        else:
                            observation = tool_result
                            tool_output = None

                        # Handle tool outputs and manage widget/step state
                        await self._handle_tool_output(tool_name, tool_input, observation, tool_output)

                        # Circuit breaker: track repeated tool failures
                        if observation and observation.get("error"):
                            failed_tool_count[tool_name] = failed_tool_count.get(tool_name, 0) + 1
                            if failed_tool_count[tool_name] >= max_tool_failures:
                                analysis_done = True
                                observation.update({
                                    "analysis_complete": True,
                                    "final_answer": f"Unable to complete the task. The {tool_name} tool failed {failed_tool_count[tool_name]} times with errors. Please check the tool configuration or try a different approach."
                                })
                        else:
                            if tool_name in failed_tool_count:
                                del failed_tool_count[tool_name]
                            action_signature = f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"
                            successful_tool_actions.append(action_signature)
                            if len(successful_tool_actions) >= max_repeated_successes:
                                recent_actions = successful_tool_actions[-max_repeated_successes:]
                                if len(set(recent_actions)) == 1:
                                    analysis_done = True
                                    observation.update({
                                        "analysis_complete": True,
                                        "final_answer": f"Task completed successfully. The {tool_name} tool has been executed {max_repeated_successes} times with the same parameters, indicating the goal has been achieved."
                                    })

                        if observation and observation.get("analysis_complete"):
                            analysis_done = True

                            # If tool provides final_answer, update completion and block content
                            final_answer_from_tool = observation.get("final_answer")
                            if final_answer_from_tool and self.system_completion:
                                # Update completion message
                                await self.project_manager.update_message(
                                    self.db, self.system_completion, message=final_answer_from_tool
                                )
                                # Update block content so UI shows it
                                if current_plan_decision:
                                    current_plan_decision.final_answer = final_answer_from_tool
                                    current_plan_decision.analysis_complete = True
                                    try:
                                        block = await self.project_manager.upsert_block_for_decision(
                                            self.db, self.system_completion, self.current_execution, current_plan_decision
                                        )
                                        await self.project_manager.rebuild_completion_from_blocks(
                                            self.db, self.system_completion, self.current_execution
                                        )
                                        # Emit updated block to frontend
                                        if block:
                                            block_schema = await serialize_block_v2(self.db, block)
                                            seq_blk = await self.project_manager.next_seq(self.db, self.current_execution)
                                            await self._emit_sse_event(SSEEvent(
                                                event="block.upsert",
                                                completion_id=str(self.system_completion.id),
                                                agent_execution_id=str(self.current_execution.id),
                                                seq=seq_blk,
                                                data={"block": block_schema.model_dump()}
                                            ))
                                    except Exception:
                                        pass

                            # Emit completion.finished immediately so UI updates
                            if self.system_completion and not completion_finished_emitted:
                                await self.project_manager.update_completion_status(
                                    self.db, 
                                    self.system_completion, 
                                    'success'
                                )
                                if self.event_queue:
                                    await self.event_queue.put(SSEEvent(
                                        event="completion.finished",
                                        completion_id=str(self.system_completion.id),
                                        data={"status": "success"}
                                    ))
                                completion_finished_emitted = True

                        # Extract created objects from observation, with fallback to orchestrator state
                        created_widget_id = None
                        created_step_id = None
                        if observation and "widget_id" in observation:
                            created_widget_id = observation["widget_id"]
                        if observation and "step_id" in observation:
                            created_step_id = observation["step_id"]
                        # Fallback to orchestrator's current_step_id for tools that trigger step creation via progress events
                        if not created_step_id and self.current_step_id:
                            created_step_id = self.current_step_id

                        # Capture post-tool context snapshot
                        await self.context_hub.refresh_warm()
                        # Refresh static sections (schemas with stats) so post_tool snapshot reflects latest table usage
                        try:
                            await self.context_hub.build_context()
                        except Exception:
                            pass
                        post_view = self.context_hub.get_view()
                        await self._update_context_token_metadata(post_view)

                        # Build slim context snapshot with only usage tracking
                        post_tool_view_data = self._build_slim_context_snapshot(post_view, top_k_schema=self.top_k_schema)

                        post_snap = await self.project_manager.save_context_snapshot(
                            self.db,
                            agent_execution=self.current_execution,
                            kind="post_tool",
                            context_view_json=post_tool_view_data,
                        )

                        # Build created_visualization_ids with fallback to orchestrator state
                        created_visualization_ids = (observation.get("created_visualization_ids") if observation else None)
                        if not created_visualization_ids and getattr(self, 'current_visualization', None):
                            created_visualization_ids = [str(self.current_visualization.id)]

                        # Finish tool execution tracking
                        await self.project_manager.finish_tool_execution_from_models(
                            self.db,
                            tool_execution=tool_execution,
                            result_model=tool_output,
                            summary=observation.get("summary", "") if observation else "",
                            created_widget_id=created_widget_id,
                            created_step_id=created_step_id,
                            created_visualization_ids=created_visualization_ids,
                            error_message=observation.get("error", {}).get("message") if observation and observation.get("error") else None,
                            context_snapshot_id=post_snap.id,
                            success=bool(observation and not observation.get("error")),
                        )

                        # Telemetry: tool finished
                        try:
                            await telemetry.capture(
                                "agent_tool_finished",
                                {
                                    "agent_execution_id": str(self.current_execution.id),
                                    "tool_name": tool_name,
                                    "status": "success" if observation and not observation.get("error") else "error",
                                    "duration_ms": getattr(tool_execution, "duration_ms", None),
                                },
                                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                                org_id=str(self.organization.id) if self.organization else None,
                            )
                        except Exception:
                            pass

                        # Upsert completion block for tool and rebuild transcript
                        try:
                            block = await self.project_manager.upsert_block_for_tool(self.db, self.system_completion, self.current_execution, tool_execution)
                            await self.project_manager.rebuild_completion_from_blocks(self.db, self.system_completion, self.current_execution)
                            if block is not None:
                                try:
                                    block_schema = await serialize_block_v2(self.db, block)
                                    seq_blk = await self.project_manager.next_seq(self.db, self.current_execution)
                                    await self._emit_sse_event(SSEEvent(
                                        event="block.upsert",
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=seq_blk,
                                        data={"block": block_schema.model_dump()}
                                    ))
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        # Emit tool.finished with result
                        seq_fin = await self.project_manager.next_seq(self.db, self.current_execution)
                        safe_result_json = None
                        if tool_output is not None:
                            try:
                                safe_result_json = json.loads(json.dumps(tool_output, default=str))
                            except Exception:
                                safe_result_json = {"summary": observation.get("summary", "") if observation else ""}
                        await self._emit_sse_event(SSEEvent(
                            event="tool.finished",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq_fin,
                            data={
                                "tool_name": tool_name,
                                "status": "success" if observation and not observation.get("error") else "error",
                                "result_summary": observation.get("summary", "") if observation else "",
                                # Include query_id for hydration in frontend previews when available
                                "result_json": ({**safe_result_json, "query_id": (str(self.current_query.id) if getattr(self, "current_query", None) else None)} if isinstance(safe_result_json, dict) else safe_result_json),
                                "duration_ms": tool_execution.duration_ms,
                                "created_widget_id": created_widget_id,
                                "created_step_id": created_step_id,
                                "created_visualization_ids": created_visualization_ids,
                            }
                        ))

                        # Track tool observation for history
                        try:
                            meta = self.registry.get_metadata(tool_name)
                            if not meta or getattr(meta, "observation_policy", "on_trigger") != "never":
                                self.context_hub.observation_builder.add_tool_observation(tool_name, tool_input, observation)
                        except Exception:
                            pass

                        # Reset invalid retry counter
                        invalid_retry_count = 0

                        # Refresh for next iteration
                        await self.context_hub.refresh_warm()
                        view = self.context_hub.get_view()
                        schemas_excerpt = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""
                        history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

                        break

                # If planner finalized analysis, stop the outer loop as well
                if analysis_done:
                    break

            # Save final context snapshot (recompute metadata so counts/tokens are up to date)
            await self.context_hub.refresh_warm()
            try:
                await self.context_hub.build_context()
            except Exception:
                pass
            view = self.context_hub.get_view()
            await self._update_context_token_metadata(view)

            # Build slim context snapshot with only usage tracking
            final_view_data = self._build_slim_context_snapshot(view, top_k_schema=self.top_k_schema)

            await self.project_manager.save_context_snapshot(
                self.db,
                agent_execution=self.current_execution,
                kind="final",
                context_view_json=final_view_data,
            )
            
            # Generate report title if this is the first completion (non-blocking)
            try:
                if self.head_completion and self.report:
                    first_completion = await self.db.execute(
                        select(Completion)
                        .filter(Completion.report_id == self.report.id)
                        .order_by(Completion.created_at.asc())
                        .limit(1)
                    )
                    first_completion = first_completion.scalar_one_or_none()
                    
                    if first_completion and self.head_completion.id == first_completion.id:
                        # Generate title in background to not block completion
                        messages_section = await self.context_hub.message_builder.build(max_messages=5)
                        messages_context = messages_section.render()
                        
                        # Extract plan information from current execution
                        plan_info = []
                        if current_plan_decision:
                            if hasattr(current_plan_decision, 'action_name') and current_plan_decision.action_name:
                                plan_info.append({"action": current_plan_decision.action_name})
                        
                        # Run title generation in background
                        asyncio.create_task(self._generate_title_background(messages_context, plan_info))
            except Exception as e:
                # Don't fail the entire execution if title generation fails
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to start title generation: {e}")
            
            # Late scoring (non-blocking): capture context string and observation snapshot, then run in isolated session
            try:
                final_messages_context = await self.context_hub.get_messages_context(max_messages=20)
            except Exception:
                final_messages_context = ""
            observation_snapshot = self.context_hub.observation_builder.to_dict()
            asyncio.create_task(self._run_late_scoring_background(final_messages_context, observation_snapshot))

            # Finish agent execution
            status = 'sigkill' if self.sigkill_event.is_set() else 'success'
            await self.project_manager.finish_agent_execution(
                self.db,
                agent_execution=self.current_execution,
                status=status,
            )
            # Telemetry: agent execution completed
            try:
                await telemetry.capture(
                    "agent_execution_completed",
                    {
                        "agent_execution_id": str(self.current_execution.id),
                        "status": status,
                    },
                    user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                    org_id=str(self.organization.id) if self.organization else None,
                )
            except Exception:
                pass
            
            # Update system completion status and emit event if not already done
            # Success case is typically handled earlier in the analysis_complete block for faster UI response
            if self.system_completion and not completion_finished_emitted:
                completion_status = 'stopped' if self.sigkill_event.is_set() else 'success'
                await self.project_manager.update_completion_status(
                    self.db, 
                    self.system_completion, 
                    completion_status
                )
                
                # Emit completion finished event
                if self.event_queue:
                    finished_event = SSEEvent(
                        event="completion.finished",
                        completion_id=str(self.system_completion.id),
                        data={"status": completion_status}
                    )
                    await self.event_queue.put(finished_event)
                completion_finished_emitted = True
            
        except Exception as e:
            # Handle errors and finish execution with error status
            if self.current_execution:
                error_payload = {"message": str(e), "type": type(e).__name__}
                await self.project_manager.finish_agent_execution(
                    self.db,
                    agent_execution=self.current_execution,
                    status='error',
                    error_json=error_payload,
                )
                # Telemetry: agent execution failed
                try:
                    await telemetry.capture(
                        "agent_execution_failed",
                        {
                            "agent_execution_id": str(self.current_execution.id),
                            "error_type": type(e).__name__,
                        },
                        user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                        org_id=str(self.organization.id) if self.organization else None,
                    )
                except Exception:
                    pass
                # Persist error on completion and latest block for UI
                try:
                    # Update completion record with status and message
                    if self.system_completion:
                        await self.project_manager.update_completion_status(self.db, self.system_completion, 'error')
                        await self.project_manager.update_message(self.db, self.system_completion, message=error_payload.get('message'), reasoning=None)
                    # Mark last block as error with message
                    await self.project_manager.mark_error_on_latest_block(self.db, self.current_execution, error_payload.get('message'))
                except Exception:
                    pass
            
            # Update system completion status on error
            if self.system_completion:
                await self.project_manager.update_completion_status(
                    self.db, 
                    self.system_completion, 
                    'error'
                )
            # Emit a final completion.finished event with error details for UI consumption
            try:
                if self.event_queue:
                    await self.event_queue.put(SSEEvent(
                        event="completion.finished",
                        completion_id=str(self.system_completion.id) if self.system_completion else None,
                        data={
                            "status": "error",
                            "error": error_payload,
                        }
                    ))
            except Exception:
                pass
            raise
        finally:
            # Cleanup
            try:
                websocket_manager.remove_handler(self._handle_completion_update)
            except Exception:
                pass

    async def _build_planner_prompt_text(self, view=None) -> str:
        if view is None:
            view = self.context_hub.get_view()

        instructions_section = await self.context_hub.instruction_builder.build()
        instructions = instructions_section.render()

        history_summary = self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

        try:
            schemas_ctx = await self.context_hub.schema_builder.build(
                with_stats=True,
            )
            schemas_combined = schemas_ctx.render_combined(top_k_per_ds=self.top_k_schema, index_limit=INDEX_LIMIT)
        except Exception:
            schemas_combined = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""

        messages_section = await self.context_hub.message_builder.build(max_messages=20)
        messages_context = messages_section.render()

        resources_section = await self.context_hub.resource_builder.build()
        resources_context = resources_section.render()
        try:
            resources_combined_small = resources_section.render_combined(top_k_per_repo=self.top_k_metadata_resources, index_limit=INDEX_LIMIT)
        except Exception:
            resources_combined_small = resources_context

        files_context = view.static.files.render() if getattr(view.static, "files", None) else ""
        mentions_context = (view.warm.mentions.render() if getattr(view.warm, "mentions", None) else "")
        entities_context = (view.warm.entities.render() if getattr(view.warm, "entities", None) else "")

        user_message = (self.head_completion.prompt or {}).get("content", "")

        planner_input = PlannerInput(
            organization_name=self.organization.name,
            organization_ai_analyst_name=self.ai_analyst_name,
            instructions=instructions,
            user_message=user_message,
            schemas_excerpt=None,
            schemas_combined=schemas_combined,
            schemas_names_index=None,
            files_context=files_context,
            mentions_context=mentions_context,
            entities_context=entities_context,
            history_summary=history_summary,
            messages_context=messages_context,
            resources_context=resources_context,
            resources_combined=resources_combined_small,
            last_observation=None,
            past_observations=self.context_hub.observation_builder.tool_observations,
            external_platform=getattr(self.head_completion, "external_platform", None),
            tool_catalog=self.planner.tool_catalog,
            mode=self.mode,
        )

        return self.planner.prompt_builder.build_prompt(planner_input)

    async def _update_context_token_metadata(self, view=None):
        try:
            prompt_text = await self._build_planner_prompt_text(view=view)
            prompt_tokens = count_tokens(prompt_text, getattr(self.model, "model_id", None))
            metadata = self.context_hub.metadata
            section_sizes = dict(metadata.section_sizes or {})
            section_sizes["_planner_prompt_total"] = prompt_tokens
            metadata.section_sizes = section_sizes
            metadata.total_tokens = prompt_tokens
            if view is not None and isinstance(getattr(view, "meta", None), dict):
                try:
                    view.meta.update(metadata.model_dump())
                except Exception:
                    pass
        except Exception:
            pass

    async def _emit_sse_event(self, event: SSEEvent):
        """Emit SSE event via event queue and optionally websocket."""
        try:
            # Add to streaming queue for new streaming API
            if self.event_queue:
                await self.event_queue.put(event)
            
        except Exception as e:
            print(f"Error emitting SSE event: {e}")

    async def _should_suggest_instructions(self, prev_tool_name_before_last_user: Optional[str]) -> Dict[str, object]:
        """Decide whether to run suggest_instructions based on report history.

        Delegates to InstructionTriggerEvaluator for condition evaluation.
        Returns: {"decision": bool, "conditions": [{"name": str, "hint": str}, ...]}
        """
        try:
            # Get user message for condition evaluation
            user_message = ""
            if self.head_completion and self.head_completion.prompt:
                user_message = self.head_completion.prompt.get("content", "")
            
            evaluator = InstructionTriggerEvaluator(
                db=self.db,
                organization_settings=self.organization_settings,
                report_id=str(self.report.id) if self.report else None,
                current_execution_id=str(self.current_execution.id) if self.current_execution else None,
                user_message=user_message,
            )
            return await evaluator.evaluate(prev_tool_name_before_last_user)
        except Exception:
            return {"decision": False, "conditions": []}

    def _validate_tool_for_plan_type(self, tool_name: str, plan_type: str) -> bool:
        """Validate that tool is available for the chosen plan type.
        
        NOTE: We no longer enforce strict plan_type matching. The plan_type is a
        reasoning signal for the LLM, not a hard constraint. Strict validation
        was causing loops where the LLM couldn't call action tools during research.
        """
        metadata = self.registry.get_metadata(tool_name)
        if not metadata:
            return False
        
        # Always allow - plan_type is advisory, not enforced
        return True

    async def _handle_streaming_event(self, tool_name: str, event: dict, tool_input: dict = None):
        """Handle real-time streaming events for widget/step management."""
        event_type = event.get("type")
        payload = event.get("payload", {})
        
        if event_type != "tool.progress":
            return
            
        stage = payload.get("stage")
        
        try:
            if tool_name in ["create_widget", "create_data", "describe_entity"]:
                if stage == "data_model_type_determined":
                    # Create Query, Step and Visualization early when we know the type
                    data_model_type = payload.get("data_model_type")
                    # Accept either payload.query_title (preferred) or tool_input.title/widget_title for backward-compat
                    query_title = (
                        (payload.get("query_title") if isinstance(payload, dict) else None)
                        or (tool_input and (tool_input.get("title") or tool_input.get("widget_title")))
                        or "Untitled Query"
                    )

                    if data_model_type and self.report and not self.current_step:
                        # Create query (transitional service may still create a widget under the hood)
                        try:
                            self.current_query = await self.project_manager.create_query_v2(
                                self.db, self.report, query_title
                            )
                        except Exception:
                            self.current_query = None

                        # Create step under the query
                        initial_data_model = {"type": data_model_type, "columns": [], "series": []}
                        self.current_step = await self.project_manager.create_step_for_query(
                            self.db, self.current_query, query_title, "chart", initial_data_model
                        )
                        self.current_step_id = str(self.current_step.id)
                        await self.project_manager.set_query_default_step_if_empty(self.db, self.current_query, self.current_step_id)

                        # Create visualization (draft) with only type in view
                        try:
                            self.current_visualization = await self.project_manager.create_visualization_v2(
                                self.db, str(self.report.id), str(self.current_query.id), query_title, view={"type": data_model_type}, status="draft"
                            )
                        except Exception:
                            self.current_visualization = None

                        # Emit early query/visualization creation events
                        try:
                            seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(
                                event="query.created",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=seq,
                                data={
                                    "query_id": str(self.current_query.id) if self.current_query else None,
                                    "report_id": str(self.report.id),
                                    "title": query_title,
                                }
                            ))
                        except Exception:
                            pass
                        try:
                            if self.current_visualization:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="visualization.created",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "visualization_id": str(self.current_visualization.id),
                                        "query_id": str(self.current_query.id) if self.current_query else None,
                                        "report_id": str(self.report.id),
                                        "step_id": str(self.current_step.id),
                                        "view": {"type": data_model_type},
                                    }
                                ))
                        except Exception:
                            pass

                        # Emit artifact delta for step data_model.type
                        try:
                            seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            change = ArtifactChangeSchema(
                                type="step",
                                step_id=str(self.current_step.id),
                                partial=True,
                                changed_fields=["data_model.type"],
                                fields={"data_model": {"type": data_model_type}},
                            )
                            await self._emit_sse_event(SSEEvent(
                                event="block.delta.artifact",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=seq,
                                data={"change": change.model_dump()}
                            ))
                        except Exception:
                            pass
                
                elif stage == "column_added":
                    # Update current step's data model with new column
                    column = payload.get("column", {})
                    if self.current_step and column:
                        current_data_model = getattr(self.current_step, "data_model", {}) or {}
                        current_data_model.setdefault("columns", [])
                        # Add column if not already present
                        if not any(col.get("generated_column_name") == column.get("generated_column_name") 
                                 for col in current_data_model["columns"]):
                            current_data_model["columns"].append(column)
                            await self.project_manager.update_step_with_data_model(
                                self.db, self.current_step, current_data_model
                            )
                            # Emit artifact delta per column
                            try:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                change = ArtifactChangeSchema(
                                    type="step",
                                    step_id=str(self.current_step.id),
                                    widget_id=str(self.current_widget.id) if self.current_widget else None,
                                    partial=True,
                                    changed_fields=["data_model.columns"],
                                    fields={"data_model": {"columns": [column]}},
                                )
                                await self._emit_sse_event(SSEEvent(
                                    event="block.delta.artifact",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={"change": change.model_dump()}
                                ))
                            except Exception:
                                pass
                            
                elif stage == "series_configured":
                    # Update current step's data model with series
                    series = payload.get("series", [])
                    if self.current_step and series:
                        current_data_model = getattr(self.current_step, "data_model", {}) or {}
                        current_data_model["series"] = series
                        await self.project_manager.update_step_with_data_model(
                            self.db, self.current_step, current_data_model
                        )
                        # Emit artifact delta for series update
                        try:
                            seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            change = ArtifactChangeSchema(
                                type="step",
                                step_id=str(self.current_step.id),
                                widget_id=str(self.current_widget.id) if self.current_widget else None,
                                partial=True,
                                changed_fields=["data_model.series"],
                                fields={"data_model": {"series": series}},
                            )
                            await self._emit_sse_event(SSEEvent(
                                event="block.delta.artifact",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=seq,
                                data={"change": change.model_dump()}
                            ))
                        except Exception:
                            pass
                elif stage == "validating_code":
                    # If validation fails, mark the step as error with the validation message
                    try:
                        is_valid = payload.get("valid", None)
                        if is_valid is False and self.current_step:
                            error_msg = payload.get("error") or "Validation failed"
                            await self.project_manager.update_step_status(
                                self.db, self.current_step, "error", status_reason=str(error_msg)
                            )
                    except Exception:
                        pass
                        
                elif stage == "widget_creation_needed":
                    # Update step with final complete data_model
                    data_model = payload.get("data_model", {})
                    query_title = (tool_input and tool_input.get("widget_title")) or payload.get("widget_title") or "Untitled Query"

                    # If for some reason earlier streaming did not create query/step/visualization, create them now
                    if data_model and not self.current_step and self.report:
                        try:
                            self.current_query = await self.project_manager.create_query_v2(self.db, self.report, query_title)
                            self.current_step = await self.project_manager.create_step_for_query(self.db, self.current_query, query_title, "chart", {"type": data_model.get("type"), "columns": [], "series": []})
                            self.current_step_id = str(self.current_step.id)
                            await self.project_manager.set_query_default_step_if_empty(self.db, self.current_query, self.current_step_id)
                            self.current_visualization = await self.project_manager.create_visualization_v2(self.db, str(self.report.id), str(self.current_query.id), query_title, view={"type": data_model.get("type")}, status="draft")
                            # Emit creation events
                            seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(event="query.created", completion_id=str(self.system_completion.id), agent_execution_id=str(self.current_execution.id), seq=seq, data={"query_id": str(self.current_query.id), "report_id": str(self.report.id), "title": query_title}))
                            if self.current_visualization:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(event="visualization.created", completion_id=str(self.system_completion.id), agent_execution_id=str(self.current_execution.id), seq=seq, data={"visualization_id": str(self.current_visualization.id), "query_id": str(self.current_query.id), "report_id": str(self.report.id), "step_id": str(self.current_step.id), "view": {"type": data_model.get("type")}}))
                        except Exception:
                            pass
            elif tool_name == "create_data":
                # Code-first path: create query/step/visualization early so outputs can be persisted
                if stage in ["generated_code", "executing_code"]:
                    try:
                        query_title = (tool_input and (tool_input.get("title") or tool_input.get("widget_title"))) or "Untitled Query"
                        if not self.current_step and self.report:
                            # Create query and step with a default table view
                            try:
                                self.current_query = await self.project_manager.create_query_v2(
                                    self.db, self.report, query_title
                                )
                            except Exception:
                                self.current_query = None

                            self.current_step = await self.project_manager.create_step_for_query(
                                self.db,
                                self.current_query,
                                query_title,
                                "chart",
                                {"type": "table", "columns": [], "series": []},
                            )
                            self.current_step_id = str(self.current_step.id)
                            await self.project_manager.set_query_default_step_if_empty(self.db, self.current_query, self.current_step_id)

                            # Create a draft visualization with table view
                            try:
                                self.current_visualization = await self.project_manager.create_visualization_v2(
                                    self.db,
                                    str(self.report.id),
                                    str(self.current_query.id),
                                    query_title,
                                    view={"type": "table"},
                                    status="draft",
                                )
                            except Exception:
                                self.current_visualization = None

                            # Emit creation events
                            try:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="query.created",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "query_id": str(self.current_query.id) if self.current_query else None,
                                        "report_id": str(self.report.id),
                                        "title": query_title,
                                    }
                                ))
                            except Exception:
                                pass
                            try:
                                if self.current_visualization:
                                    seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                    await self._emit_sse_event(SSEEvent(
                                        event="visualization.created",
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=seq,
                                        data={
                                            "visualization_id": str(self.current_visualization.id),
                                            "query_id": str(self.current_query.id) if self.current_query else None,
                                            "report_id": str(self.report.id),
                                            "step_id": str(self.current_step.id),
                                            "view": {"type": "table"},
                                        }
                                    ))
                            except Exception:
                                pass
                    except Exception:
                        pass
            
            elif tool_name == "create_dashboard":
                # Stream-only handling: append blocks into active layout via ProjectManager
                if stage == "init":
                    # Clear existing blocks before generating new dashboard layout
                    await self.project_manager.clear_active_layout_blocks(
                        self.db, str(self.report.id)
                    )
                elif stage == "block.completed":
                    block = payload.get("block") or {}
                    if isinstance(block, dict) and self.report:
                        try:
                            await self.project_manager.append_block_to_active_dashboard_layout(
                                self.db, str(self.report.id), block
                            )
                        except Exception:
                            pass
                # No persistence outside layout service; finalization happens on tool end
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error handling streaming event {stage} for {tool_name}: {e}")
            # Don't re-raise; this is streaming and shouldn't break the main flow

    async def _handle_tool_output(self, tool_name: str, tool_input: dict, observation: dict, tool_output: dict = None):
        """Handle tool outputs and manage final state updates."""
        if not observation or observation.get("error"):
            return  # Don't process failed tool executions
            
        try:
            if tool_name in ["create_and_execute_code", "create_widget", "create_data", "describe_entity"]:
                # Update current step with code and data using tool_output
                if not tool_output:
                    return
                
                code = tool_output.get("code", "")
                widget_data = tool_output.get("widget_data", {}) or tool_output.get("data", {})
                success = tool_output.get("success", False)
                data_model_from_tool = tool_output.get("data_model") or {}
                view_options_from_tool = tool_output.get("view_options") or {}
                
                step_obj = None
                if self.current_step_id:
                    step_obj = await self.db.get(Step, self.current_step_id)
                
                if step_obj and success and widget_data:
                    # If tool provided a minimal data_model (type/series), merge it into the step before deriving view
                    try:
                        if isinstance(data_model_from_tool, dict) and data_model_from_tool:
                            existing_dm = (getattr(step_obj, "data_model", {}) or {}).copy()
                            merged = existing_dm.copy()
                            # Preserve existing type; only set if missing
                            if not merged.get("type") and data_model_from_tool.get("type"):
                                merged["type"] = data_model_from_tool.get("type")
                            # Merge series/grouping fields
                            for key in ("series", "group_by", "sort", "limit"):
                                if data_model_from_tool.get(key) is not None:
                                    merged[key] = data_model_from_tool.get(key)
                            await self.project_manager.update_step_with_data_model(self.db, step_obj, merged)
                            # Refresh the object to read the updated data_model
                            await self.db.refresh(step_obj)
                    except Exception:
                        pass
                    # Update step with code
                    await self.project_manager.update_step_with_code(
                        self.db, step_obj, code
                    )
                    # Update step with full data (not just preview)
                    await self.project_manager.update_step_with_data(
                        self.db, step_obj, widget_data
                    )
                    
                    # Update step status
                    await self.project_manager.update_step_status(
                        self.db, step_obj, "success"
                    )

                    # Emit table usage events based on the step's data model (align with legacy agent)
                    try:
                        await self.project_manager.emit_table_usage(
                            db=self.db,
                            report=self.report,
                            step=step_obj,
                            data_model=getattr(step_obj, "data_model", {}) or {},
                            user_id=str(getattr(self.head_completion, "user_id", None)) if hasattr(self.head_completion, "user_id") and self.head_completion.user_id else None,
                            user_role=None
                        )
                    except Exception:
                        pass

                    # Fallback for create_data: if no columns in data_model, emit usage from tool_input.tables_by_source
                    try:
                        if tool_name == "create_data":
                            dm = getattr(step_obj, "data_model", {}) or {}
                            cols = dm.get("columns") if isinstance(dm, dict) else None
                            has_columns = isinstance(cols, list) and len(cols) > 0
                            if not has_columns and isinstance(tool_input, dict):
                                tbs = tool_input.get("tables_by_source")
                                if tbs:
                                    await self.project_manager.emit_table_usage_from_tables_by_source(
                                        db=self.db,
                                        report=self.report,
                                        step=step_obj,
                                        tables_by_source=tbs,
                                        user_id=str(getattr(self.head_completion, "user_id", None)) if hasattr(self.head_completion, "user_id") and self.head_completion.user_id else None,
                                        user_role=None,
                                        source_type="sql",
                                    )
                    except Exception:
                        pass

                    # Finalize visualization view.encoding and status
                    try:
                        dm = getattr(step_obj, "data_model", {}) or {}
                        if getattr(self, 'current_visualization', None):
                            # Prefer tool-provided view (ViewSchema v2) if available
                            view_from_tool = tool_output.get("view")
                            if isinstance(view_from_tool, dict) and view_from_tool.get("version") == "v2":
                                # Use the new ViewSchema v2 format directly
                                view = view_from_tool
                            else:
                                # Legacy fallback: compute encoding from step.data_model.series
                                enc = self.project_manager.derive_encoding_from_data_model(dm)
                                view = {"type": dm.get("type")}
                                if enc:
                                    view["encoding"] = enc
                                # Merge any tool-provided view options (e.g., colors palette)
                                try:
                                    if isinstance(view_options_from_tool, dict) and view_options_from_tool:
                                        current_options = (view.get("options") or {})
                                        merged_options = {**current_options, **view_options_from_tool}
                                        view["options"] = merged_options
                                except Exception:
                                    pass
                            await self.project_manager.update_visualization_view(self.db, self.current_visualization, view)
                            await self.project_manager.set_visualization_status(self.db, self.current_visualization, "success")
                            # Emit visualization.updated
                            try:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event="visualization.updated",
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
                                    data={
                                        "visualization_id": str(self.current_visualization.id),
                                        "view": view,
                                        "status": "success",
                                    }
                                ))
                            except Exception:
                                pass
                            # Add created_visualization_ids to observation result for tool.finished
                            observation.setdefault("created_visualization_ids", [])
                            observation["created_visualization_ids"].append(str(self.current_visualization.id))
                    except Exception:
                        pass

                    # Ensure observation carries ids for auditing/tracking
                    observation["step_id"] = self.current_step_id

            elif tool_name == "inspect_data":
                # Track table usage for inspection
                try:
                    if isinstance(tool_input, dict):
                        tbs = tool_input.get("tables_by_source")
                        if tbs:
                            await self.project_manager.emit_table_usage_from_tables_by_source(
                                db=self.db,
                                report=self.report,
                                step=None,
                                tables_by_source=tbs,
                                user_id=str(getattr(self.head_completion, "user_id", None)) if hasattr(self.head_completion, "user_id") and self.head_completion.user_id else None,
                                user_role=None,
                                source_type="sql",
                            )
                except Exception:
                    pass
            
            elif tool_name == "create_dashboard":
                # Finalize: ensure observation has the latest active layout blocks
                try:
                    if self.report:
                        blocks = await self.project_manager.get_active_dashboard_layout_blocks(
                            self.db, str(self.report.id)
                        )
                        observation.setdefault("layout", {})
                        observation["layout"]["blocks"] = blocks
                except Exception:
                    pass

                # Optional: publish widgets per input (kept from previous behavior)
                try:
                    widget_ids = []
                    use_all_widgets = True
                    if isinstance(tool_input, dict):
                        widget_ids = tool_input.get("widget_ids") or []
                        use_all_widgets = tool_input.get("use_all_widgets", True)

                    if widget_ids:
                        for wid in widget_ids:
                            w = await self.db.get(Widget, str(wid))
                            if w and str(getattr(w, "report_id", "")) == str(getattr(self.report, "id", "")):
                                w.status = "published"
                                self.db.add(w)
                    elif use_all_widgets and self.report:
                        res = await self.db.execute(select(Widget).where(Widget.report_id == str(self.report.id)))
                        for w in res.scalars().all():
                            if w.status != "published":
                                w.status = "published"
                                self.db.add(w)
                    await self.db.commit()
                except Exception:
                    pass
        except Exception as e:
            # Import logging if not already available
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error handling tool output for {tool_name}: {e}")
            # Don't re-raise; this is post-processing and shouldn't break the main flow