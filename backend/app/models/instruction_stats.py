from sqlalchemy import Column, String, DateTime, BigInteger, ForeignKey, Index, Float
from sqlalchemy.orm import relationship
from datetime import datetime

from app.models.base import BaseSchema


class InstructionStats(BaseSchema):
    __tablename__ = "instruction_stats"

    org_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    # NULL report_id row is the org-wide rollup
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=True)
    instruction_id = Column(String(36), ForeignKey("instructions.id"), nullable=False)

    # Usage counts by load mode
    usage_count = Column(BigInteger, nullable=False, default=0)
    always_count = Column(BigInteger, nullable=False, default=0)
    intelligent_count = Column(BigInteger, nullable=False, default=0)
    mentioned_count = Column(BigInteger, nullable=False, default=0)

    # Weighted usage (by user role)
    weighted_usage_count = Column(Float, nullable=False, default=0.0)

    # Feedback counts
    pos_feedback_count = Column(BigInteger, nullable=False, default=0)
    neg_feedback_count = Column(BigInteger, nullable=False, default=0)
    weighted_pos_feedback = Column(Float, nullable=False, default=0.0)
    weighted_neg_feedback = Column(Float, nullable=False, default=0.0)

    # User engagement
    unique_users = Column(BigInteger, nullable=False, default=0)

    last_used_at = Column(DateTime, nullable=True)
    last_feedback_at = Column(DateTime, nullable=True)
    updated_at_stats = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    instruction = relationship("Instruction", back_populates="stats")

    __table_args__ = (
        Index("ix_inststats_org_report_inst", "org_id", "report_id", "instruction_id", unique=True),
        Index("ix_inststats_instruction", "instruction_id"),
        Index("ix_inststats_usage", "org_id", "usage_count"),
    )
