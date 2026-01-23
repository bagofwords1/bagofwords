from sqlalchemy import Column, String, ForeignKey, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


from app.models.report_data_source_association import report_data_source_association
from app.models.dashboard_layout_version import DashboardLayoutVersion  # noqa: F401

class Report(BaseSchema):
    __tablename__ = 'reports'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    report_type = Column(String, nullable=False, default='regular', index=True)
    theme_name = Column(String, nullable=True, default=None)
    theme_overrides = Column(JSON, nullable=True, default=dict)
    mode = Column(String, nullable=False, default='chat')  # 'chat' | 'deep' | 'training'
    
    #privacy = Column(String, nullable=False, default='private') # private, internal, public
    cron_schedule = Column(String, nullable=True)
    
    # Conversation sharing (separate from dashboard publishing)
    conversation_share_token = Column(String, nullable=True, unique=True, index=True)
    conversation_share_enabled = Column(Boolean, default=False, nullable=False)

    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    user = relationship("User", back_populates="reports", lazy="selectin")

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    organization = relationship("Organization", back_populates="reports")

    external_platform_id = Column(String(36), ForeignKey('external_platforms.id'), nullable=True, index=True, default=None)
    external_platform = relationship("ExternalPlatform", back_populates="reports", lazy="selectin")

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