from dataclasses import dataclass, field
from typing import Optional

from app.schemas.ai.planner import PlannerInputV3, PlannerMetrics


@dataclass
class PlannerStateV3:
    """Internal state for planner_v3 event-stream folding."""

    input: PlannerInputV3
    metrics: PlannerMetrics = field(default_factory=PlannerMetrics)
    start_time: Optional[float] = None
    first_token_time: Optional[float] = None
    # Pre-tool reasoning text accumulator (text deltas before any tool_use)
    reasoning_buffer: str = ""
    # Final-answer text accumulator (text deltas when the model is finishing without a tool)
    final_buffer: str = ""
    # Provider-native extended-thinking accumulator (Anthropic thinking blocks).
    # Distinct from reasoning_buffer (which holds assistant text fragments).
    thinking_buffer: str = ""
    # Have we seen a tool_use_start yet? Determines which buffer text deltas go to.
    saw_tool_use: bool = False
    # Reasoning timing
    reasoning_start_time: Optional[float] = None
    reasoning_end_time: Optional[float] = None
