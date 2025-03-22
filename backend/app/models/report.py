from sqlalchemy import Column, String, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


from app.models.report_data_source_association import report_data_source_association

class Report(BaseSchema):
    __tablename__ = 'reports'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    
    #privacy = Column(String, nullable=False, default='private') # private, internal, public
    cron_schedule = Column(String, nullable=True)

    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    user = relationship("User", back_populates="reports", lazy="selectin")

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    organization = relationship("Organization", back_populates="reports")

    widgets = relationship("Widget", back_populates="report", lazy="selectin")
    text_widgets = relationship("TextWidget", back_populates="report", lazy="selectin")
    completions = relationship("Completion", back_populates="report", lazy="selectin")
    files = relationship("File", secondary="report_file_association", back_populates="reports", lazy="selectin")
    data_sources = relationship(
        "DataSource", 
        secondary="report_data_source_association", 
        back_populates="reports", 
        lazy="selectin", 
        overlaps="git_repository,organization"
    )