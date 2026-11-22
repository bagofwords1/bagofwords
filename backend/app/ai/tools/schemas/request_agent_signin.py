"""Pydantic schemas for the request_agent_signin tool."""
from typing import List, Optional

from pydantic import BaseModel, Field


class RequestAgentSigninInput(BaseModel):
    agent_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="Ids of agents from <agents_needing_signin> (or marked SIGN-IN REQUIRED by search_agents) that the request needs.",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=600,
        description=(
            "One or two sentences to the user, in their language: which agent the answer "
            "needs and that they should connect it with the button below, then ask again. "
            "Shown as your reply; no ids."
        ),
    )
    title: Optional[str] = Field(
        None,
        description="Short active-voice status label shown to the user, e.g. 'Asking to connect Sales'.",
    )


class SigninAgentItem(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    type: Optional[str] = None
    connector_key: Optional[str] = None
    icon: Optional[str] = None


class RequestAgentSigninOutput(BaseModel):
    success: bool
    agents: List[SigninAgentItem] = Field(default_factory=list)
    # Requested ids that need no sign-in (usable now) or are not in scope.
    rejected_ids: List[str] = Field(default_factory=list)
    message: Optional[str] = None
