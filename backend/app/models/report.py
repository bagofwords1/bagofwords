from datetime import datetime
from sqlalchemy import Column, String, ForeignKey, Boolean, JSON, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


from app.models.report_data_source_association import report_data_source_association
from app.models.dashboard_layout_version import DashboardLayoutVersion  # noqa: F401
from app.models.report_share import ReportShare  # noqa: F401
from app.models.report_star import ReportStar  # noqa: F401
from app.models.project import Project  # noqa: F401

class Report(BaseSchema):
    __tablename__ = 'reports'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    report_type = Column(String, nullable=False, default='regular', index=True)
    theme_name = Column(String, nullable=True, default=None)
    theme_overrides = Column(JSON, nullable=True, default=dict)
    mode = Column(String, nullable=False, default='chat')  # 'chat' | 'training'
    # Report-level LLM override. Soft reference to llm_models.id (no FK, same
    # convention as prompt.model_id / membership.default_llm_model_id): a stale
    # value — model disabled/restricted/deleted after being picked, or set by a
    # teammate with access the current user lacks — degrades silently to the
    # user/org default at resolve time and never breaks chat. null = fall back
    # to the user preference, then the org default. Precedence when running a
    # completion: prompt.model_id (explicit per-message) > report.model_id >
    # user default > org default.
    model_id = Column(String(36), nullable=True)
    
    # Sharing visibility: 'none' | 'shared' | 'internal' | 'public'
    # 'none' = only owner, 'shared' = specific users, 'internal' = org, 'public' = anyone
    artifact_visibility = Column(String, nullable=False, default='none', server_default='none')
    conversation_visibility = Column(String, nullable=False, default='none', server_default='none')

    # Agent focus (subset of the attached data_sources whose FULL schema is
    # rendered into the planner context). null/empty = no explicit focus: the
    # planner renders all attached agents when few, or auto-seeds a focus from
    # per-user usage when many (see agent_v2 schema assembly). Written by the
    # set_report_agents tool and the prompt-box focus selector; the attached
    # roster (report.data_sources) always stays visible as a thin index.
    focused_data_source_ids = Column(JSON, nullable=True, default=None)

    # Whose data-source credentials a shared-artifact viewer's "Run" uses:
    # 'viewer' = the viewer's own credentials, 'creator' = on behalf of the
    # report owner. Results always land in the viewer's step_user_results
    # rows — never in the shared Step.data snapshot.
    shared_run_identity = Column(String, nullable=False, default='viewer', server_default='viewer')

    # Show the Data tab (the per-query execution history behind the report) to
    # viewers of the shared artifact page /r/{id}. Artifact sharing only.
    # Display-only: the public queries/step endpoints serve the same payload
    # either way, because they also feed the artifact iframe itself — hiding
    # the tab is a presentation choice, not a data boundary.
    include_data_tab = Column(Boolean, nullable=False, default=True, server_default='1')

    # Chat on the shared artifact page /r/{id}. Off by default; only meaningful
    # while artifact_visibility != 'none'. Viewer conversations never touch this
    # report's own transcript — each viewer gets a hidden child report
    # (report_type='artifact_chat', forked_from_id=this) holding their thread.
    artifact_chat_enabled = Column(Boolean, nullable=False, default=False, server_default='0')
    # Which agents (data sources) artifact-page chat may query, chosen by the
    # owner. null = inherit this report's attached roster; [] = no agents
    # (answers come from the artifact's own data only); a list = explicit
    # subset. Never Auto: a viewer's chat can only narrow from here — the
    # effective set is this ∩ the viewer's own accessible agents.
    artifact_chat_data_source_ids = Column(JSON, nullable=True, default=None)

    cron_schedule = Column(String, nullable=True)
    # Rerun the artifact's queries when a viewer opens the shared report page
    # (/r/{id}). Independent of cron_schedule — a report can refresh on view
    # without being on a schedule, and turning the schedule off must not
    # silently disable this. Rate limiting is the hardcoded staleness gate in
    # ReportService.refresh_on_view_rerun (see REFRESH_ON_VIEW_MIN_INTERVAL_SECONDS):
    # because the interval is not owner-configurable, the gate doubles as the
    # ceiling — at most one rerun per report per interval regardless of traffic.
    # Default ON: artifacts open on fresh data unless the owner opts out (or
    # picks a recurring schedule — the UI treats the modes as exclusive).
    refresh_on_view = Column(Boolean, nullable=False, default=True, server_default='1')
    last_run_at = Column(DateTime, nullable=True, default=None)
    # Last conversation activity: bumped when a new user message is created and
    # when an agent turn finalizes. Distinct from `updated_at` (which bumps on any
    # report-row metadata edit) so the report list can sort by real chat activity.
    # See ReportService.get_reports ordering and the bump points in
    # completion_service / agent_v2.
    last_activity_at = Column(DateTime, nullable=True, default=datetime.utcnow, index=True)
    # Subscribers notified after each scheduled rerun
    # Format: [{"type": "user", "id": "..."}, {"type": "email", "address": "..."}]
    notification_subscribers = Column(JSON, nullable=True, default=None)
    
    # Conversation sharing (separate from dashboard publishing)
    conversation_share_token = Column(String, nullable=True, unique=True, index=True)
    conversation_share_enabled = Column(Boolean, default=False, nullable=False)

    # Fork lineage
    forked_from_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    forked_from = relationship("Report", remote_side="Report.id", lazy="selectin")

    # Project membership (shared folder). NULL = personal root list. Moving a
    # report between projects is just an update of this column.
    project_id = Column(String(36), ForeignKey('projects.id'), nullable=True, index=True)
    project = relationship("Project", back_populates="reports", lazy="joined")  # to-one: fold into parent query

    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    user = relationship("User", back_populates="reports", lazy="joined")  # to-one: fold into parent query

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    organization = relationship("Organization", back_populates="reports")

    external_platform_id = Column(String(36), ForeignKey('external_platforms.id'), nullable=True, index=True, default=None)
    external_platform = relationship("ExternalPlatform", back_populates="reports", lazy="joined")  # to-one: fold into parent query

    # Trigger provenance: set when this report was spawned by a standalone
    # trigger webhook delivery. Plain string (no FK constraint) to avoid a
    # circular FK with webhooks.report_id; powers the ⚡ origin indicator and
    # per-trigger run history.
    webhook_id = Column(String(36), nullable=True, index=True, default=None)
    # Scheduled-run provenance: set when this report was spawned by a
    # scheduled prompt with spawn_new_report routing (report per run). Plain
    # string like webhook_id; powers the 🕐 origin indicator.
    scheduled_prompt_id = Column(String(36), nullable=True, index=True, default=None)

    widgets = relationship("Widget", back_populates="report", lazy="selectin")
    text_widgets = relationship("TextWidget", back_populates="report", lazy="selectin")
    completions = relationship("Completion", back_populates="report", lazy="selectin")
    dashboard_layout_versions = relationship("DashboardLayoutVersion", back_populates="report", lazy="selectin")
    files = relationship("File", secondary="report_file_association", back_populates="reports", lazy="selectin")
    data_sources = relationship(
        "DataSource", 
        secondary="report_data_source_association", 
        back_populates="reports", 
        lazy="selectin", 
        overlaps="git_repository,organization"
    )
    queries = relationship("Query", back_populates="report", lazy="selectin")
    visualizations = relationship("Visualization", back_populates="report", lazy="selectin")
    artifacts = relationship("Artifact", back_populates="report", lazy="selectin")
    scheduled_prompts = relationship("ScheduledPrompt", back_populates="report", lazy="selectin")
    shares = relationship("ReportShare", back_populates="report", lazy="selectin")
    stars = relationship("ReportStar", back_populates="report", lazy="selectin")