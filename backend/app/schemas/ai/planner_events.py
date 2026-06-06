from typing import List, Literal, Optional
from pydantic import BaseModel
from .planner import PlannerDecision


class PlannerEvent(BaseModel):
    type: str


class PlannerTokenEvent(PlannerEvent):
    type: Literal["planner.tokens"]
    delta: str


class PlannerDecisionEvent(PlannerEvent):
    type: Literal["planner.decision.partial", "planner.decision.final"]
    data: PlannerDecision


class PlannerWebSearchEvent(PlannerEvent):
    """A provider-executed web search completed during planning. The agent turns
    this into a tool-execution record + block so it renders like other tools."""
    type: Literal["planner.web_search"]
    id: str
    query: Optional[str] = None
    queries: Optional[List[str]] = None
    status: Optional[str] = None


