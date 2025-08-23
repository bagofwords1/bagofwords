import asyncio
from typing import Dict, Optional

from app.ai.agents.planner import PlannerV2
from app.ai.context.instruction_context_builder import InstructionContextBuilder
from app.ai.registry import ToolRegistry
from app.websocket_manager import websocket_manager
from app.ai.runner.tool_runner import ToolRunner
from app.ai.runner.policies import RetryPolicy, TimeoutPolicy


class AgentV2:
    """Minimal orchestrator with streaming.

    - One action per loop
    - Streams planner tokens and decision snapshots
    - Streams tool events (delegated to tool.run_stream)
    - Uses a simple in-memory tool map (registry scaffold added separately)
    """

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

        # Registry scaffold
        self.registry = ToolRegistry()
        # Build planner with catalog from registry
        self.planner = PlannerV2(
            model=self.model,
            instruction_context_builder=InstructionContextBuilder(self.db, self.organization),
            tool_catalog=self.registry.get_catalog(self.organization),
        )
        # Optional: also allow direct injection map for early testing
        self.tools: Dict[str, object] = {}
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

    async def _build_schemas_excerpt(self) -> str:
        # Reuse existing builder lightly by slicing, or implement a short excerpt later
        try:
            from app.ai.agent import Agent as AgentV1
            agent_v1 = AgentV1(db=self.db, organization=self.organization, organization_settings=self.organization_settings,
                               report=self.report, model=self.model, head_completion=self.head_completion,
                               system_completion=self.system_completion, widget=self.widget, step=self.step)
            # Use existing method then trim
            schemas = await agent_v1._build_schemas_context()
            return schemas[:8000]
        except Exception:
            return ""

    async def _build_history_summary(self) -> str:
        # Minimal placeholder; can be improved to a concise summary
        return ""

    async def main_execution(self):
        schemas_excerpt = await self._build_schemas_excerpt()
        history_summary = await self._build_history_summary()
        observation: Optional[dict] = None
        step_limit = self.organization_settings.get_config("limit_analysis_steps").value if self.organization_settings else 3

        for loop_index in range(step_limit):
            if self.sigkill_event.is_set():
                break

            # PLAN: stream planner tokens and decision
            async for evt in self.planner.execute(
                user_message=self.head_completion.prompt["content"],
                schemas_excerpt=schemas_excerpt,
                history_summary=history_summary,
                last_observation=observation,
                external_platform=getattr(self.head_completion, "external_platform", None),
                sigkill_event=self.sigkill_event,
            ):
                if self.sigkill_event.is_set():
                    break

                if evt.get("type") == "planner.decision.final":
                    decision = evt.get("data", {})
                    if decision.get("analysis_complete"):
                        # Final answer path (MVP: write to system completion externally via existing services)
                        break

                    action = (decision.get("action") or {})
                    # action: { type: "tool_call", name, arguments }
                    tool_name = action.get("name") or action.get("tool")
                    tool_input = action.get("arguments") or {}

                    tool = self.registry.get(tool_name) or self.tools.get(tool_name)
                    if not tool:
                        # Unknown tool: return observation to planner and continue loop
                        observation = {
                            "summary": f"Tool '{tool_name}' unavailable",
                            "error": {"type": "resolve_error", "message": "not registered"},
                        }
                        break

                    # RUN TOOL (streaming via ToolRunner)
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
                    }
                    async def emit(ev: dict):
                        # In future: forward to UI and persist
                        _ = ev

                    observation = await self.tool_runner.run(tool, tool_input, runtime_ctx, emit)
                    break

        # End; cleanup handler
        try:
            websocket_manager.remove_handler(self._handle_completion_update)
        except Exception:
            pass

