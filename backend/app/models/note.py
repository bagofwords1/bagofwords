from sqlalchemy import Column, String, ForeignKey, Text
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class Note(BaseSchema):
    """A per-report working note — the agent's scratchpad memory.

    Notes are freeform markdown the agent writes and reads while answering a
    single report: a plan (``- [ ]`` task lines), findings, ruled-out
    hypotheses, definitions still being pinned down. They are injected back into
    the planner (and the knowledge harness) on every iteration and shown in the
    report UI. Unlike instructions they are NOT reviewed shared knowledge, and
    unlike doc artifacts they are not versioned — ``edit_note`` updates in place.

    Scope is the report; ``agent_execution_id`` records which run wrote the note
    (provenance / optional filtering), but the note lives for the whole report.
    """
    __tablename__ = 'notes'

    # The report this note belongs to (the note's scope)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    report = relationship("Report", lazy="selectin")

    # Which agent run created the note (provenance; nullable for user-created)
    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=True, index=True)

    # Author (the agent's acting user, or a human for user-created notes)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)

    # Organization for multi-tenancy
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)

    # Optional short title
    title = Column(String(255), nullable=True)

    # Freeform markdown body (may contain `- [ ]` task lists)
    content = Column(Text, nullable=False, default="")

    # Provenance: 'agent' (written by the AI) or 'user'
    source = Column(String(20), nullable=False, default='agent')
