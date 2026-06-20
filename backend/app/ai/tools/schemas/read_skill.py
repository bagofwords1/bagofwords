from typing import Optional
from pydantic import BaseModel, Field


class ReadSkillInput(BaseModel):
    """Input schema for the read_skill tool.

    Reads the full text of a single skill (advertised in <available_skills>) by
    the SHORT id prefix shown there.
    """

    id: str = Field(
        ...,
        description=(
            "The skill id — pass the SHORT id prefix exactly as shown in "
            "<available_skills> (e.g. 'be8090f2'). The full UUID also works. "
            "Must be at least 4 characters; if the prefix matches more than one "
            "skill you'll get the candidates back, so pass a longer prefix."
        ),
        min_length=4,
    )


class ReadSkillOutput(BaseModel):
    """Output schema for the read_skill tool response."""

    success: bool = Field(..., description="Whether the read succeeded")
    id: Optional[str] = Field(None, description="Full skill id that was read")
    short_id: Optional[str] = Field(None, description="Short id (first 8 chars)")
    title: Optional[str] = Field(None, description="Skill title")
    description: Optional[str] = Field(None, description="Skill description, if set")
    text: Optional[str] = Field(None, description="Full skill text")
    category: Optional[str] = Field(None, description="Skill category")
    load_mode: Optional[str] = Field(None, description="Loading mode")
    message: Optional[str] = Field(None, description="Status or error message")
