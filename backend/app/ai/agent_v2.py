import asyncio
import json
from typing import Dict, Optional

from app.ai.agents.planner import PlannerV2
from app.ai.context import ContextHub, ContextBuildSpec
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.registry import ToolRegistry, ToolCatalogFilter
from app.schemas.ai.planner import PlannerInput, ToolDescriptor
from app.websocket_manager import websocket_manager
from app.ai.runner.tool_runner import ToolRunner
from app.ai.runner.policies import RetryPolicy, TimeoutPolicy
from app.project_manager import ProjectManager


class AgentV2:
    """Enhanced orchestrator with intelligent research/action flow."""

    def __init__(self, db=None, organization=None, organization_settings=None, report=None,
                 model=None, messages=[], head_completion=None, system_completion=None, widget=None, step=None):
        self.db = db
        self.organization = organization
        self.organization_settings = organization_settings
        self.report = report
        self.model = model
        self.head_completion = head_completion
        self.system_completion = system_completion
        self.widget = widget
        self.step = step

        self.sigkill_event = asyncio.Event()
        websocket_manager.add_handler(self._handle_completion_update)
        
        # Agent execution tracking
        self.project_manager = ProjectManager()
        self.current_execution = None

        # Initialize ContextHub for centralized context management
        self.context_hub = ContextHub(
            db=self.db,
            organization=self.organization,
            report=self.report,
            user=getattr(self.head_completion, 'user', None) if self.head_completion else None,
            head_completion=self.head_completion,
            widget=self.widget
        )
        
        # Initialize observation context builder
        self.observation_builder = ObservationContextBuilder()

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
        
        # Build planner with full catalog (using ContextHub for enhanced context)
        from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder
        self.planner = PlannerV2(
            model=self.model,
            instruction_context_builder=InstructionContextBuilder(self.db, self.organization),
            tool_catalog=tool_catalog,
        )
        
        # Tool runner with enhanced policies
        self.tool_runner = ToolRunner(
            retry=RetryPolicy(max_attempts=2, backoff_ms=500, backoff_multiplier=2.0, jitter_ms=200),
            timeout=TimeoutPolicy(start_timeout_s=5, idle_timeout_s=30, hard_timeout_s=120),
        )

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
            schemas_excerpt = view.static.schemas
            
            # History summary based on observation context only
            history_summary = await self.context_hub.get_history_summary(self.observation_builder.to_dict())
            observation: Optional[dict] = None
            step_limit = self.organization_settings.get_config("limit_analysis_steps").value if self.organization_settings else 5

            current_plan_decision = None
            
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

                # Build enhanced planner input
                planner_input = PlannerInput(
                    user_message=self.head_completion.prompt["content"],
                    schemas_excerpt=schemas_excerpt,
                    history_summary=history_summary,
                    last_observation=observation,
                    external_platform=getattr(self.head_completion, "external_platform", None),
                    tool_catalog=self.planner.tool_catalog,
                )

                # PLAN: stream planner tokens and decision using typed input
                async for evt in self.planner.execute(planner_input, self.sigkill_event):
                    if self.sigkill_event.is_set():
                        break

                    # Handle typed events
                    if evt.type == "planner.decision.partial":
                        decision = evt.data
                        
                        # Get next sequence number
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        
                        # Save partial decision (Pydantic model)
                        current_plan_decision = await self.project_manager.save_plan_decision_from_model(
                            self.db,
                            agent_execution=self.current_execution,
                            seq=seq,
                            loop_index=loop_index,
                            planner_decision_model=decision,
                        )
                        
                        # Emit SSE event
                        await self._emit_sse_event({
                            "event": "decision.partial",
                            "data": {
                                "agent_execution_id": self.current_execution.id,
                                "seq": seq,
                                "plan_type": decision.plan_type,
                                "reasoning": decision.reasoning_message,
                                "assistant": decision.assistant_message,
                                "action": decision.action.model_dump() if decision.action else None,
                            }
                        })
                    
                    elif evt.type == "planner.decision.final":
                        decision = evt.data
                        
                        # Get next sequence number
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        
                        # Save final decision (Pydantic model)
                        current_plan_decision = await self.project_manager.save_plan_decision_from_model(
                            self.db,
                            agent_execution=self.current_execution,
                            seq=seq,
                            loop_index=loop_index,
                            planner_decision_model=decision,
                        )
                        
                        # Emit SSE event
                        await self._emit_sse_event({
                            "event": "decision.final",
                            "data": {
                                "agent_execution_id": self.current_execution.id,
                                "seq": seq,
                                "analysis_complete": decision.analysis_complete,
                                "final_answer": decision.final_answer,
                                "metrics": decision.metrics.model_dump() if decision.metrics else None,
                            }
                        })
                        
                        if decision.analysis_complete:
                            # Final answer path
                            break

                        action = decision.action
                        if not action:
                            break
                            
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
                        await self._emit_sse_event({
                            "event": "tool.started",
                            "data": {
                                "agent_execution_id": self.current_execution.id,
                                "seq": seq,
                                "tool_name": tool_name,
                                "arguments": tool_input,
                            }
                        })
                        
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
                            "sigkill_event": self.sigkill_event,
                            "observation_context": self.observation_builder.to_dict(),  # Pass observation context
                            "context_view": view,
                        }
                        
                        async def emit(ev: dict):
                            # Forward tool events to UI and persist important ones
                            if ev.get("type") in ["tool.progress", "tool.error"]:
                                seq = await self.project_manager.next_seq(self.db, self.current_execution)
                                await self._emit_sse_event({
                                    "event": ev.get("type", "tool.progress"),
                                    "data": {
                                        "agent_execution_id": self.current_execution.id,
                                        "seq": seq,
                                        "tool_name": tool_name,
                                        "payload": ev.get("payload", {}),
                                    }
                                })

                        observation = await self.tool_runner.run(tool, tool_input, runtime_ctx, emit)
                        
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
                            result_model=observation,
                            summary=observation.get("summary", "") if observation else "",
                            created_widget_id=created_widget_id,
                            created_step_id=created_step_id,
                            error_message=observation.get("error", {}).get("message") if observation and observation.get("error") else None,
                            context_snapshot_id=post_snap.id,
                            success=bool(observation and not observation.get("error")),
                        )
                        
                        # Emit tool finished event
                        seq = await self.project_manager.next_seq(self.db, self.current_execution)
                        await self._emit_sse_event({
                            "event": "tool.finished",
                            "data": {
                                "agent_execution_id": self.current_execution.id,
                                "seq": seq,
                                "tool_name": tool_name,
                                "status": "success" if observation and not observation.get("error") else "error",
                                "result_summary": observation.get("summary", "") if observation else "",
                                "created_widget_id": created_widget_id,
                                "created_step_id": created_step_id,
                            }
                        })
                        
                        # Track tool execution in observation builder
                        self.observation_builder.add_tool_observation(tool_name, tool_input, observation)
                        
                        # Refresh warm sections and view for next iteration
                        await self.context_hub.refresh_warm()
                        view = self.context_hub.get_view()
                        schemas_excerpt = view.static.schemas
                        
                        # Refresh history summary with updated context
                        history_summary = await self.context_hub.get_history_summary(self.observation_builder.to_dict())
                        
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
            
            # Finish agent execution
            status = 'sigkill' if self.sigkill_event.is_set() else 'success'
            await self.project_manager.finish_agent_execution(
                self.db,
                agent_execution=self.current_execution,
                status=status,
            )
            
        except Exception as e:
            # Handle errors and finish execution with error status
            if self.current_execution:
                await self.project_manager.finish_agent_execution(
                    self.db,
                    agent_execution=self.current_execution,
                    status='error',
                    error_json={"message": str(e), "type": type(e).__name__},
                )
            raise
        finally:
            # Cleanup
            try:
                websocket_manager.remove_handler(self._handle_completion_update)
            except Exception:
                pass

    async def _emit_sse_event(self, event_data: dict):
        """Emit SSE event via websocket manager."""
        try:
            if self.report:
                event_data["report_id"] = str(self.report.id)
                await websocket_manager.broadcast_to_report(
                    str(self.report.id), 
                    json.dumps(event_data)
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