from typing import Optional, List, Any
from pydantic import BaseModel, Field


class PromptParameterSpec(BaseModel):
    """A single template parameter for a prompt (mirrors PromptParameter)."""
    name: str = Field(..., description="Placeholder key referenced in the text as {{name}}.")
    label: Optional[str] = Field(None, description="Human-friendly label shown when filling the prompt.")
    type: str = Field('text', description="One of: 'text', 'number', 'enum', 'date', 'date_range'.")
    required: bool = Field(False, description="Whether the user must supply a value before running.")
    default: Optional[Any] = Field(None, description="Default value used when none is supplied.")
    options: Optional[List[str]] = Field(None, description="Allowed values when type == 'enum'.")


class CreatePromptInput(BaseModel):
    """Input schema for create_prompt tool — saves a reusable prompt for an agent.

    A prompt is a saved, completion-shaped instruction users can re-run (or that
    surfaces as a conversation starter). Use this in training mode to curate the
    reusable prompts attached to the agent(s) you manage.
    """

    text: str = Field(
        ...,
        description=(
            "The prompt body — what gets sent as the user message when the prompt "
            "is run. Write it as a clear, self-contained analytical request. "
            "Templated placeholders use {{name}} and must be declared in `parameters`."
        ),
        min_length=4,
    )

    title: Optional[str] = Field(
        None,
        description="Short human-friendly title. Auto-derived from the text when omitted.",
        max_length=200,
    )

    scope: str = Field(
        'agent',
        description=(
            "'agent' (default) attaches the prompt to the agent(s) in `data_source_ids` "
            "(falls back to the agents on the current report); 'private' is visible only "
            "to you. 'global' (org-wide) requires org admin and is usually not appropriate "
            "in training mode."
        ),
    )

    data_source_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Agent (data source) IDs to attach an 'agent'-scoped prompt to. When omitted, "
            "the agents attached to the current report are used. Ignored for 'private'/'global'."
        ),
    )

    mode: str = Field(
        'chat',
        description="Mode the prompt runs in: 'chat' (default), 'deep', or 'training'.",
    )

    is_starter: bool = Field(
        False,
        description="When true, the prompt is surfaced as a conversation starter for the agent.",
    )

    parameters: Optional[List[PromptParameterSpec]] = Field(
        None,
        description="Template parameters for {{placeholders}} in the text.",
    )


class CreatePromptOutput(BaseModel):
    """Output schema for create_prompt tool response."""

    success: bool = Field(..., description="Whether the prompt was created")
    prompt_id: Optional[str] = Field(None, description="ID of the created prompt")
    title: Optional[str] = Field(None, description="Resolved title of the prompt")
    scope: Optional[str] = Field(None, description="Scope the prompt was created with")
    data_source_ids: List[str] = Field(default_factory=list, description="Agents the prompt is attached to")
    is_starter: Optional[bool] = Field(None, description="Whether it surfaces as a conversation starter")
    message: str = Field(..., description="Status message")
    rejected_reason: Optional[str] = Field(
        None, description="Reason if rejected (e.g. permission_denied, no_data_sources, invalid_scope)"
    )
