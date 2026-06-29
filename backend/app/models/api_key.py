from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ApiKey(BaseSchema):
    __tablename__ = "api_keys"

    # The principal the key authenticates as. For a personal key this is the
    # human owner; for a service-account key it is the SA's backing users row.
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    # Set when the key belongs to a service account (NULL for personal keys).
    # Auth resolution does not need this (it goes via user_id); it scopes
    # listing/management of a service account's keys.
    service_account_id = Column(String(36), ForeignKey("service_accounts.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)  # e.g., "MCP Integration", "CI Pipeline"
    key_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256 hash
    key_prefix = Column(String(12), nullable=False)  # First 12 chars for identification (e.g., "bow_abc123...")
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="api_keys")
    organization = relationship("Organization")
    service_account = relationship("ServiceAccount")
