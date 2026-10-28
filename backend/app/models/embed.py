"""Embedded chat deployments ("embeds").

An embed is BOW chat delivered inside a customer's own application (an ERP,
ServiceNow, an internal portal) through a BOW-served script and iframe.

Trust model, in one place:

* The embed row is the ONLY authority for who may frame the chat
  (``allowed_origins``), who may sign employees in (``issuer`` + the RSA
  ``public_key`` whose private half only the customer backend holds) and
  which BOW user an asserted employee is
  (``subject_attribute``: the admin-set member attribute key whose value must
  equal the assertion's ``sub``). Nothing the browser or the host backend
  sends can widen any of that.
* Employees are EXISTING BOW users. There is no provisioning, no email
  matching and no role assignment from assertion claims (see
  services/embed_service.py).
* ``epoch`` is bumped on disable and on key rotation; every embed session
  token carries the epoch it was minted under, so a bump revokes them all at
  once (same idea as ``User.session_epoch``).
* ``runtime`` is the admin's execution configuration (agents, model, effort,
  public tool switches, follow-ups). It is copied onto the hidden per-user
  chat report (``Report.report_type='embed_chat'``, ``Report.embed_id``) on
  every turn, so a saved change applies to the next message without any
  host redeploy — and so every existing enforcement path that reads the
  report (roster, disabled tools, model) applies unchanged.
* ``branding`` is appearance only. Secrets never live in JSON columns.
"""
from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class Embed(BaseSchema):
    __tablename__ = "embeds"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    # The id the host page uses (``BOWEmbed.mount({ embedId })``) and the
    # assertion audience. Public, not a secret — the secret is what signs.
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")

    # Exact origins (scheme://host[:port]) allowed to frame the chat. Drives the
    # frame-ancestors policy of the chat document and the postMessage handshake.
    allowed_origins = Column(JSON, nullable=False, default=list)

    # Trusted host assertion contract (RS256, see services/embed_service.py).
    issuer = Column(String(255), nullable=False)
    # Member attribute key (Membership.custom_attributes) whose value is the
    # assertion ``sub`` — e.g. "servicenow_sys_id" or "employee_id".
    subject_attribute = Column(String(64), nullable=False)
    # PEM public key that verifies host assertions. BOW generates the keypair
    # and shows the private key exactly once (creation / rotation); it is never
    # stored, so a database leak yields nothing that can sign anyone in.
    public_key = Column(Text, nullable=False)
    # SHA-256 of the public key, first 32 hex chars: the ``kid`` the customer
    # sees and the way the settings page tells keys apart.
    key_fingerprint = Column(String(32), nullable=False)
    epoch = Column(Integer, nullable=False, default=1, server_default="1")

    # {data_source_ids, model_id, reasoning_effort, tools, follow_ups,
    #  suggest_instructions, allow_agent_selection}
    runtime = Column(JSON, nullable=False, default=dict)
    # {display_name, accent_color, theme, greeting, suggestions, logo_key, locale}
    branding = Column(JSON, nullable=False, default=dict)

    # Optimistic concurrency for the settings page: a PUT carries the revision
    # it edited and is rejected (409) when a newer one has been saved.
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    organization = relationship("Organization")


class EmbedAssertionReplay(BaseSchema):
    """One row per accepted assertion ``jti`` until it expires: the bootstrap
    exchange is one-time, so a captured assertion cannot be replayed."""
    __tablename__ = "embed_assertion_replays"
    __table_args__ = (
        UniqueConstraint("embed_id", "jti", name="uq_embed_assertion_replays_embed_jti"),
    )

    embed_id = Column(String(36), ForeignKey("embeds.id", ondelete="CASCADE"), nullable=False, index=True)
    jti = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
