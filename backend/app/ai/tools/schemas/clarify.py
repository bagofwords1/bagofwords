from typing import Optional
from pydantic import BaseModel, Field


class ClarifyQuestion(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        description="The question shown to the user. Keep it concise.",
    )
    options: Optional[list[str]] = Field(
        None,
        description=(
            "Clickable answer choices rendered as selectable chips. "
            "Omit for free-form text input. "
            "Include an 'Other…' entry when the list may not be exhaustive."
        ),
    )


class ClarifyInput(BaseModel):
    """Input schema for the clarify tool.

    Each entry in ``questions`` becomes an interactive form row: a chip-picker
    when ``options`` is supplied, a text field otherwise. All questions are
    shown at once; the user submits all answers in a single reply.
    """

    questions: list[ClarifyQuestion] = Field(
        ...,
        min_length=1,
        description="One or more questions to ask the user before proceeding.",
    )
    context: Optional[str] = Field(
        None,
        description="Brief internal note about why clarification is needed (not shown to the user).",
    )


class ClarifyOutput(BaseModel):
    """Output schema for the clarify tool."""

    status: str = Field(
        default="awaiting_response",
        description="Status of the clarification request.",
    )
