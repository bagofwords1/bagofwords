from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime

from app.models.base import BaseSchema


class TableFeedbackEvent(BaseSchema):
    __tablename__ = "table_feedback_events"

    org_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=True)
    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=True)
    step_id = Column(String(36), ForeignKey("steps.id"), nullable=True)

    completion_feedback_id = Column(String(36), ForeignKey("completion_feedbacks.id"), nullable=False)

    table_fqn = Column(Text, nullable=False)
    datasource_table_id = Column(String(36), ForeignKey("datasource_tables.id"), nullable=True)

    # 'positive' | 'negative' as plain text to keep SQLite/Postgres compatible and avoid enums
    feedback_type = Column(String(16), nullable=False)

    created_at_event = Column(DateTime, nullable=False, default=datetime.utcnow)

    data_source = relationship("DataSource", back_populates="table_feedback_events")
    datasource_table = relationship("DataSourceTable", back_populates="feedback_events")

    __table_args__ = (
        Index("ix_feedback_org_report_table_time", "org_id", "report_id", "table_fqn", "created_at_event"),
        Index("ix_feedback_org_report_ds_time", "org_id", "report_id", "data_source_id", "created_at_event"),
        Index("ix_feedback_table_time", "table_fqn", "created_at_event"),
    )

