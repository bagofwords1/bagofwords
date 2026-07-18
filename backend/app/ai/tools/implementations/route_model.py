"""route_model tool — let the planner escalate to a stronger model mid-run.

When the org's Auto model router is on and the user picked no model, the planner
starts on the small model. Before doing user-visible work it may call this tool
to switch to a stronger model for the rest of the task. The switch is applied by
the run's RoutingController (passed in via runtime_ctx), which swaps the agent's
model and rebuilds the planner LLM — so every later planner turn and tool call
(create_data codegen, artifacts) runs on the new model automatically.

The tool is deterministic — no LLM call. It only appears in the catalog when
routing is active and at least one guided candidate model exists.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.route_model import RouteModelInput, RouteModelOutput
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolEvent,
    ToolStartEvent,
)

logger = logging.getLogger(__name__)


class RouteModelTool(Tool):
    """Escalate the planner/agent model to a stronger one for the current task."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="route_model",
            description=(
                "Switch the model used for the REST of this task. You started on a small, fast "
                "model. Call route_model FIRST — before any create_data/create_artifact or other "
                "user-visible work — if the task needs a stronger model (multi-step analysis, "
                "multi-source joins, dashboards, ambiguous or complex reasoning). Skip it for "
                "simple lookups, single-metric questions, and small follow-ups — those stay on the "
                "small model. Pick the cheapest model whose guidance fits the task. The switch is "
                "one-way and sticky for this task; it propagates to code generation too. Do not "
                "call it more than once per task."
            ),
            category="action",
            version="1.0.0",
            input_schema=RouteModelInput.model_json_schema(),
            output_schema=RouteModelOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=10,
            idempotent=False,
            required_permissions=[],
            is_active=True,
            observation_policy="always",
            tags=["routing", "meta"],
            allowed_modes=None,  # gated in agent_v2 by the model_routing org setting + candidates
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return RouteModelInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return RouteModelOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        requested = (tool_input or {}).get("model")
        reason = (tool_input or {}).get("reason")
        yield ToolStartEvent(type="tool.start", payload={"title": f"Route → {requested}"})

        controller = runtime_ctx.get("routing_controller")
        if controller is None or not getattr(controller, "has_candidates", lambda: False)():
            observation = {
                "summary": "Model routing is not active for this run; keeping current model.",
                "routed": False,
                "error": {"code": "routing_inactive", "message": "no routing controller/candidates"},
            }
            yield ToolEndEvent(
                type="tool.end",
                payload={"output": {"success": False, "routed": False}, "observation": observation},
            )
            return

        observation = await controller.apply(requested, reason)
        routed = bool(observation.get("routed"))
        output = {
            "success": True,
            "routed": routed,
            "model": observation.get("model"),
            "model_name": observation.get("model_name"),
            "provider_type": observation.get("provider_type"),
        }
        yield ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})
