"""OAuth 2.1 Authorization Server models.

Used when BOW acts as an OAuth Authorization Server for external apps
(e.g., Claude Web for the MCP connector). Not to be confused with OAuthAccount,
which stores BOW's OAuth *client* credentials for SSO login providers.

Client configuration belongs to a ConnectedApplication. Grant/token tables keep
their historical names and public client identifiers for compatibility.
"""

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.models.base import Base, BaseSchema
from app.models.connected_application import ApplicationConfiguration
from app.models.host_application import HostApplication  # noqa: F401  (FK target)


class OAuthClient(ApplicationConfiguration, Base):
    """A registered OAuth client that can request access to a protected resource."""
    __tablename__ = "application_oauth_config"

    client_id = Column(String(64), nullable=False, unique=True, index=True)
    client_secret_hash = Column(String(64), nullable=False)  # SHA-256
    redirect_uris = Column(Text, nullable=False)  # JSON array of allowed redirect URIs
    scopes = Column(String(255), nullable=False)
    entra_exchange = Column(JSON, nullable=True)
    trusted = Column(Boolean, nullable=False, default=False, server_default="false")


class OAuthAuthorizationCode(BaseSchema):
    """Short-lived authorization code issued during the OAuth consent flow."""
    __tablename__ = "oauth_mcp_authorization_codes"

    code = Column(String(128), nullable=False, unique=True, index=True)
    client_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    redirect_uri = Column(String(2048), nullable=False)
    scope = Column(String(255), nullable=False)
    code_challenge = Column(String(128), nullable=False)  # PKCE S256
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User")
    organization = relationship("Organization")


class OAuthAccessToken(BaseSchema):
    """Access and refresh tokens issued to OAuth clients."""
    __tablename__ = "oauth_mcp_access_tokens"

    token_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256
    client_id = Column(String(64), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    scope = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    exchange_context = Column(JSON, nullable=True)
    # Embed sessions (scope "embed", prefix bow_embed_): the host application
    # they were issued for. NULL for ordinary OAuth clients.
    host_application_id = Column(String(36), ForeignKey("application_embedding_config.id", ondelete="CASCADE"),
                                 nullable=True, index=True)
    refresh_token_hash = Column(String(64), nullable=True, unique=True, index=True)  # SHA-256
    refresh_expires_at = Column(DateTime, nullable=True)

    user = relationship("User")
    organization = relationship("Organization")
