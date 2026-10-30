"""Host applications: external systems that embed BOW artifacts.

A host application is one registered system ("Northwind ERP - production")
that may frame artifacts and sign its users in to BOW. It is an organization
integration, managed next to OAuth apps (Settings -> Integrations), never one
per artifact. See docs/design/embedded-artifacts.md.

Trust model in one place:

* ``allowed_origins`` decide who may frame an artifact for this host (CSP
  ``frame-ancestors`` on ``/d/{ref}?host=``) and which parent window the
  embedded page accepts a handshake from.
* ``identity_mode`` decides how a viewer is signed in silently:
  - ``assertion``: the host's backend signs an RS256 JWT with a private key
    BOW generated and showed once (only ``public_key`` is stored);
  - ``idp_token``: the host passes the user's token from an OIDC provider
    BOW already trusts for login; the provider vouches, not the host;
  - ``sso``: the viewer signs in to BOW in a top-level popup (any login BOW
    supports), which hands a single-use code back to the frame.
* ``identity_source`` decides which BOW identity a host subject is
  (``oidc:<provider>``, ``saml:<provider>``, ``ldap``, ``scim``,
  ``member_attribute:<key>``). Never email.
* ``create_users`` lets a first-time viewer be admitted through that
  source's own admission rules (invitations / domain policy, SAML or LDAP
  auto-provisioning). Off by default.
* ``epoch`` is bumped on disable, key rotation and "sign everyone out"; every
  embed session records the epoch it was issued under.

Which artifacts may appear in a host is the artifact's side of the opt-in
(``ArtifactHostApplication``); both must agree, and the viewer must still
pass the artifact's normal sharing.
"""
from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema

IDENTITY_MODES = ("assertion", "idp_token", "sso")


class HostApplication(BaseSchema):
    __tablename__ = "host_applications"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    allowed_origins = Column(JSON, nullable=False, default=list)

    identity_mode = Column(String(16), nullable=False, default="assertion", server_default="assertion")
    identity_source = Column(String(80), nullable=True)
    create_users = Column(Boolean, nullable=False, default=False, server_default="0")
    # assertion mode: the ``iss`` the host puts in every assertion, and the
    # public half of the BOW-generated key pair.
    issuer = Column(String(255), nullable=True)
    public_key = Column(Text, nullable=True)
    key_fingerprint = Column(String(32), nullable=True)
    # idp_token mode: the ``aud`` the provider's tokens must carry.
    audience = Column(String(255), nullable=True)

    epoch = Column(Integer, nullable=False, default=1, server_default="1")
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    organization = relationship("Organization")


class ArtifactHostApplication(BaseSchema):
    """The artifact's side of the opt-in: this artifact may be framed by
    this host application."""
    __tablename__ = "artifact_host_applications"
    __table_args__ = (
        UniqueConstraint("artifact_id", "host_application_id", name="uq_artifact_host_applications_pair"),
    )

    artifact_id = Column(String(36), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True)
    host_application_id = Column(String(36), ForeignKey("host_applications.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)


class HostAssertionReplay(BaseSchema):
    """Accepted assertion ``jti`` values until they expire (one-time use)."""
    __tablename__ = "host_assertion_replays"
    __table_args__ = (
        UniqueConstraint("host_application_id", "jti", name="uq_host_assertion_replays_host_jti"),
    )

    host_application_id = Column(String(36), ForeignKey("host_applications.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    jti = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
