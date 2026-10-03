"""Organization-bound federated identities and short-lived browser requests."""

from sqlalchemy import Column, String, DateTime, ForeignKey, UniqueConstraint
from app.models.base import BaseSchema


class SAMLIdentity(BaseSchema):
    __tablename__ = "saml_identities"
    identity_hash = Column(String(64), nullable=False, unique=True)
    provider = Column(String(48), nullable=False)
    issuer = Column(String(2048), nullable=False)
    subject = Column(String(2048), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    __table_args__ = (UniqueConstraint("provider", "organization_id", "user_id", name="uq_saml_user_provider_org"),)


class SAMLRequest(BaseSchema):
    __tablename__ = "saml_requests"
    request_id = Column(String(128), nullable=False, unique=True)
    provider = Column(String(48), nullable=False)
    config_hash = Column(String(64), nullable=False)
    relay_hash = Column(String(64), nullable=False, unique=True)
    browser_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    consumed_at = Column(DateTime, nullable=True)
    # Replay guard across requests/workers, bound to the trusted issuer.
    assertion_hash = Column(String(64), nullable=True, unique=True)
