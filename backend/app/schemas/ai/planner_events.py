from typing import Literal
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


