from sqlalchemy import Column, String, ForeignKey, Text, JSON, DateTime, Integer, UniqueConstraint, Index

from app.models.base import BaseSchema


class MemoryEntry(BaseSchema):
    """One fact about a user that the agent remembers across sessions.

    Scope is (organization, user). Entries are private to that user: there is
    no admin read path. Memory holds facts (their work, projects, deadlines,
    what they follow, their own shorthand), never rules: how to answer or
    compute lives in instructions.

    An edit updates the row in place (the handle stays the same). Forgetting
    blanks the content and keeps only id/status/timestamps.
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

    # One declarative fact about the user (≤280 chars). Blank once forgotten.
    text = Column(Text, nullable=False, default="")
    # 1–4 normalized slugs (topic tags or object tags like agent:<id>).
    tags = Column(JSON, nullable=True)
    # Other words the user uses for the same thing (their shorthand).
    aliases = Column(JSON, nullable=True)

    # Optional date (and end date) — a dated fact ends after its date.
    event_start = Column(DateTime, nullable=True)
    event_end = Column(DateTime, nullable=True)
    # Explicit expiry; when empty the computed default applies (memory_rules.effective_expiry).
    expires_at = Column(DateTime, nullable=True)

    # user | agent
    source = Column(String(12), nullable=False, default="agent")
    # {report_id, completion_id, quote}; quote is the user's own words (≤200).
    evidence = Column(JSON, nullable=True)

    seen_count = Column(Integer, nullable=False, default=1)
    last_seen_at = Column(DateTime, nullable=True)

    # active | forgotten
    status = Column(String(12), nullable=False, default="active", index=True)
