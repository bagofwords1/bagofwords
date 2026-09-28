from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text

from app.models.base import BaseSchema


# ── Lifecycle ────────────────────────────────────────────────────────────────
# A check-in is the agent's own one-shot follow-up on a conversation. Every
# decision is kept as a row (including a planner "no") so the TraceModal can
# show the whole lifecycle, even the parts that left nothing visible to the user.
STATUS_NOT_PROPOSED = "not_proposed"  # planner ran and said no follow-up
STATUS_REJECTED = "rejected"          # planner proposed, a code limit denied it
STATUS_PLANNED = "planned"            # armed; a checkin:<id> date job exists
STATUS_CANCELLED = "cancelled"        # a guardrail stopped it before/at fire time
STATUS_SKIPPED = "skipped"            # the judge decided not to run it
STATUS_RUNNING = "running"            # the machine turn is in flight
STATUS_RAN_QUIET = "ran_quiet"        # ran, did not notify
STATUS_SENT = "sent"                  # ran and notified the user
STATUS_FAILED = "failed"              # the run raised

STATUSES = {
    STATUS_NOT_PROPOSED, STATUS_REJECTED, STATUS_PLANNED, STATUS_CANCELLED,
    STATUS_SKIPPED, STATUS_RUNNING, STATUS_RAN_QUIET, STATUS_SENT, STATUS_FAILED,
}
# Statuses that count as a delivered follow-up run toward the weekly/daily caps.
RUN_STATUSES = (STATUS_RAN_QUIET, STATUS_SENT)

# ── Machine-readable reasons (status_reason) ────────────────────────────────
REASON_DISABLED = "disabled"
REASON_PENDING_USER = "pending_exists_user"
REASON_PENDING_REPORT = "pending_exists_report"
REASON_WEEKLY_CAP = "weekly_cap"
REASON_ORG_DAILY_CAP = "org_daily_cap"
REASON_REPORT_DELETED = "report_deleted"
REASON_ACCESS_LOST = "access_lost"
REASON_JUDGE_SKIP = "judge_skip"
REASON_RUN_FAILED = "run_failed"
REASON_INVALID_JUDGE_OUTPUT = "invalid_judge_output"
REASON_OPTED_OUT = "opted_out"
REASON_FIRE_ERROR = "fire_error"          # an exception before/around the run (guardrails, judge)
REASON_STALE_RUNNING = "stale_running"    # left in 'running' (e.g. a restart mid-run); swept


class AgentCheckin(BaseSchema):
    """One planned (or declined) agent follow-up on a report conversation."""

    __tablename__ = "agent_checkins"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    # The human the follow-up is for (the one who drove the planning turn).
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    # The follow-up runs in this same report.
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=False, index=True)
    # The system completion of the turn that planned it (TraceModal anchor).
    source_completion_id = Column(String(36), ForeignKey("completions.id"), nullable=True, index=True)

    note = Column(Text, nullable=True)
    plan_reason = Column(Text, nullable=True)
    due_at = Column(DateTime, nullable=True)  # naive UTC, like every other timestamp
    job_id = Column(String(64), nullable=True)

    status = Column(String(24), nullable=False, index=True)
    status_reason = Column(String(64), nullable=True)

    judge_decision = Column(String(8), nullable=True)  # run | skip
    judge_reason = Column(Text, nullable=True)
    judge_focus = Column(Text, nullable=True)
    judged_at = Column(DateTime, nullable=True)

    run_completion_id = Column(String(36), ForeignKey("completions.id"), nullable=True)
    notified = Column(Boolean, nullable=False, default=False)
    notify_subject = Column(String(280), nullable=True)
    sent_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # Pending-per-user / pending-per-report / cap counts all filter on status.
        Index("ix_agent_checkins_org_status", "organization_id", "status"),
        Index("ix_agent_checkins_user_status", "user_id", "status"),
    )
