from sqlalchemy import Column, String, ForeignKey, Text, JSON, DateTime, Integer, UniqueConstraint, Index

from app.models.base import BaseSchema


class MemoryEntry(BaseSchema):
    """One durable, personal fact the agent remembers about a user.

    Scope is (organization, user) — the same scope as the legacy
    ``Membership.memory`` document this replaces. Entries are private to that
    user: there is no admin read path. Memory is personal context (style,
    role, schedule, the user's own shorthand), never business logic —
    definitions and rules live in instructions.

    Versioning: an update inserts a new row (new handle) and marks the old one
    ``superseded`` with ``superseded_by_id`` pointing at the replacement.
    Forgetting blanks the content and keeps only id/status/timestamps.
    """
    __tablename__ = 'memory_entries'
    __table_args__ = (
        UniqueConstraint('organization_id', 'user_id', 'seq', name='uq_memory_entries_org_user_seq'),
        Index('ix_memory_entries_org_user_status', 'organization_id', 'user_id', 'status'),
    )

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)

    # Stable short id shown to the agent ("m7"); ``seq`` is its numeric part and
    # carries the per-(org, user) uniqueness so concurrent writers can't collide.
    seq = Column(Integer, nullable=False)
    handle = Column(String(12), nullable=False)

    # style | role | vocabulary | events | focus | preferences
    section = Column(String(16), nullable=False)
    # One declarative, personal fact (≤280 chars). Blank once forgotten.
    text = Column(Text, nullable=False, default="")
    # 1–4 normalized slugs (topic tags or object tags like agent:<id>).
    tags = Column(JSON, nullable=True)
    # Other words the user uses for the same thing (vocabulary / focus).
    aliases = Column(JSON, nullable=True)

    event_start = Column(DateTime, nullable=True)
    event_end = Column(DateTime, nullable=True)
    # Explicit expiry; when empty the computed defaults apply (see memory_rules).
    expires_at = Column(DateTime, nullable=True)

    # user | agent | migration
    source = Column(String(12), nullable=False, default="agent")
    # {report_id, completion_id, quote}; quote is the user's own words (≤200).
    evidence = Column(JSON, nullable=True)

    seen_count = Column(Integer, nullable=False, default=1)
    last_seen_at = Column(DateTime, nullable=True)

    # active | superseded | forgotten
    status = Column(String(12), nullable=False, default="active", index=True)
    superseded_by_id = Column(String(36), nullable=True)
