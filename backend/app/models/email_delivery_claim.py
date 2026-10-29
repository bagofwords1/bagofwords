from sqlalchemy import Column, ForeignKey, String, UniqueConstraint

from .base import BaseSchema


class EmailDeliveryClaim(BaseSchema):
    """Durable, per-execution claim made before an outbound agent email."""

    __tablename__ = "email_delivery_claims"
    __table_args__ = (
        UniqueConstraint("agent_execution_id", "recipient", name="uq_email_delivery_run_recipient"),
    )

    agent_execution_id = Column(
        String(36), ForeignKey("agent_executions.id"), nullable=False, index=True
    )
    recipient = Column(String(320), nullable=False)
