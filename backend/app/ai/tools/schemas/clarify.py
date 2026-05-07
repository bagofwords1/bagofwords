from typing import Optional
from pydantic import BaseModel, Field


class ClarifyInput(BaseModel):
    """Input schema for clarify tool - signals that clarification is needed.

    The user-facing questions live in the model's message text, not in this
    tool's input. This tool only carries an optional internal note and marks
    that we're waiting for the user's response.
    """

    context: Optional[str] = Field(
        None,
        description="Brief internal note about why clarification is needed (optional, not shown to the user)"
    )


class ClarifyOutput(BaseModel):
    """Output schema for clarify tool response."""

    status: str = Field(
        default="awaiting_response",
        description="Status of the clarification request"
    )
