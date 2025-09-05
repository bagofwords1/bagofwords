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
from sqlalchemy import select
from app.ai.agents.judge.judge import Judge
from app.settings.database import create_async_session_factory


class AgentV2:
    """Enhanced orchestrator with intelligent research/action flow."""

    def __init__(self, db=None, organization=None, organization_settings=None, report=None,
                 model=None, messages=[], head_completion=None, system_completion=None, widget=None, step=None, event_queue=None):
        self.db = db
        self.organization = organization
        self.organization_settings = organization_settings


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
            self.clients = {data_source.name: data_source.get_client() for data_source in self.data_sources}
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

    async def _run_early_scoring_background(self, planner_input: PlannerInput):
        """Run instructions/context scoring in a fresh DB session to avoid concurrency conflicts."""
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    # Use a new Judge instance (stateless) and score from the same planner input
                    judge = Judge(model=self.model, organization_settings=self.organization_settings)
                    instructions_score, context_score = await judge.score_instructions_and_context_from_planner_input(planner_input)
                    # Re-fetch completion to avoid using objects from another session
                    completion = await session.get(Completion, str(self.head_completion.id))
                    if completion is not None:
                        await self.project_manager.update_completion_scores(session, completion, instructions_score, context_score)
                except Exception:
                    pass
        except Exception:
            pass

    async def _run_late_scoring_background(self, messages_context: str, observation_data: dict):
        """Run response scoring in a fresh DB session to avoid concurrency conflicts."""
        try:
            SessionLocal = create_async_session_factory()
            async with SessionLocal() as session:
                try:
                    judge = Judge(model=self.model, organization_settings=self.organization_settings)
                    original_prompt = self.head_completion.prompt.get("content", "") if getattr(self.head_completion, "prompt", None) else ""
                    response_score = await judge.score_response_quality(original_prompt, messages_context, observation_data=observation_data)
                    completion = await session.get(Completion, str(self.head_completion.id))
                    if completion is not None:
                        await self.project_manager.update_completion_response_score(session, completion, response_score)
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

            
            # Prime static once; then refresh warm each loop
            await self.context_hub.prime_static()
            await self.context_hub.refresh_warm()
            view = self.context_hub.get_view()
            
            # Save initial context snapshot
            await self.project_manager.save_context_snapshot(
                self.db,
                agent_execution=self.current_execution,
                kind="initial",
                context_view_json=view.model_dump(),
                prompt_text=self.head_completion.prompt.get("content", "") if self.head_completion.prompt else "",
            )
            
            # Initial context values
            schemas_excerpt = view.static.schemas.render() if view.static.schemas else ""
            
            # History summary based on observation context only
            history_summary = await self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())

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
                    planner_input = PlannerInput(
                        organization_name=self.organization.name,
                        organization_ai_analyst_name=self.ai_analyst_name,
                        instructions=instructions,
                        user_message=self.head_completion.prompt["content"],
                        schemas_excerpt=schemas_excerpt,
                        history_summary=history_summary,
                        messages_context=messages_context,
                        resources_context=resources_context,
                        last_observation=observation,
                        past_observations=self.context_hub.observation_builder.tool_observations,
                        external_platform=getattr(self.head_completion, "external_platform", None),
                        tool_catalog=self.planner.tool_catalog,
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

                        # Start tool execution tracking
                        tool_execution = await self.project_manager.start_tool_execution_from_models(
                            self.db,
                            agent_execution=self.current_execution,
                            plan_decision_id=current_plan_decision.id if current_plan_decision else None,
                            tool_name=tool_name,
                            tool_action=action.type,
                            tool_input_model=tool_input,
                        )
                        
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
                            "current_widget": self.current_widget,  # Current widget being worked on
                            "current_step": self.current_step,      # Current step being worked on
                            "current_step_id": self.current_step_id, # Stable id for persistence across tools
                            "project_manager": self.project_manager,  # For widget/step creation
                            "model": self.model,  # LLM model for code generation
                            "sigkill_event": self.sigkill_event,
                            "observation_context": self.context_hub.observation_builder.to_dict(),  # Pass observation context
                            "context_view": view,
                            "context_hub": self.context_hub,
                            # Data source clients and files (mirror agent.py pattern)
                            "ds_clients": self.clients,
                            "excel_files": self.files
                            # Context builders can be accessed via context_hub when needed
                        }
                        
                        async def emit(ev: dict):
                            # Handle streaming state management for widget/step creation
                            await self._handle_streaming_event(tool_name, ev, tool_input)
                            
                            # Forward tool events to UI and persist important ones
                            if ev.get("type") in ["tool.progress", "tool.error", "tool.partial", "tool.stdout"]:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event(SSEEvent(
                                    event=ev.get("type", "tool.progress"),
                                    completion_id=str(self.system_completion.id),
                                    agent_execution_id=str(self.current_execution.id),
                                    seq=seq,
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
                            # Fallback for legacy format
                            observation = tool_result
                            tool_output = None
                        

                        
                        # Handle tool outputs and manage widget/step state
                        await self._handle_tool_output(tool_name, tool_input, observation, tool_output)
                        
                        # Circuit breaker: track repeated tool failures
                        if observation and observation.get("error"):
                            failed_tool_count[tool_name] = failed_tool_count.get(tool_name, 0) + 1
                            
                            # If this tool has failed too many times, force completion
                            if failed_tool_count[tool_name] >= max_tool_failures:
                                analysis_done = True
                                # Override the observation to include a final answer
                                observation.update({
                                    "analysis_complete": True,
                                    "final_answer": f"Unable to complete the task. The {tool_name} tool failed {failed_tool_count[tool_name]} times with errors. Please check the tool configuration or try a different approach."
                                })
                        else:
                            # Reset failure count on successful execution
                            if tool_name in failed_tool_count:
                                del failed_tool_count[tool_name]
                            
                            # Track successful actions to detect infinite success loops
                            action_signature = f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"
                            successful_tool_actions.append(action_signature)
                            
                            # Check for repeated successful actions
                            if len(successful_tool_actions) >= max_repeated_successes:
                                recent_actions = successful_tool_actions[-max_repeated_successes:]
                                if len(set(recent_actions)) == 1:  # All same action
                                    analysis_done = True
                                    # Override observation to include completion
                                    observation.update({
                                        "analysis_complete": True,
                                        "final_answer": f"Task completed successfully. The {tool_name} tool has been executed {max_repeated_successes} times with the same parameters, indicating the goal has been achieved."
                                    })
                        
                        # Check if tool runner is signaling to complete analysis
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
                        post_view = self.context_hub.get_view()
                        post_snap = await self.project_manager.save_context_snapshot(
                            self.db,
                            agent_execution=self.current_execution,
                            kind="post_tool",
                            context_view_json=post_view.model_dump(),
                        )

                        # Finish tool execution tracking (Pydantic-friendly)
                        await self.project_manager.finish_tool_execution_from_models(
                            self.db,
                            tool_execution=tool_execution,
                            result_model=tool_output,
                            summary=observation.get("summary", "") if observation else "",
                            created_widget_id=created_widget_id,
                            created_step_id=created_step_id,
                            error_message=observation.get("error", {}).get("message") if observation and observation.get("error") else None,
                            context_snapshot_id=post_snap.id,
                            success=bool(observation and not observation.get("error")),
                        )
                        # Upsert completion block for tool and rebuild transcript
                        try:
                            block = await self.project_manager.upsert_block_for_tool(self.db, self.system_completion, self.current_execution, tool_execution)
                            await self.project_manager.rebuild_completion_from_blocks(self.db, self.system_completion, self.current_execution)
                            if block is not None:
                                # Emit a v2-shaped block snapshot
                                try:
                                    block_schema = await serialize_block_v2(self.db, block)
                                    await self._emit_sse_event(SSEEvent(
                                        event="block.upsert",
                                        completion_id=str(self.system_completion.id),
                                        agent_execution_id=str(self.current_execution.id),
                                        seq=seq,
                                        data={"block": block_schema.model_dump()}
                                    ))
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        
                        # Emit tool finished event (include result_json and duration_ms for frontend)
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        # Sanitize tool_output for SSE (avoid invalid JSON due to special values)
                        safe_result_json = None
                        if tool_output is not None:
                            try:
                                safe_result_json = json.loads(json.dumps(tool_output, default=str))
                            except Exception:
                                # Fallback to minimal payload
                                safe_result_json = {
                                    "summary": observation.get("summary", "") if observation else "",
                                }
                        await self._emit_sse_event(SSEEvent(
                            event="tool.finished",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq,
                            data={
                                "tool_name": tool_name,
                                "status": "success" if observation and not observation.get("error") else "error",
                                "result_summary": observation.get("summary", "") if observation else "",
                                "result_json": safe_result_json,  # Include tool output (code, logs, etc.)
                                "duration_ms": tool_execution.duration_ms,
                                "created_widget_id": created_widget_id,
                                "created_step_id": created_step_id,
                            }
                        ))
                        
                        # Track tool execution in observation builder unless tool opts out
                        try:
                            meta = self.registry.get_metadata(tool_name)
                            if not meta or getattr(meta, "observation_policy", "on_trigger") != "never":
                                self.context_hub.observation_builder.add_tool_observation(tool_name, tool_input, observation)
                        except Exception:
                            pass
                        # Reset invalid retry counter after a successful tool attempt (even if tool errors, planner was valid)
                        invalid_retry_count = 0
                        
                        # If suggest_instructions just ran, mark analysis complete to end execution
                        if tool_name == "suggest_instructions":
                            analysis_done = True

                        # Refresh warm sections and view for next iteration
                        await self.context_hub.refresh_warm()
                        view = self.context_hub.get_view()
                        schemas_excerpt = view.static.schemas.render() if getattr(view.static, "schemas", None) else ""
                        
                        # Refresh history summary with updated context
                        history_summary = await self.context_hub.get_history_summary(self.context_hub.observation_builder.to_dict())
                        
                        break

                # If planner finalized analysis, stop the outer loop as well
                if analysis_done:
                    break

            # Save final context snapshot
            await self.context_hub.refresh_warm()
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
            
            # Keep existing websocket broadcasting for backward compatibility
            if self.report:
                legacy_event_data = {
                    "event": event.event,
                    "data": event.data,
                    "report_id": str(self.report.id)
                }
                await websocket_manager.broadcast_to_report(
                    str(self.report.id), 
                    json.dumps(legacy_event_data)
                )
                
        except Exception as e:
            print(f"Error emitting SSE event: {e}")

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
            if tool_name in ["create_data_model", "create_widget"]:
                if stage == "data_model_type_determined":
                    # Create widget and step early when we know the type
                    data_model_type = payload.get("data_model_type")
                    widget_title = (tool_input and tool_input.get("widget_title")) or "Untitled Widget"
                    
                    if data_model_type and self.report:
                        # Create widget
                        self.current_widget = await self.project_manager.create_widget(
                            self.db, self.report, widget_title
                        )
                        
                        # Create step
                        self.current_step = await self.project_manager.create_step(
                            self.db, widget_title, self.current_widget, "chart"
                        )
                        # Track stable step id for subsequent tools in this execution
                        self.current_step_id = str(self.current_step.id)
                        
                        # Initialize data_model with type
                        initial_data_model = {"type": data_model_type, "columns": [], "series": []}
                        await self.project_manager.update_step_with_data_model(
                            self.db, self.current_step, initial_data_model
                        )
                        
                        # Emit early widget creation event
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        await self._emit_sse_event(SSEEvent(
                            event="widget.created",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq,
                            data={
                                "widget_id": str(self.current_widget.id),
                                "step_id": str(self.current_step.id),
                                "widget_title": widget_title,
                                "data_model_type": data_model_type,
                            }
                        ))
                        try:
                            seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            change = ArtifactChangeSchema(
                                type="step",
                                step_id=str(self.current_step.id),
                                widget_id=str(self.current_widget.id) if self.current_widget else None,
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
                    widget_title = (tool_input and tool_input.get("widget_title")) or payload.get("widget_title") or "Untitled Widget"
                    
                    # If for some reason earlier streaming did not create widget/step, create them now
                    if data_model and not self.current_step and self.report:
                        try:
                            self.current_widget = await self.project_manager.create_widget(
                                self.db, self.report, widget_title
                            )
                            self.current_step = await self.project_manager.create_step(
                                self.db, widget_title, self.current_widget, "chart"
                            )
                            self.current_step_id = str(self.current_step.id)
                            # Emit widget.created event so UI can latch to ids
                            seq = await self.project_manager.next_seq(self.db, self.current_execution)
                            await self._emit_sse_event(SSEEvent(
                                event="widget.created",
                                completion_id=str(self.system_completion.id),
                                agent_execution_id=str(self.current_execution.id),
                                seq=seq,
                                data={
                                    "widget_id": str(self.current_widget.id),
                                    "step_id": str(self.current_step.id),
                                    "widget_title": widget_title,
                                    "data_model_type": data_model.get("type")
                                }
                            ))
                        except Exception:
                            pass

                    if data_model and self.current_step:
                        # Update step with final data_model
                        await self.project_manager.update_step_with_data_model(
                            self.db, self.current_step, data_model
                        )
                        # Do not auto-publish; publishing will be done explicitly by user or create_dashboard
                        
                        # Emit data model completion event to UI
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        await self._emit_sse_event(SSEEvent(
                            event="data_model.completed",
                            completion_id=str(self.system_completion.id),
                            agent_execution_id=str(self.current_execution.id),
                            seq=seq,
                            data={
                                "widget_id": str(self.current_widget.id),
                                "step_id": str(self.current_step.id),
                                "data_model": data_model,
                            }
                        ))
                        # Also emit a consolidated artifact snapshot delta
                        try:
                            change = ArtifactChangeSchema(
                                type="step",
                                step_id=str(self.current_step.id),
                                widget_id=str(self.current_widget.id) if self.current_widget else None,
                                partial=False,
                                changed_fields=["data_model"],
                                fields={"data_model": data_model},
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
            if tool_name == "create_data_model":
                # Widget/step creation already handled in streaming
                # Refresh current_step from database to get updated data_model
                if self.current_widget and self.current_step:
                    await self.db.refresh(self.current_step)
                    observation["widget_id"] = str(self.current_widget.id)
                    observation["step_id"] = str(self.current_step.id)
                    observation["data_model"] = getattr(self.current_step, "data_model", {})
                    
            elif tool_name == "modify_data_model":
                # Update current step's data_model using tool_output
                if tool_output:
                    data_model = tool_output.get("data_model", {})
                    
                    if data_model and self.current_step:
                        await self.project_manager.update_step_with_data_model(
                            self.db, self.current_step, data_model
                        )
                    
            elif tool_name in ["create_and_execute_code", "create_widget"]:
                # Update current step with code and data using tool_output
                if not tool_output:
                    return
                    
                code = tool_output.get("code", "")
                widget_data = tool_output.get("widget_data", {})
                success = tool_output.get("success", False)
                
                step_obj = None
                if self.current_step_id:
                    step_obj = await self.db.get(Step, self.current_step_id)
                
                if step_obj and success and widget_data:
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

                    # Ensure observation carries ids for auditing/tracking
                    if self.current_widget:
                        observation["widget_id"] = str(self.current_widget.id)
                    observation["step_id"] = self.current_step_id
                    
            elif tool_name == "create_dashboard":
                # Publish widgets per tool input: publish selected widget_ids or all widgets in report
                try:
                    widget_ids = []
                    use_all_widgets = True
                    if isinstance(tool_input, dict):
                        widget_ids = tool_input.get("widget_ids") or []
                        use_all_widgets = tool_input.get("use_all_widgets", True)

                    if widget_ids:
                        # Publish only specified widgets
                        for wid in widget_ids:
                            w = await self.db.get(Widget, str(wid))
                            if w and str(getattr(w, "report_id", "")) == str(getattr(self.report, "id", "")):
                                w.status = "published"
                                self.db.add(w)
                    elif use_all_widgets and self.report:
                        # Publish all widgets belonging to the current report
                        res = await self.db.execute(select(Widget).where(Widget.report_id == str(self.report.id)))
                        for w in res.scalars().all():
                            if w.status != "published":
                                w.status = "published"
                                self.db.add(w)
                    await self.db.commit()
                except Exception:
                    # Publishing failure should not break the tool flow
                    pass

        except Exception as e:
            # Import logging if not already available
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error handling tool output for {tool_name}: {e}")
            # Don't re-raise; this is post-processing and shouldn't break the main flow