from typing import Optional

from pydantic import BaseModel, Field


class RouteModelInput(BaseModel):
    """Input for the route_model tool.

    The per-request tool schema advertised to the planner replaces ``model``
    with an enum of the org's eligible routing targets (see
    app.ai.model_router.build_route_model_schema); this Pydantic model is the
    permissive validation fallback.
    """

    model: str = Field(..., description="Provider model id to route the rest of the task to.")
    reason: Optional[str] = Field(default=None, description="Short reason for the routing choice.")


class RouteModelOutput(BaseModel):
    success: bool
    routed: bool = False
    model: Optional[str] = None
