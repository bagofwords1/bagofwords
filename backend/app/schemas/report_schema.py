from pydantic import BaseModel, field_validator
from typing import List, Optional, Literal
from .widget_schema import WidgetSchema
from app.schemas.user_schema import UserSchema
from datetime import datetime
from app.schemas.data_source_schema import DataSourceReportSchema
from app.schemas.external_platform_schema import ExternalPlatformSchema
from app.schemas.dashboard_layout_version_schema import DashboardLayoutVersionSchema
from app.schemas.project_schema import ProjectMiniSchema

class ReportBase(BaseModel):
    title: Optional[str] = None

class ReportCreate(ReportBase):
    files: Optional[List[str]] = []
    data_sources: Optional[List[str]] = []
    external_platform_id: Optional[str] = None
    # Create the report directly inside a project (folder). Validated against
    # the creator's project access at create time.
    project_id: Optional[str] = None

class ReportUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[Literal["draft", "published", "archived"]] = None
    theme_name: Optional[str] = None
    theme_overrides: Optional[dict] = None
    cron_schedule: Optional[str] = None
    data_sources: Optional[List[str]] = None
    # Agent focus: subset of attached data_sources whose full schema is rendered.
    # Omit to leave unchanged; send [] to clear (revert to auto roster/seed).
    focused_data_source_ids: Optional[List[str]] = None
    mode: Optional[Literal["chat", "training"]] = None
    # Report-level LLM override. Sentinel-aware: omit to leave unchanged, send a
    # model id to set, send "" (empty string) to clear back to user/org default.
    model_id: Optional[str] = None
    # Project membership. Sentinel-aware like model_id: omit to leave unchanged,
    # send a project id to move into that project, send "" to move back to the
    # personal root list.
    project_id: Optional[str] = None

class ReportSchema(ReportBase):
    class PublicGeneralSettings(BaseModel):
        ai_analyst_name: str = "AI Analyst"
        bow_credit: bool = True
        icon_url: Optional[str] = None

    id: str
    status: Literal["draft", "published", "archived"]
    slug: str
    report_type: Literal["regular", "test", "artifact_chat"]
    widgets: List[WidgetSchema] = []
    dashboard_layout_versions: List[DashboardLayoutVersionSchema] = []
    data_sources: List[DataSourceReportSchema] = []
    external_platform: Optional[ExternalPlatformSchema] = None
    user: UserSchema
    created_at: datetime
    updated_at: datetime
    last_activity_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    cron_schedule: Optional[str] = None
    # Rerun this report's queries when a viewer opens /r/{id}. This schema also
    # serves the public GET /r/{id}, so the shared page reads the flag directly.
    # Default ON — artifacts open on fresh data unless the owner opts out.
    refresh_on_view: bool = True
    app_version: Optional[str] = None  # Version for routing decisions
    general: Optional[PublicGeneralSettings] = None
    theme_name: Optional[str] = None
    theme_overrides: Optional[dict] = None
    mode: Literal["chat", "training"] = "chat"

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_retired_mode(cls, v):
        """Map a retired mode (e.g. the removed 'deep') onto chat.

        This is a RESPONSE model, and it serializes every row of GET /reports —
        so one un-migrated row would otherwise 500 the entire list, not just
        that conversation. Writes stay strict: ReportUpdate.mode still rejects
        anything outside the Literal, so nothing can reintroduce a retired mode.
        """
        return v if v in ("chat", "training") else "chat"

    # Report-level LLM override (null = user/org default resolves at run time)
    model_id: Optional[str] = None
    # Agent focus: subset of attached agents whose full schema is in context.
    # null/empty = no explicit focus (planner renders all when few / auto-seeds when many).
    focused_data_source_ids: Optional[List[str]] = None
    # Conversation sharing
    conversation_share_enabled: bool = False
    conversation_share_token: Optional[str] = None
    # Sharing visibility
    artifact_visibility: Literal["none", "shared", "internal", "public"] = "none"
    conversation_visibility: Literal["none", "shared", "internal", "public"] = "none"
    # Whose credentials a shared-artifact viewer's "Run" uses ('viewer' | 'creator')
    shared_run_identity: Literal["viewer", "creator"] = "viewer"
    # Show the Data tab (the queries behind the report) to viewers of the
    # shared artifact page. Artifact sharing only.
    include_data_tab: bool = True
    # Chat on the shared artifact page /r/{id}. This schema also serves the
    # public GET /r/{id}, so the shared page reads the flag directly.
    artifact_chat_enabled: bool = False
    # Owner's agent allowlist for artifact-page chat: null = inherit the
    # report's attached roster, [] = dashboard data only, list = subset.
    artifact_chat_data_source_ids: Optional[List[str]] = None
    # True when the report reads an RLS-enabled relation: viewers always run
    # under their own identity and 'run on my behalf' (creator mode) is blocked.
    has_rls: bool = False
    # True when the report reads a user-scoped (user_required) source. The
    # share dialog only shows the run-identity toggle then — on system-only
    # credentials creator vs viewer identity resolves to the same credentials.
    has_user_scoped: bool = False
    artifact_shared_user_ids: List[str] = []
    conversation_shared_user_ids: List[str] = []
    artifact_shared_group_ids: List[str] = []
    conversation_shared_group_ids: List[str] = []
    # Artifact modes (page, slides) that exist for this report
    artifact_modes: List[str] = []
    # Thumbnail URL for the main artifact
    thumbnail_url: Optional[str] = None
    # Whether the current user has starred this report (per-user, list view)
    is_starred: bool = False
    # Scheduled rerun notification subscribers
    notification_subscribers: Optional[List[dict]] = None
    # Summary counts for list view
    query_count: int = 0
    artifact_count: int = 0
    has_scheduled_prompts: bool = False
    scheduled_prompt_count: int = 0
    instruction_count: int = 0
    webhook_count: int = 0
    # Trigger provenance: set when this report was spawned by a trigger
    # webhook delivery (powers the ⚡ indicator in the reports list).
    webhook_id: Optional[str] = None
    # Scheduled-run provenance: set when spawned by a scheduled prompt with
    # report-per-run routing (powers the 🕐 origin indicator).
    scheduled_prompt_id: Optional[str] = None
    # Fork lineage
    forked_from_id: Optional[str] = None
    forked_from_title: Optional[str] = None
    forked_from_user_name: Optional[str] = None
    # Project (folder) membership; project carries name/icon for the chip.
    project_id: Optional[str] = None
    project: Optional[ProjectMiniSchema] = None

    class Config:
        from_attributes = True

class ReportRecentSchema(BaseModel):
    """Schema for recent reports on home page."""
    id: str
    title: Optional[str]
    slug: str
    user_id: str
    user_name: Optional[str] = None
    is_published: bool = False
    has_artifact: bool = False
    artifact_mode: Optional[str] = None  # 'page' or 'slides' if has artifact
    conversation_share_enabled: bool = False
    conversation_share_token: Optional[str] = None
    artifact_visibility: str = "none"
    conversation_visibility: str = "none"
    thumbnail_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ReportRerunResultSchema(BaseModel):
    """Outcome of POST /reports/{id}/rerun — what actually ran, so clients
    can tell a refreshed dashboard from a silent no-op or a failed run."""
    message: str
    steps_total: int
    steps_succeeded: int
    steps_failed: int
    last_run_at: Optional[datetime] = None
    # True when a refresh-on-view request was declined (feature off, data still
    # fresh, or another refresh already in flight). Always False for an
    # explicit user-triggered rerun, which never gets rate limited.
    skipped: bool = False


VISIBILITY_LITERAL = Literal["none", "shared", "internal", "public"]


class ReportVisibilityUpdate(BaseModel):
    """Update visibility for either artifact or conversation sharing."""
    visibility: VISIBILITY_LITERAL
    shared_user_ids: Optional[List[str]] = None  # required when visibility == 'shared'
    # Group grants: every member of a listed group can view. None = leave
    # the current group shares unchanged (mirrors shared_user_ids semantics).
    shared_group_ids: Optional[List[str]] = None
    # Artifact sharing only: whose credentials viewer-triggered runs use.
    # Omitted = leave unchanged.
    run_identity: Optional[Literal["viewer", "creator"]] = None
    # Artifact sharing only: show viewers the Data tab on /r/{id}.
    # Omitted = leave unchanged (same semantics as run_identity).
    include_data_tab: Optional[bool] = None
    # Artifact sharing only: allow viewers of /r/{id} to chat with the report.
    # Omitted = leave unchanged.
    artifact_chat_enabled: Optional[bool] = None
    # Artifact sharing only: which agents chat may query. Sentinel-aware:
    # omitted = leave unchanged; [] = dashboard data only; ["*"] = reset to
    # inherit the report's attached roster (null); a list = explicit subset.
    artifact_chat_data_source_ids: Optional[List[str]] = None


class ViewerRunResultSchema(BaseModel):
    """Outcome of POST /r/{id}/run — a shared-artifact viewer's re-execution
    of the dashboard's queries into their own per-user result rows."""
    message: str
    steps_total: int
    steps_succeeded: int
    steps_failed: int
    executed_as: Literal["viewer", "creator"]
    last_run_at: Optional[datetime] = None
    # Data sources whose clients could not be constructed for this run
    # (e.g. the viewer has no stored credentials for a user_required source)
    data_source_errors: List[dict] = []


class ReportShareUserSchema(BaseModel):
    """A user who has been granted access to a report."""
    id: str
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    share_type: str
    created_at: datetime

    class Config:
        from_attributes = True


class PaginationMeta(BaseModel):
    total: int
    page: int
    limit: int
    total_pages: int
    has_next: bool
    has_prev: bool

class ReportListResponse(BaseModel):
    reports: List[ReportSchema]
    meta: PaginationMeta

class ReportActivitySchema(BaseModel):
    """Lightweight per-report status for list badges (sidebar, /reports, projects).

    ``state`` is the live activity of the conversation; ``unread`` / ``error``
    are viewer-relative flags the client combines with it (see precedence in
    ReportStatusDot). Kept intentionally tiny — this is polled/refetched far
    more often than the full ReportSchema.
    """
    id: str
    # awaiting_user: the run is paused on this user (clarify form or tool
    # confirmation). running: a system completion is in_progress. queued: a
    # user prompt is parked behind another run. idle: nothing live.
    state: Literal["awaiting_user", "running", "queued", "idle"]
    # True when last_activity_at is newer than this user's view watermark
    # (or they never opened the report).
    unread: bool
    # True when the latest system completion ended in error.
    error: bool
    last_activity_at: Optional[datetime] = None


class ReportActivityResponse(BaseModel):
    activity: List[ReportActivitySchema]
