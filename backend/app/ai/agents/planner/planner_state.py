from dataclasses import dataclass, field
from typing import Optional
from app.ai.schemas.planner import PlannerInput, PlannerDecision, PlannerMetrics


@dataclass
class PlannerState:
    """Internal state management for planner execution."""
    
    input: PlannerInput
    buffer: str = ""
    metrics: PlannerMetrics = field(default_factory=PlannerMetrics)
    decision: Optional[PlannerDecision] = None
    start_time: Optional[float] = None
    first_token_time: Optional[float] = None