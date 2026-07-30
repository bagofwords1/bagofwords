"""Pydantic schemas for the set_report_agents tool."""
from typing import List, Optional

from pydantic import BaseModel, Field


class SetReportAgentsInput(BaseModel):
    agent_ids: List[str] = Field(
        default_factory=list,
        description=(
            "The agent (data source) ids to focus for this report. Their FULL schema "
            "will be in context from the next step on; other attached agents stay in "
            "the thin roster. Pass an empty list to clear the focus (revert to the "
            "automatic selection). Get ids from search_agents or the <available_agents> "
            "roster."
        ),
    )
    reason: Optional[str] = Field(
        None,
        description=(
            "One short sentence for the USER explaining why these agents are needed "
            "(e.g. 'Need the Sales agent for order-level revenue'). Required in spirit "
            "when the report has a manual agent selection and you're adding an agent "
            "outside it — the user sees this on the approval card."
        ),
    )
    title: Optional[str] = Field(
        None,
        description="Short active-voice status label shown to the user, e.g. 'Focusing on the Sales agent'.",
    )


class SetReportAgentsOutput(BaseModel):
    success: bool = Field(..., description="Whether the focus was updated")
    focused_agent_ids: List[str] = Field(default_factory=list)
    focused_agent_names: List[str] = Field(default_factory=list)
    # Names actually ATTACHED to the report by this call (approved expansion) —
    # UI says "Added X to the report" for these vs "Focused on X".
    added_agent_names: List[str] = Field(default_factory=list)
    rejected_ids: List[str] = Field(default_factory=list)
    message: Optional[str] = None
