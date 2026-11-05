# Audit Log Model
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

from sqlalchemy import BigInteger, Column, Integer, String, DateTime, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

from app.models.base import Base


class AuditLog(Base):
    """
    Audit log entry for tracking user actions.
    Enterprise feature - requires active license.
    """
    __tablename__ = "audit_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    organization_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Action details
    action = Column(String(100), nullable=False)  # e.g., "report.created", "user.login"
    resource_type = Column(String(50), nullable=True)  # e.g., "report", "data_source"
    resource_id = Column(String(36), nullable=True)

    # Additional context
    details = Column(JSON, nullable=True)  # Flexible JSON for additional info

    # Request metadata
    ip_address = Column(String(45), nullable=True)  # IPv4 or IPv6
    user_agent = Column(String(500), nullable=True)

    # recording time (when the row was written); the event time is occurred_at
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # --- contract v2 (docs/design/audit-event-contract.md); all nullable so
    # rows written before the columns existed stay valid and are exported as
    # legacy (schema_version NULL). ---
    # When the action happened, UTC naive like created_at. Queued or replayed
    # writes keep the original time here while created_at records the write.
    occurred_at = Column(DateTime, nullable=True)
    outcome = Column(String(16), nullable=True)  # success | failure | denied
    failure_reason = Column(String(255), nullable=True)  # safe, non-sensitive reason code
    request_id = Column(String(128), nullable=True)  # opaque per-request correlation
    session_id = Column(String(64), nullable=True)  # opaque per-login correlation (JWT sid claim)
    actor_type = Column(String(16), nullable=True)  # user | agent | service | system | unknown
    # Identity at event time: {"email", "name"} (user), {"service_account"} ...
    # Survives renames and deletion of the live row.
    actor_snapshot = Column(JSON, nullable=True)
    schema_version = Column(Integer, nullable=True)

    # Visibility-ordered sequence stamped by the log-stream exporter once the
    # row is committed (app/ee/audit/streams/exporter.py). Null until then.
    export_seq = Column(BigInteger, nullable=True)

    # Relationships
    organization = relationship("Organization", backref="audit_logs")
    user = relationship("User", backref="audit_logs")

    # Indexes for common queries
    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_user", "user_id"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_org_export_seq", "organization_id", "export_seq"),
        Index("ix_audit_logs_request", "request_id"),
        Index("ix_audit_logs_org_occurred", "organization_id", "occurred_at"),
    )

    def __repr__(self):
        return f"<AuditLog(id={self.id}, action={self.action}, user_id={self.user_id})>"
