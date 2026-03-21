# SCIM Token Model
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from sqlalchemy import Column, String, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ScimToken(BaseSchema):
    """
    Bearer token for SCIM 2.0 provisioning.
    Scoped to an organization, used by identity providers (Okta, Azure AD, etc.).
    """
    __tablename__ = "scim_tokens"

    organization_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # e.g., "Okta SCIM", "Azure AD"
    token_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256 hash
    token_prefix = Column(String(16), nullable=False)  # First 16 chars for display
    created_by_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    organization = relationship("Organization")
    created_by = relationship("User")
