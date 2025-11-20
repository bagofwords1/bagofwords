from sqlalchemy import Column, String, Text, DateTime, JSON, Boolean, ForeignKey, UniqueConstraint, Index, Float
from sqlalchemy.orm import relationship
from datetime import datetime

from app.models.base import BaseSchema


class TableUsageEvent(BaseSchema):
    __tablename__ = "table_usage_events"

    org_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=True)
    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=True)
    step_id = Column(String(36), ForeignKey("steps.id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    # Natural, durable identity of a table across schema refreshes
    table_fqn = Column(Text, nullable=False)  # e.g., "<data_source_id>.Account"

    # Optional enrichment link to the current schema row
    datasource_table_id = Column(String(36), ForeignKey("datasource_tables.id"), nullable=True)

    # Source metadata
    source_type = Column(String(32), nullable=False)  # 'sql' | 'excel' | 'pandas' | 'api'
    columns = Column(JSON, nullable=True)  # list of column names as strings
    success = Column(Boolean, nullable=False, default=True)

    # User/role weighting captured at write-time for reproducibility
    user_role = Column(String(64), nullable=True)
    role_weight = Column(Float, nullable=True)

    used_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    datasource_table = relationship("DataSourceTable", back_populates="usage_events")

    __table_args__ = (
        # Prevent duplicate usage for the same step-table pair
        UniqueConstraint("step_id", "table_fqn", name="uq_usage_step_table"),
        # Common query patterns
        Index("ix_usage_org_report_table_time", "org_id", "report_id", "table_fqn", "used_at"),
        Index("ix_usage_org_report_ds_time", "org_id", "report_id", "data_source_id", "used_at"),
        Index("ix_usage_table_time", "table_fqn", "used_at"),
    )

