import asyncio
import json
from typing import Dict, Optional
from pydantic import ValidationError

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
from app.ai.agents.reporter.reporter import Reporter
from sqlalchemy import select, func
from app.models.tool_execution import ToolExecution
from app.models.agent_execution import AgentExecution
from app.ai.agents.judge.judge import Judge
from app.ai.agents.suggest_instructions.suggest_instructions import SuggestInstructions
from app.settings.database import create_async_session_factory
from app.core.telemetry import telemetry

TOP_K_PER_DS = 20  # Number of tables to sample per data source
INDEX_LIMIT = 1000  # Number of tables to include in the index


class AgentV2:
    """Enhanced orchestrator with intelligent research/action flow."""

    def __init__(self, db=None, organization=None, organization_settings=None, report=None,
                 model=None, mode=None, messages=[], head_completion=None, system_completion=None, widget=None, step=None, event_queue=None, clients=None):
        self.db = db
        self.organization = organization
        self.organization_settings = organization_settings
        self.mode = mode


        self.ai_analyst_name = organization_settings.config.get('general', {}).get('ai_analyst_name', "AI Analyst")

        self.report = report
        self.model = model
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
            widget=self.widget
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
        )
        
        # Tool runner with enhanced policies
        self.tool_runner = ToolRunner(
            retry=RetryPolicy(max_attempts=2, backoff_ms=500, backoff_multiplier=2.0, jitter_ms=200),
            timeout=TimeoutPolicy(start_timeout_s=5, idle_timeout_s=30, hard_timeout_s=120),
        )
        
        # Initialize Reporter for title generation
        self.reporter = Reporter(model=self.model)
        # Initialize Judge using ContextHub's instruction builder
        self.judge = Judge(model=self.model, organization_settings=self.organization_settings, instruction_context_builder=self.context_hub.instruction_builder)

        # Initialize SuggestInstructions agent for post-analysis suggestions
        self.suggest_instructions = SuggestInstructions(model=self.model)

    async def _run_early_scoring_background(self, planner_input: PlannerInput):
        """Run instructions/context scoring in a fresh DB session to avoid concurrency conflicts."""
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    # Use a new Judge instance (stateless) and score from the same planner input
                    if self.organization_settings.get_config("enable_llm_judgement") and self.organization_settings.get_config("enable_llm_judgement").value:
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
                    if self.organization_settings.get_config("enable_llm_judgement") and self.organization_settings.get_config("enable_llm_judgement").value:
                        judge = Judge(model=self.model, organization_settings=self.organization_settings)
                        original_prompt = self.head_completion.prompt.get("content", "") if getattr(self.head_completion, "prompt", None) else ""
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


    async def main_execution(self):
        try:
            # Start agent execution tracking
            self.current_execution = await self.project_manager.start_agent_execution(
                self.db,
                completion_id=str(self.system_completion.id),
                organization_id=str(self.organization.id),
                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                report_id=str(self.report.id) if self.report else None,
            )

            # Telemetry: agent execution started
            try:
                await telemetry.capture(
                    "agent_execution_started",
                    {
                        "agent_execution_id": str(self.current_execution.id),
                        "report_id": str(self.report.id) if self.report else None,
                        "model_id": self.model.model_id if self.model else None,
                    },
                    user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None,
                    org_id=str(self.organization.id) if self.organization else None,
                )
            except Exception:
                pass

            
            # Prime static once; then refresh warm each loop
            await self.context_hub.prime_static()
            await self.context_hub.refresh_warm()
            # Populate metadata counts (schemas/messages/etc) before first snapshot
            try:
                await self.context_hub.build_context()
            except Exception:
                pass
            view = self.context_hub.get_view()
            
            # Save initial context snapshot
            await self.project_manager.save_context_snapshot(
                self.db,
                agent_execution=self.current_execution,
                kind="initial",
                context_view_json=view.model_dump(),
                prompt_text=self.head_completion.prompt.get("content", "") if self.head_completion.prompt else "",
            )
            
            # Initial context values (combined per data source: sample Top-K + index)
            try:
                schemas_ctx = await self.context_hub.schema_builder.build(
                    include_inactive=False,
                    with_stats=True,
                    active_only=True,
                )
                combined_schemas = schemas_ctx.render_combined(top_k_per_ds=TOP_K_PER_DS, index_limit=INDEX_LIMIT)
                schemas_excerpt = combined_schemas
            except Exception:
                schemas_excerpt = view.static.schemas.render() if view.static.schemas else ""
            # Resources combined (sample + index)
            try:
                resources_ctx = await self.context_hub.resource_builder.build()
                resources_combined = resources_ctx.render_combined(top_k_per_repo=TOP_K_PER_DS, index_limit=INDEX_LIMIT)
            except Exception:
                try:
                    resources_section_fallback = await self.context_hub.resource_builder.build()
                    resources_combined = resources_section_fallback.render()
                except Exception:
                    resources_combined = ""
            # History summary based on observation context only
            history_summary = await self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

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

            # Instructions
            inst_section = await self.context_hub.instruction_builder.build()
            instructions = inst_section.render()

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
            
            # Early scoring will be launched as a background task using an isolated session

            for loop_index in range(step_limit):
                if self.sigkill_event.is_set():
                    break

                # Save pre-tool context snapshot
                await self.context_hub.refresh_warm()
                view = self.context_hub.get_view()
                await self.project_manager.save_context_snapshot(
                    self.db,
                    agent_execution=self.current_execution,
                    kind="pre_tool",
                    context_view_json=view.model_dump(),
                )

                # Build enhanced planner input with validation and retry on failure
                try:
                    # Get messages context for detailed conversation history
                    # Render messages from object section
                    messages_section = await self.context_hub.message_builder.build(max_messages=20)
                    messages_context = messages_section.render()
                    # Get resources context from metadata resources
                    resources_section = await self.context_hub.resource_builder.build()
                    resources_context = resources_section.render()
                    # Smaller combined excerpt to control tokens per-iteration
                    try:
                        resources_combined_small = resources_section.render_combined(top_k_per_repo=10, index_limit=200)
                    except Exception:
                        resources_combined_small = resources_context
                    # Files context (uploaded files schemas/metadata)
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
                        
                        # Get next sequence number for SSE event ordering (not used for DB upsert)
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
                        
                        # Get next sequence number for SSE event ordering (not used for DB upsert)
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
                        if decision.analysis_complete:
                            # Final answer path
                            invalid_retry_count = 0
                            # === Post-analysis: determine if instruction suggestions are required
                            res = await self._should_suggest_instructions(prev_tool_name_before_last_user)
                            should_trigger_suggestions = res.get("decision", False)
                            hint = res.get("hint", "")

                            if should_trigger_suggestions:
                                # Stream suggestions via SSE
                                try:
                                    # Build fresh context for suggestions
                                    await self.context_hub.refresh_warm()
                                    view_for_suggest = self.context_hub.get_view()
                                except Exception:
                                    view_for_suggest = None
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
                                    if self.suggest_instructions is not None:
                                        drafts = []
                                        async for draft in self.suggest_instructions.stream_suggestions(context_view=view_for_suggest, context_hub=self.context_hub, hint=hint):
                                            # Persist immediately and stream back full instruction object
                                            inst = await self.project_manager.create_instruction_from_draft(
                                                self.db,
                                                self.organization,
                                                text=draft.get("text", ""),
                                                category=draft.get("category", "general"),
                                                agent_execution_id=str(self.current_execution.id),
                                                trigger_reason=hint,
                                                ai_source="completion",
                                                user_id=str(getattr(self.head_completion, 'user_id', None)) if hasattr(self.head_completion, 'user_id') and self.head_completion.user_id else None
                                            )
                                            # Append a minimal client payload too
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
                            break

                        action = decision.action
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
                                include_inactive=False,
                                with_stats=True,
                                active_only=True,
                            )
                            schemas_excerpt = schemas_ctx.render_combined(top_k_per_ds=10, index_limit=200)
                        except Exception:
                            schemas_excerpt = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""
                        # Refresh history summary with updated context
                        history_summary = await self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

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

                        # Extract created objects from observation
                        created_widget_id = None
                        created_step_id = None
                        if observation and "widget_id" in observation:
                            created_widget_id = observation["widget_id"]
                        if observation and "step_id" in observation:
                            created_step_id = observation["step_id"]

                        # Capture post-tool context snapshot
                        await self.context_hub.refresh_warm()
                        # Refresh static sections (schemas with stats) so post_tool snapshot reflects latest table usage
                        try:
                            await self.context_hub.build_context()
                        except Exception:
                            pass
                        post_view = self.context_hub.get_view()
                        post_snap = await self.project_manager.save_context_snapshot(
                            self.db,
                            agent_execution=self.current_execution,
                            kind="post_tool",
                            context_view_json=post_view.model_dump(),
                        )

                        # Finish tool execution tracking
                        await self.project_manager.finish_tool_execution_from_models(
                            self.db,
                            tool_execution=tool_execution,
                            result_model=tool_output,
                            summary=observation.get("summary", "") if observation else "",
                            created_widget_id=created_widget_id,
                            created_step_id=created_step_id,
                            created_visualization_ids=(observation.get("created_visualization_ids") if observation else None),
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
                                "created_visualization_ids": (observation.get("created_visualization_ids") if observation else None),
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
                        history_summary = await self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

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
            await self.project_manager.save_context_snapshot(
                self.db,
                agent_execution=self.current_execution,
                kind="final",
                context_view_json=view.model_dump(),
            )
            
            # Generate report title if this is the first completion
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
                        # Generate title based on the conversation and plan decisions
                        messages_section = await self.context_hub.message_builder.build(max_messages=5)
                        messages_context = messages_section.render()
                        
                        # Extract plan information from current execution
                        plan_info = []
                        if current_plan_decision:
                            if hasattr(current_plan_decision, 'action_name') and current_plan_decision.action_name:
                                plan_info.append({"action": current_plan_decision.action_name})
                        
                        title = await self.reporter.generate_report_title(messages_context, plan_info)
                        await self.project_manager.update_report_title(self.db, self.report, title)
            except Exception as e:
                # Don't fail the entire execution if title generation fails
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to generate report title: {e}")
            
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
            
            # Update system completion status and emit event
            if self.system_completion:
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

        Always returns a dict: {"decision": bool, "hint": str}.

        Conditions:
        - A) This agent_execution includes a create_widget tool AND the previous tool before the latest user message was clarify.
        - OR
        - B) Within THIS agent_execution, there exists a successful create_widget whose result_json.errors has length >= 1
             (i.e., one or more internal retries/failures before eventual success).
        """
        if not self.organization_settings.get_config("suggest_instructions") and self.organization_settings.get_config("suggest_instructions").value:
            return {"decision": False, "hint": ""}
        
        hint = ""
        try:
            report_id = str(self.report.id) if self.report else None
            if not report_id:
                return {"decision": False, "hint": ""}

            # A) Current execution contains create_widget
            ran_create_widget = False
            try:
                if self.current_execution:
                    stmt_cw_current = (
                        select(ToolExecution.id)
                        .where(ToolExecution.agent_execution_id == str(self.current_execution.id))
                        .where(ToolExecution.tool_name == "create_widget")
                        .limit(1)
                    )
                    res_cw_current = await self.db.execute(stmt_cw_current)
                    ran_create_widget = res_cw_current.first() is not None
            except Exception:
                ran_create_widget = False

            trigger_a = bool(ran_create_widget and prev_tool_name_before_last_user == "clarify")
            hint += "Suggesting instructions due to a clarification tool call followed by a user message that triggered a create_widget tool" if trigger_a else ""

            # B) Success with internal retries in the CURRENT execution (errors list present on success)
            success_with_retries = False
            try:
                if self.current_execution:
                    stmt_successes = (
                        select(ToolExecution.result_json)
                        .where(ToolExecution.agent_execution_id == str(self.current_execution.id))
                        .where(ToolExecution.tool_name == "create_widget")
                        .where((ToolExecution.success == True) | (ToolExecution.status == "success"))
                        .order_by(ToolExecution.started_at.desc())
                        .limit(10)
                    )
                    res_rows = await self.db.execute(stmt_successes)
                    for (result_json,) in res_rows.all():
                        try:
                            errors = (result_json or {}).get("errors", [])
                            if isinstance(errors, list) and len(errors) >= 1:
                                success_with_retries = True
                                hint += "\n\nSuggesting instructions due to a successful create_widget tool with internal retries"
                                break
                        except Exception:
                            continue
            except Exception:
                success_with_retries = False

            return { "decision": bool(trigger_a or success_with_retries), "hint": hint }
        except Exception:
            return {"decision": False, "hint": ""}

    def _validate_tool_for_plan_type(self, tool_name: str, plan_type: str) -> bool:
        """Validate that tool is available for the chosen plan type."""
        metadata = self.registry.get_metadata(tool_name)
        if not metadata:
            return False
            
        if plan_type == "research":
            return metadata.category in ["research", "both"]
        elif plan_type == "action":
            return metadata.category in ["action", "both"]
            
        return False

    async def _handle_streaming_event(self, tool_name: str, event: dict, tool_input: dict = None):
        """Handle real-time streaming events for widget/step management."""
        event_type = event.get("type")
        payload = event.get("payload", {})
        
        if event_type != "tool.progress":
            return
            
        stage = payload.get("stage")
        
        try:
            if tool_name in ["create_widget", "create_data"]:
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
                    # No-op here; layout service ensures active layout on first write
                    pass
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
            if tool_name in ["create_and_execute_code", "create_widget", "create_data"]:
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
                        # Compute encoding from step.data_model.series
                        dm = getattr(step_obj, "data_model", {}) or {}
                        enc = self.project_manager.derive_encoding_from_data_model(dm)
                        if getattr(self, 'current_visualization', None):
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