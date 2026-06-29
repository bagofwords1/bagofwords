from typing import Optional, List
from pydantic import BaseModel, Field

from app.ai.tools.schemas.create_prompt import PromptParameterSpec


class EditPromptInput(BaseModel):
    """Input schema for edit_prompt tool — updates an existing reusable prompt.

    Use this to refine a prompt's text/title, change which agents it's attached
    to, toggle conversation-starter, or adjust its mode/parameters. Only the
    fields you set are changed.
    """

    prompt_id: str = Field(
        ...,
        description=(
            "ID of the prompt to edit. Find it via search_prompts or from a prior "
            "create_prompt observation."
        ),
    )

    text: Optional[str] = Field(
        None,
        description="New prompt body. Leave unset to keep the current text.",
        min_length=4,
    )

    title: Optional[str] = Field(None, description="New title.", max_length=200)

    scope: Optional[str] = Field(
        None,
        description="New scope: 'agent', 'private', or 'global' (global requires org admin).",
    )

    data_source_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Replacement set of agent (data source) IDs to attach the prompt to. "
            "Pass an empty list to detach from all agents."
        ),
    )

    mode: Optional[str] = Field(None, description="New mode: 'chat', 'deep', or 'training'.")

    is_starter: Optional[bool] = Field(
        None, description="Toggle whether the prompt surfaces as a conversation starter."
    )

    parameters: Optional[List[PromptParameterSpec]] = Field(
        None, description="Replacement template parameters."
    )


class EditPromptOutput(BaseModel):
    """Output schema for edit_prompt tool response."""

    success: bool = Field(..., description="Whether the prompt was updated")
    prompt_id: str = Field(..., description="ID of the prompt that was edited")
    title: Optional[str] = Field(None, description="Title after the edit")
    message: str = Field(..., description="Status message")
    rejected_reason: Optional[str] = Field(
        None, description="Reason if rejected (e.g. not_found, permission_denied, invalid_scope)"
    )
