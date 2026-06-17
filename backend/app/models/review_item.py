from sqlalchemy import Column, String, Integer, JSON, ForeignKey, DateTime, Index
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


# ── Event types ───────────────────────────────────────────────────────────────
# A review item is something about an agent that an admin may want to act on.
# New types are added by writing a producer + (optionally) a default policy rule;
# the feed/actions are generic.
TYPE_INSTRUCTION_SUGGESTION = "instruction_suggestion"  # AI/non-admin proposed an instruction change
TYPE_SCHEMA_CHANGED = "schema_changed"                  # a connection's schema changed
TYPE_SLOW_QUERY = "slow_query"                          # queries running over the latency budget
TYPE_LOW_CONFIDENCE = "low_confidence"                  # agent answers scored low (<3/5)
TYPE_QUERY_ERROR = "query_error"                        # data query tool errored
EVENT_TYPES = {
    TYPE_INSTRUCTION_SUGGESTION,
    TYPE_SCHEMA_CHANGED,
    TYPE_SLOW_QUERY,
    TYPE_LOW_CONFIDENCE,
    TYPE_QUERY_ERROR,
}

# ── Severity (drives sort + accent) ─────────────────────────────────────────────
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITIES = {SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR}
SEVERITY_RANK = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}

# ── Shared lifecycle (team queue: anyone with access transitions it) ────────────
STATUS_OPEN = "open"
STATUS_IN_PROGRESS = "in_progress"   # a workflow action is running for it
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"
STATUS_SNOOZED = "snoozed"
STATUSES = {STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_DISMISSED, STATUS_SNOOZED}
# Statuses that still occupy the queue (and the group_key slot).
ACTIVE_STATUSES = {STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_SNOOZED}

# How it was handled.
DISPOSITION_NOTIFY = "notify"   # surfaced to a human (default)
DISPOSITION_AUTO = "auto"       # the policy ran action(s) automatically


class ReviewItem(BaseSchema):
    """One actionable item in the admin Review feed.

    Topic = an agent (``data_source_id``) or global (null). Recipients are not
    stored — visibility is derived from manage-permission on the agent (or full
    admin). State is shared: anyone with access reads/dismisses/resolves it.

    Dedup: at most one ACTIVE item per (org, agent, type, group_key); repeat
    occurrences bump ``group_count`` instead of inserting new rows.
    """

    __tablename__ = "review_items"

    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    # Null = global (applies to all agents). Otherwise scoped to one agent.
    data_source_id = Column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )

    type = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=False, default=SEVERITY_INFO)
    status = Column(String, nullable=False, default=STATUS_OPEN, index=True)
    disposition = Column(String, nullable=False, default=DISPOSITION_NOTIFY)

    title = Column(String, nullable=False)
    why = Column(String, nullable=True)            # the human-readable reason

    # Polymorphic pointer to the thing this is about (instruction+build,
    # completion, query_run, etc.): {"kind": "...", "instruction_id": "...", ...}
    subject_json = Column(JSON, nullable=True, default=dict)

    # Dedup/aggregation: items sharing a group_key collapse into one row whose
    # group_count ticks up on each occurrence.
    group_key = Column(String, nullable=True, index=True)
    group_count = Column(Integer, nullable=False, default=1)

    # Outcome of a resolution: {"action": "run_training", "ref": "...", "by": "...", "at": "..."}
    resolution_json = Column(JSON, nullable=True)
    # Async workflows fired from this item: [{"kind": "training_session", "id": "...", "status": "..."}]
    spawned_json = Column(JSON, nullable=True, default=list)

    # Provenance / links (soft references — no hard FK so transient ids are OK).
    source_run_id = Column(String(36), nullable=True)   # agent_automation_runs.id
    build_id = Column(String(36), nullable=True)
    caused_by_id = Column(String(36), nullable=True)    # review_items.id that spawned this

    # Shared triage audit.
    read_at = Column(DateTime, nullable=True)
    read_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    resolved_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    dismissed_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    snoozed_until = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)

    last_seen_at = Column(DateTime, nullable=True)       # most recent occurrence

    data_source = relationship("DataSource", lazy="noload")

    __table_args__ = (
        Index("ix_review_items_org_status_type", "organization_id", "status", "type"),
        Index("ix_review_items_dedup", "organization_id", "data_source_id", "type", "group_key"),
    )
