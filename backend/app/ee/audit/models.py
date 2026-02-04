# Audit Log Model
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from sqlalchemy import Column, String, DateTime, ForeignKey, JSON, Index
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
    organization_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
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

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    organization = relationship("Organization", backref="audit_logs")
    user = relationship("User", backref="audit_logs")

    # Indexes for common queries
    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_user", "user_id"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    def __repr__(self):
        return f"<AuditLog(id={self.id}, action={self.action}, user_id={self.user_id})>"
