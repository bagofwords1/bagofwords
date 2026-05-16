from typing import Optional
from pydantic import BaseModel, Field


class ClarifyInput(BaseModel):
    """Input schema for clarify tool - signals that clarification is needed.

    The user-facing clarification text lives in ``question`` — this is what
    the user sees. Required because a clarify call with no question is
    useless to the user.
    """

    question: str = Field(
        ...,
        min_length=1,
        description=(
            "The clarifying message shown to the user. Markdown OK. "
            "Use a numbered list for multiple questions and bullets to "
            "enumerate concrete options under a question (end the bullets "
            "with 'or specify your own.' when listing options)."
        ),
    )
    context: Optional[str] = Field(
        None,
        description="Brief internal note about why clarification is needed (optional, not shown to the user)",
    )


class ClarifyOutput(BaseModel):
    """Output schema for clarify tool response."""

    status: str = Field(
        default="awaiting_response",
        description="Status of the clarification request"
    )
