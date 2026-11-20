from sqlalchemy import Column, String, JSON, ForeignKey, DateTime, Index
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


# ── Sources (where the notification was produced) ───────────────────────────────
# A notification is the *per-user delivery* of something that happened. Unlike a
# ReviewItem (org/agent-scoped, shared team state), each row here belongs to one
# recipient and carries that recipient's own read/dismiss state.
SOURCE_REVIEW = "review"            # fanned out from a ReviewItem (low_confidence, schema_changed, …)
SOURCE_SHARE = "share"             # someone shared a dashboard/conversation with this user
SOURCE_REPORT_TOOL = "report_tool"  # emitted by a tool from inside a report run
SOURCE_SCHEDULE = "schedule"        # a scheduled report/prompt produced results
SOURCE_CHECKIN = "checkin"          # the agent's own follow-up (agent check-ins)
SOURCE_CONNECTION = "connection"    # a shared connection's health changed (down / restored)
SOURCES = {SOURCE_REVIEW, SOURCE_SHARE, SOURCE_REPORT_TOOL, SOURCE_SCHEDULE,
           SOURCE_CHECKIN, SOURCE_CONNECTION}

# ── Severity (drives sort + accent; mirrors review_item) ────────────────────────
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITIES = {SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR}
SEVERITY_RANK = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}

# ── Resolution (the thing the notification is about was closed) ────────────────
# Set on every recipient's row when the source ReviewItem closes, so the inbox
# shows it as handled instead of an open alert. Distinct from the recipient's
# own dismiss (``dismissed_at``), which only hides it for that one user.
RESOLUTION_RESOLVED = "resolved"    # the underlying issue was fixed
RESOLUTION_DISMISSED = "dismissed"  # someone closed the item in the Review feed
RESOLUTIONS = {RESOLUTION_RESOLVED, RESOLUTION_DISMISSED}

# How the item was handled — the reason shown next to "Handled" in the inbox and
# the Review feed. Every closure has a person behind it, directly (marked) or
# through something they did (accepted a suggestion, ran a training that passed).
HANDLED_MARKED = "marked"                        # someone chose "Mark as handled"
HANDLED_SUGGESTION_REVIEWED = "suggestion_reviewed"  # the pending change was accepted/resolved
HANDLED_SUGGESTION_REJECTED = "suggestion_rejected"  # the pending change was rejected
HANDLED_SUGGESTION_VOIDED = "suggestion_voided"      # the instruction changed under it
HANDLED_TRAINING_PASSED = "training_passed"      # a training run started from Review passed
HANDLED_EVAL_PASSED = "eval_passed"              # an eval run started from Review passed
HANDLED_CONDITION_CLEARED = "condition_cleared"  # a producer re-verified the condition is gone

# Handled notifications stay visible (greyed) for this long, then drop out of the
# inbox list on their own. The rows are kept: dedup reads them.
RESOLVED_VISIBLE_DAYS = 7


class Notification(BaseSchema):
    """One notification delivered to one user — the per-recipient inbox row.

    This is the delivery layer the review feed and the share/notify paths were
    missing: ``ReviewItem`` is org/agent-scoped with *shared* state, and the
    share path only sent email. A ``Notification`` belongs to a single
    ``user_id`` and owns that user's ``read_at`` / ``dismissed_at`` so each
    recipient triages independently.

    Provenance lives in (``source``, ``source_id``) — a soft reference back to
    the originating ``ReviewItem`` / ``ReportShare`` / report run (no hard FK so
    transient producers are fine). ``group_key`` dedups within a single user's
    inbox: at most one ACTIVE (un-dismissed) row per (user, source, group_key).
    """

    __tablename__ = "notifications"

    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    # The recipient. Every notification has exactly one.
    user_id = Column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Who/what caused it (the sharer, or null for system/agent producers).
    actor_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    source = Column(String, nullable=False, index=True)   # see SOURCE_* above
    type = Column(String, nullable=False)                 # finer-grained, e.g. review type or "share_conversation"
    severity = Column(String, nullable=False, default=SEVERITY_INFO)

    title = Column(String, nullable=False)
    body = Column(String, nullable=True)                  # human-readable detail
    # Frontend deep-link target — where the recipient acts on it, e.g.
    # "/reports/<id>", "/agents/<id>/tables", "/agents/<id>/review/<item>".
    link = Column(String, nullable=True)

    # Polymorphic pointer to the thing this is about:
    #   {"kind": "review_item", "review_item_id": "...", "data_source_id": "..."}
    #   {"kind": "report", "report_id": "...", "share_type": "conversation"}
    subject_json = Column(JSON, nullable=True, default=dict)

    # Provenance — soft reference back to the producer row (ReviewItem.id,
    # ReportShare.id, report run id, …). Used for backfill, dedup and to keep a
    # fanned-out notification in sync if the source resolves/escalates.
    source_id = Column(String(36), nullable=True, index=True)

    # Per-user dedup/collapse: one ACTIVE row per (user, source, group_key).
    group_key = Column(String, nullable=True, index=True)

    # Per-user triage state (this is the whole point of the table).
    read_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)

    # Shared state mirrored from the source (see RESOLUTION_*): when and how the
    # underlying item closed. Null while it is still open.
    resolved_at = Column(DateTime, nullable=True)
    resolution = Column(String(16), nullable=True)

    user = relationship("User", foreign_keys=[user_id], lazy="noload")
    actor = relationship("User", foreign_keys=[actor_user_id], lazy="noload")

    __table_args__ = (
        # Inbox listing: a user's notifications newest-first.
        Index("ix_notifications_user_created", "user_id", "created_at"),
        # Unread badge / count.
        Index("ix_notifications_user_read", "user_id", "read_at"),
        # Per-user dedup lookups.
        Index("ix_notifications_dedup", "user_id", "source", "group_key"),
    )
