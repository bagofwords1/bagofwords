from sqlalchemy import Column, String, Text, DateTime, BigInteger, ForeignKey, Index, Float
from datetime import datetime

from app.models.base import BaseSchema


class TableStats(BaseSchema):
    __tablename__ = "table_stats"

    org_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    # NULL report_id row is the org-wide rollup
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=True)

    # Explicit data source linkage for faster joins and filtering
    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=True)

    table_fqn = Column(Text, nullable=False)
    datasource_table_id = Column(String(36), ForeignKey("datasource_tables.id"), nullable=True)

    # Total attempts (success + failure)
    usage_count = Column(BigInteger, nullable=False, default=0)
    # Successful attempts
    success_count = Column(BigInteger, nullable=False, default=0)
    weighted_usage_count = Column(Float, nullable=False, default=0.0)

    pos_feedback_count = Column(BigInteger, nullable=False, default=0)
    neg_feedback_count = Column(BigInteger, nullable=False, default=0)
    weighted_pos_feedback = Column(Float, nullable=False, default=0.0)
    weighted_neg_feedback = Column(Float, nullable=False, default=0.0)

    unique_users = Column(BigInteger, nullable=False, default=0)
    trusted_usage_count = Column(BigInteger, nullable=False, default=0)
    failure_count = Column(BigInteger, nullable=False, default=0)

    last_used_at = Column(DateTime, nullable=True)
    last_feedback_at = Column(DateTime, nullable=True)
    updated_at_stats = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        # Composite uniqueness for fast upserts per scope (now includes data_source_id)
        Index("ix_tabstats_org_report_ds_table", "org_id", "report_id", "data_source_id", "table_fqn", unique=True),
        Index("ix_tabstats_table", "table_fqn"),
        Index("ix_tabstats_dstbl", "datasource_table_id"),
    )

