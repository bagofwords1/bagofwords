"""YAML wrappers for suite import/export.

Thin layer over PromptSchema and ExpectationsSpec so existing pydantic
validation covers the bulk of the surface. Only the envelope (SuiteYaml /
CaseYaml) is new.

Portability rules:
- No UUIDs in YAML. Data sources referenced by slug/name; LLM models by
  ``<provider>/<model>`` pair. Resolution happens in TestSuiteService.
- A case is multi-turn iff ``turns`` is set; ``prompt`` and ``turns`` are
  mutually exclusive.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.schemas.completion_v2_schema import PromptSchema
from app.schemas.test_expectations import ExpectationsSpec


class PromptYaml(BaseModel):
    """Subset of PromptSchema that is authored in YAML.

    ``model`` is a ``<provider>/<model>`` pair resolved to ``model_id`` by
    the service; ``widget_id`` / ``step_id`` / ``mentions`` are runtime-only
    and not part of the authoring surface.
    """

    content: str
    mode: Optional[str] = None
    model: Optional[str] = None

    def to_prompt_schema(self, *, model_id: Optional[str]) -> PromptSchema:
        return PromptSchema(
            content=self.content,
            mode=self.mode or "chat",
            model_id=model_id,
        )


class TurnYaml(BaseModel):
    prompt: PromptYaml


class CaseYaml(BaseModel):
    name: str
    prompt: Optional[PromptYaml] = None
    turns: Optional[List[TurnYaml]] = None
    data_source_slugs: Optional[List[str]] = None
    expectations: ExpectationsSpec = Field(default_factory=ExpectationsSpec)

    @model_validator(mode="after")
    def _one_of_prompt_or_turns(self) -> "CaseYaml":
        has_prompt = self.prompt is not None
        has_turns = bool(self.turns)
        if has_prompt == has_turns:
            raise ValueError(
                f"case '{self.name}': exactly one of 'prompt' or 'turns' is required"
            )
        return self

    def is_multi_turn(self) -> bool:
        return bool(self.turns)


class SuiteYaml(BaseModel):
    name: str
    description: Optional[str] = None
    data_source_slugs: List[str] = Field(default_factory=list)
    cases: List[CaseYaml]

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SuiteYaml":
        return cls.model_validate(raw)
