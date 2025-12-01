from dataclasses import dataclass, field
from typing import Optional
from app.schemas.ai.planner import PlannerInput, PlannerDecision, PlannerMetrics


@dataclass
class PlannerState:
    """Internal state management for planner execution."""
    
    input: PlannerInput
    buffer: str = ""
    metrics: PlannerMetrics = field(default_factory=PlannerMetrics)
    decision: Optional[PlannerDecision] = None
    start_time: Optional[float] = None
    first_token_time: Optional[float] = None
    # Reasoning timing: track when reasoning_message field first has content
    reasoning_start_time: Optional[float] = None
    # Track when assistant_message field first has content (marks reasoning end)
    assistant_start_time: Optional[float] = None
    # Track previous field states to detect transitions
    _prev_reasoning: str = ""
    _prev_assistant: str = ""