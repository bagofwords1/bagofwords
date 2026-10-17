"""Durable, report-scoped parent/child mailboxes. Messages never complete a run."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, UniqueConstraint

from app.models.base import BaseSchema


class SubAgentMessage(BaseSchema):
    __tablename__ = "sub_agent_messages"
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=False)
    wave_id = Column(String(36), ForeignKey("sub_agent_waves.id"), nullable=False)
    sub_agent_id = Column(String(36), ForeignKey("sub_agent_runs.id"), nullable=False)
    sender_sub_agent_id = Column(String(36), ForeignKey("sub_agent_runs.id"), nullable=True)
    recipient = Column(String(36), nullable=False)  # child UUID or 'parent'
    kind = Column(String(16), nullable=False)
    body = Column(Text, nullable=False)
    reply_to = Column(String(36), ForeignKey("sub_agent_messages.id"), nullable=True)
    idempotency_key = Column(String(160), nullable=False)
    status = Column(String(16), nullable=False, default="sent")
    delivered_at = Column(DateTime, nullable=True)
    reason = Column(String(80), nullable=True)
    __table_args__ = (
        UniqueConstraint("report_id", "idempotency_key", name="uq_sub_agent_message_send"),
        Index("ix_sub_agent_messages_inbox", "report_id", "recipient", "status"),
        Index("ix_sub_agent_messages_child", "sub_agent_id", "created_at"),
    )
