from typing import Optional
from pydantic import BaseModel, Field


class ReadInstructionInput(BaseModel):
    """Input schema for the read_instruction tool.

    Reads the full text of a single instruction (typically a skill advertised
    in <available_skills>) by the SHORT id prefix shown in the prompt.
    """

    id: str = Field(
        ...,
        description=(
            "The instruction id — pass the SHORT id prefix exactly as shown in "
            "<available_skills> (e.g. 'be8090f2'). The full UUID also works. "
            "Must be at least 4 characters; if the prefix matches more than one "
            "instruction you'll get the candidates back, so pass a longer prefix."
        ),
        min_length=4,
    )


class ReadInstructionOutput(BaseModel):
    """Output schema for the read_instruction tool response."""

    success: bool = Field(..., description="Whether the read succeeded")
    id: Optional[str] = Field(None, description="Full instruction id that was read")
    short_id: Optional[str] = Field(None, description="Short id (first 8 chars)")
    title: Optional[str] = Field(None, description="Instruction/skill title")
    text: Optional[str] = Field(None, description="Full instruction/skill text")
    category: Optional[str] = Field(None, description="Instruction category")
    kind: Optional[str] = Field(None, description="'instruction' | 'skill'")
    load_mode: Optional[str] = Field(None, description="Loading mode")
    message: Optional[str] = Field(None, description="Status or error message")
