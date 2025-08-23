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
        # Prime static once; then refresh warm each loop
        await self.context_hub.prime_static()
        await self.context_hub.refresh_warm()
        view = self.context_hub.get_view()
        
        # Initial context values
        schemas_excerpt = view.static.schemas
        
        # History summary based on observation context only
        history_summary = await self.context_hub.get_history_summary(self.observation_builder.to_dict())
        observation: Optional[dict] = None
        step_limit = self.organization_settings.get_config("limit_analysis_steps").value if self.organization_settings else 5

        for loop_index in range(step_limit):
            if self.sigkill_event.is_set():
                break

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
                    # Emit reasoning and assistant messages if present
                    decision = evt.data

                    breakpoint()
                    if decision.reasoning_message:
                        # TODO: Emit reasoning message to UI
                        pass
                    if decision.assistant_message:
                        # TODO: Emit assistant message to UI  
                        pass
                
                elif evt.type == "planner.decision.final":
                    
                    decision = evt.data
                    
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
                    breakpoint()
                    
                    async def emit(ev: dict):
                        # Forward tool events to UI
                        pass

                    observation = await self.tool_runner.run(tool, tool_input, runtime_ctx, emit)
                    
                    # Track tool execution in observation builder
                    self.observation_builder.add_tool_observation(tool_name, tool_input, observation)
                    
                    # Refresh warm sections and view for next iteration
                    await self.context_hub.refresh_warm()
                    view = self.context_hub.get_view()
                    schemas_excerpt = view.static.schemas
                    
                    # Refresh history summary with updated context
                    history_summary = await self.context_hub.get_history_summary(self.observation_builder.to_dict())
                    
                    break

        # Cleanup
        try:
            websocket_manager.remove_handler(self._handle_completion_update)
        except Exception:
            pass

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