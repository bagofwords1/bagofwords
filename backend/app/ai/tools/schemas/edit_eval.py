from typing import List, Literal, Optional
from pydantic import BaseModel, Field, model_validator

from app.schemas.test_expectations import ExpectationsSpec
from app.ai.tools.schemas.create_eval import CreateEvalPrompt


class EditEvalInput(BaseModel):
    """Input schema for ``edit_eval`` — partial update; only provided fields change."""

    case_id: str = Field(..., description="The eval case to edit.")
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[Literal["active", "draft", "archived"]] = Field(
        default=None,
        description=(
            "Lifecycle change. 'active' promotes a draft into scheduled/suite "
            "runs; 'archived' retires the case (results are kept)."
        ),
    )
    prompt: Optional[CreateEvalPrompt] = Field(
        default=None,
        description=(
            "Replace the replay prompt. Changing the prompt's MEANING "
            "invalidates history comparisons — prefer creating a new case."
        ),
    )
    expectations: Optional[ExpectationsSpec] = Field(
        default=None,
        description="Replace the full expectations spec (same rules as create_eval).",
    )
    tags: Optional[List[str]] = Field(
        default=None, description="Replace the tag list (e.g. ['smoke', 'joins'])."
    )
    suite_id: Optional[str] = Field(
        default=None, description="Move the case to another suite in this organization."
    )

    @model_validator(mode="after")
    def _something_to_change(self):
        if not any([
            self.name, self.status, self.prompt, self.expectations,
            self.tags is not None, self.suite_id,
        ]):
            raise ValueError("Provide at least one field to change.")
        return self


class EditEvalOutput(BaseModel):
    success: bool
    case_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    suite_id: Optional[str] = None
    suite_name: Optional[str] = None
    changed_fields: List[str] = Field(default_factory=list)
    rejected_reason: Optional[str] = None
    message: Optional[str] = None
