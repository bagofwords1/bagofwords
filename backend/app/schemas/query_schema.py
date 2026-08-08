from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from app.schemas.visualization_schema import VisualizationSchema, PublicVisualizationSchema
from app.schemas.step_schema import StepSchema


class QueryCreate(BaseModel):
    title: str
    description: Optional[str] = None
    report_id: Optional[str] = None
    widget_id: Optional[str] = None
    organization_id: Optional[str] = None
    user_id: Optional[str] = None


class QuerySchema(BaseModel):
    id: str
    title: str
    report_id: Optional[str] = None
    widget_id: str
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    default_step_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    visualizations: Optional[list[VisualizationSchema]] = None
    default_step: Optional[StepSchema] = None
    # Row/column security. The flags are exposed so the editor can show a
    # "protected" badge without a second request; the policy bodies are NOT —
    # they name groups, roles and attribute values, which is more than a
    # plain viewer of the report needs to know.
    rls_enabled: bool = False
    cls_enabled: bool = False

    class Config:
        from_attributes = True


class QueryRunRequest(BaseModel):
    code: str
    title: Optional[str] = None
    data_model: Optional[dict] = None
    type: Optional[str] = None
    row_limit: Optional[int] = None
    tool_execution_id: Optional[str] = None


class AccessPolicyUpdate(BaseModel):
    """Row- and column-level security for one query, as the editor sends it.

    Row and column security are set together in one call because they are one
    decision in the UI ("who sees what in this query") and a partial update
    would let a client clear one while meaning only to change the other.
    """
    rls_enabled: bool = False
    rls_mode: Optional[str] = "attribute"
    rls_policy: Optional[dict] = None
    rls_default_deny: bool = True
    cls_enabled: bool = False
    cls_policy: Optional[dict] = None


class AccessPolicyPreviewRequest(BaseModel):
    """Preview a policy — saved or not — as a specific member would see it."""
    target_user_id: str
    # Unsaved edits, applied over the stored row for this preview only.
    rls_enabled: Optional[bool] = None
    rls_mode: Optional[str] = None
    rls_policy: Optional[dict] = None
    rls_default_deny: Optional[bool] = None
    cls_enabled: Optional[bool] = None
    cls_policy: Optional[dict] = None


class PublicQuerySchema(BaseModel):
    """Minimal schema for public/unauthenticated access to published reports."""
    id: str
    title: str
    default_step_id: Optional[str] = None
    visualizations: Optional[List[PublicVisualizationSchema]] = None

    class Config:
        from_attributes = True


