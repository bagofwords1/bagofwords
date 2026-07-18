from sqlalchemy import Column, ForeignKey, Table, String, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
import uuid

class Membership(BaseSchema):
    __tablename__ = 'memberships'

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), primary_key=True)
    email = Column(String, nullable=True)
    invite_token = Column(String(36), nullable=True, unique=True, default=lambda: str(uuid.uuid4()))
    # When the invite link stops being accepted (pending invites only). NULL =
    # no expiry enforced (legacy rows / non-invite memberships).
    invite_expires_at = Column(DateTime, nullable=True)
    note = Column(String, nullable=True)
    # Per-user, per-org agent memory. Small, curated, agent-written durable
    # facts about this user (preferences, writing style, analyses they liked).
    # Full-document rewrite via the update_user_memory tool; the user can
    # view/edit it in their profile. Capped at MEMBERSHIP_MEMORY_MAX_LENGTH
    # chars so it can be always-injected without bloating context. Distinct
    # from ``note`` (user-authored profile) and from Notes (per-report
    # scratchpad).
    memory = Column(String, nullable=True)
    # Per-user default LLM model for this org. Soft reference (no FK): a stale
    # value falls back to the org default at resolve time.
    default_llm_model_id = Column(String(36), nullable=True)

    user = relationship("User", back_populates="memberships")
    organization = relationship("Organization", back_populates="memberships")

    role = Column(String, nullable=False, default='member')