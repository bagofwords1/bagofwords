from datetime import datetime
from sqlalchemy import Column, String, ForeignKey, Boolean, JSON, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


from app.models.report_data_source_association import report_data_source_association
from app.models.dashboard_layout_version import DashboardLayoutVersion  # noqa: F401
from app.models.report_share import ReportShare  # noqa: F401
from app.models.report_star import ReportStar  # noqa: F401

class Report(BaseSchema):
    __tablename__ = 'reports'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    report_type = Column(String, nullable=False, default='regular', index=True)
    theme_name = Column(String, nullable=True, default=None)
    theme_overrides = Column(JSON, nullable=True, default=dict)
    mode = Column(String, nullable=False, default='chat')  # 'chat' | 'deep' | 'training'
    
    # Sharing visibility: 'none' | 'shared' | 'internal' | 'public'
    # 'none' = only owner, 'shared' = specific users, 'internal' = org, 'public' = anyone
    artifact_visibility = Column(String, nullable=False, default='none', server_default='none')
    conversation_visibility = Column(String, nullable=False, default='none', server_default='none')

    cron_schedule = Column(String, nullable=True)
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