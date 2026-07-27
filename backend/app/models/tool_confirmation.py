from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, JSON, String

from app.models.base import BaseSchema


class ToolConfirmation(BaseSchema):
    """A mid-run approval request ('ask' policy) and the user's decision.

    The row — not an in-process dict — is the source of truth, because the run
    that pauses and the HTTP request that answers land on *different uvicorn
    workers*: a worker that never registered the confirmation cannot see it, so
    an in-memory registry made the approval buttons 404 and the run hang until
    its timeout. The waiting tool polls this row, so any worker can resolve it.

    ``status`` transitions pending → approved | denied | expired, once. The row
    is also what lets a reloaded page (or a second device) find a still-pending
    approval for a run.
    """

    __tablename__ = "tool_confirmations"
    __table_args__ = (
        Index("ix_tool_confirmations_status_expires", "status", "expires_at"),
    )

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DENIED = "denied"
    STATUS_EXPIRED = "expired"

    # Discriminator so other confirmation flows can reuse the table.
    kind = Column(String, nullable=False, default="mcp_tool_policy")
    status = Column(String, nullable=False, default=STATUS_PENDING, index=True)

    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=True, index=True
    )
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=True, index=True)
    # The completions the approval card may be addressed to (the run's system
    # completion and the user's head completion). Kept as plain columns rather
    # than a FK-less JSON list so the authorization check stays queryable.
    system_completion_id = Column(String(36), nullable=True, index=True)
    head_completion_id = Column(String(36), nullable=True, index=True)

    # Only this user may answer (the run's requesting user).
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)

    connection_id = Column(String(36), nullable=True)
    connection_tool_id = Column(String(36), nullable=True, index=True)
    tool_name = Column(String, nullable=True)
    arguments = Column(JSON, nullable=True)

    # The decision.
    remember = Column(Boolean, nullable=False, default=False)
    resolved_by_user_id = Column(String(36), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    @property
    def is_pending(self) -> bool:
        return self.status == self.STATUS_PENDING

    @property
    def approved(self) -> bool:
        return self.status == self.STATUS_APPROVED
