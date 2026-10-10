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
            "Do NOT add an 'Other' entry here — set `allow_other` instead."
        ),
    )
    allow_other: bool = Field(
        False,
        description=(
            "Set true when `options` may not cover every case. The UI then adds its own "
            "'Other' choice (in the user's language) with a text box for a custom answer. "
            "Ignored for free-form questions."
        ),
    )
    multi_select: bool = Field(
        False,
        description=(
            "Set true when the user may pick several of `options` at once "
            "(select-all-that-apply, e.g. 'which metrics should the dashboard include?'). "
            "Ignored for free-form questions. Defaults to single choice."
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
