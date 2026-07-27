from typing import Optional
from pydantic import BaseModel, Field


class UpdateUserMemoryInput(BaseModel):
    """Input for the update_user_memory tool.

    The agent submits the FULL new memory document (not a diff). The current
    memory is shown to the agent in the <user_memory> block of the user turn,
    so it rewrites from what it sees — adding, revising, or pruning lines to
    keep the document small and current.
    """

    content: str = Field(..., description=(
        "The COMPLETE new memory document (replaces the old one entirely). Keep it a short, "
        "curated list of durable facts about THIS user — preferences, writing/formatting style, "
        "recurring goals, and analyses they liked (reference the report by title, don't paste "
        "data). This is a full rewrite: to add something when the doc is near the limit, drop a "
        "stale line. Pass an empty string to clear the memory. Plain text or short markdown bullets."
    ))
    title: Optional[str] = Field(default=None, description=(
        "Optional short label for what changed (e.g. \"Saved formatting preference\"). Shown to "
        "the user as the tool's status line."
    ))


class UpdateUserMemoryOutput(BaseModel):
    """Output from the update_user_memory tool."""

    success: bool = Field(..., description="Whether the memory was saved.")
    char_count: int = Field(default=0, description="Length of the saved memory document.")
    error: Optional[str] = Field(default=None, description="Error message when success is False.")
