from pydantic import BaseModel, Field, model_validator, field_validator, field_serializer
from typing import List, Optional
from datetime import datetime
from app.schemas.view_schema import ViewSchema

class StepBase(BaseModel):
    title: str
    slug: str
    status: str
    status_reason: Optional[str] = None
    prompt: str
    # Optional so code visibility can redact it to None. Redaction never uses
    # "" — an empty string is indistinguishable from a step that genuinely has
    # no code, which would make "withheld" untestable.
    code: Optional[str] = None
    description: Optional[str] = ""
    

class StepSchema(StepBase):
    id: str
    created_at: datetime
    type: str
    query_id: str
    data: dict = Field(default_factory=dict)
    data_model: dict = Field(default_factory=dict)
    view: Optional[ViewSchema] = Field(default_factory=ViewSchema)
    created_entity_id: Optional[str] = None  # ID of entity created from this step
    # Set when `data` comes from the requesting viewer's own run
    # (step_user_results) instead of the shared Step.data snapshot.
    viewer_result: Optional[dict] = None
    # True when the shared snapshot was hidden from this viewer (viewer-identity
    # mode on user-scoped connections) — `data` is empty until they run.
    snapshot_withheld: bool = False
    # The resolved param values this step's snapshot was produced with.
    applied_params: Optional[dict] = None

    class Config:
        from_attributes = True

    @field_validator("data", "data_model", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v if v is not None else {}

    @model_validator(mode="after")
    def _ensure_view(self) -> "StepSchema":
        if self.view is None:
            self.view = ViewSchema()
        return self

    @field_serializer("data")
    def _redact_data_for_display(self, data, _info):
        """Mask PII in result data when this step is serialized to the frontend.
        No-op unless a request-scoped display redactor is active (set by the
        PII display middleware), so internal ``.data`` access stays real."""
        from app.ai.llm.pii.display import redact_grid_display
        return redact_grid_display(data)

    @field_serializer("code")
    def _redact_code_for_display(self, code, _info):
        """Withhold generated code from callers without ``view_code``.

        Serializer-level (not call-site-level) on purpose: steps are serialized
        from completions, queries, widgets and reports, and a check at each of
        those is a check that can be forgotten. Internal ``.code`` access —
        execution, exports, the agent itself — is untouched.
        """
        from app.core.code_visibility import code_visible_now
        return code if code_visible_now() else None

class StepCreate(StepBase):
    widget_id: str
    data: dict = Field(default_factory=dict)
    data_model: dict = Field(default_factory=dict)
    view: Optional[ViewSchema] = Field(default_factory=ViewSchema)

    @field_validator("data", "data_model", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v if v is not None else {}

    @model_validator(mode="after")
    def _ensure_view(self) -> "StepCreate":
        if self.view is None:
            self.view = ViewSchema()
        return self

class StepUpdate(StepBase):
    pass


class PublicStepSchema(BaseModel):
    """Minimal schema for public/unauthenticated access to published reports."""
    id: str
    title: str
    type: str
    code: Optional[str] = None
    data_model: dict = Field(default_factory=dict)
    data: dict = Field(default_factory=dict)
    view: Optional[dict] = Field(default_factory=dict)
    # Set when `data` comes from the requesting viewer's own run
    # (step_user_results) instead of the shared Step.data snapshot.
    viewer_result: Optional[dict] = None
    # True when the shared snapshot was hidden from this viewer (viewer-identity
    # mode on user-scoped connections) — `data` is empty until they run.
    snapshot_withheld: bool = False
    # Param values the snapshot was materialized with — lets the host's
    # snapshot-fallback options tier skip data a param already filtered.
    applied_params: Optional[dict] = None

    class Config:
        from_attributes = True

    @field_validator("data", "data_model", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v if v is not None else {}

    @field_serializer("code")
    def _redact_code_for_display(self, code, _info):
        """Same gate as StepSchema. Anonymous viewers of a published report hold
        no role, so the public report path sets the decision explicitly rather
        than inheriting the deny default (see report_service)."""
        from app.core.code_visibility import code_visible_now
        return code if code_visible_now() else None

