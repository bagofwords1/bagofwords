from sqlalchemy import Column, String, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime

from app.models.base import BaseSchema


class InstructionFeedbackEvent(BaseSchema):
    __tablename__ = "instruction_feedback_events"

    org_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=True)
    instruction_id = Column(String(36), ForeignKey("instructions.id"), nullable=False)

    # Link to the completion feedback (thumbs up/down on AI output)
    completion_feedback_id = Column(String(36), ForeignKey("completion_feedbacks.id"), nullable=False)

    # 'positive' | 'negative' as plain text to keep SQLite/Postgres compatible
    feedback_type = Column(String(16), nullable=False)

    created_at_event = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    instruction = relationship("Instruction", back_populates="feedback_events")

    __table_args__ = (
        Index("ix_inst_fb_org_inst_time", "org_id", "instruction_id", "created_at_event"),
        Index("ix_inst_fb_completion", "completion_feedback_id"),
    )
