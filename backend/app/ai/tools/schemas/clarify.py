from typing import Optional
from pydantic import BaseModel, Field


class ClarifyInput(BaseModel):
    """Input schema for clarify tool - signals that clarification is needed.

    The planner outputs the actual questions in assistant_message.
    This tool just marks that we're waiting for user response.
    """

    context: Optional[str] = Field(
        None,
        description="Brief context about why clarification is needed (optional)"
    )


class ClarifyOutput(BaseModel):
    """Output schema for clarify tool response."""

    status: str = Field(
        default="awaiting_response",
        description="Status of the clarification request"
    )
