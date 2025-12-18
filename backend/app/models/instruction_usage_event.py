from sqlalchemy import Column, String, Text, DateTime, JSON, ForeignKey, Index, Float
from sqlalchemy.orm import relationship
from datetime import datetime

from app.models.base import BaseSchema


class InstructionUsageEvent(BaseSchema):
    __tablename__ = "instruction_usage_events"

    org_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=True)
    instruction_id = Column(String(36), ForeignKey("instructions.id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    # How it was loaded
    load_mode = Column(String(20), nullable=False)       # 'always' | 'intelligent'
    load_reason = Column(String(50), nullable=True)      # 'always' | 'search_match' | 'mentioned'
    search_score = Column(Float, nullable=True)          # Relevance score if intelligent
    search_query_keywords = Column(JSON, nullable=True)  # Keywords that matched

    # Instruction metadata at time of use (denormalized for analytics)
    source_type = Column(String(32), nullable=True)      # 'user' | 'git' | 'ai'
    category = Column(String(50), nullable=True)
    title = Column(String(255), nullable=True)

    # User/role weighting
    user_role = Column(String(64), nullable=True)
    role_weight = Column(Float, nullable=True)

    used_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    instruction = relationship("Instruction", back_populates="usage_events")

    __table_args__ = (
        Index("ix_inst_usage_org_inst_time", "org_id", "instruction_id", "used_at"),
        Index("ix_inst_usage_report_time", "report_id", "used_at"),
        Index("ix_inst_usage_load_mode", "org_id", "load_mode", "used_at"),
    )
