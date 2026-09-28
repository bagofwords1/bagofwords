from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Float

from app.models.base import BaseSchema

KIND_AGENT = "agent"
KIND_USER = "user"
KINDS = {KIND_AGENT, KIND_USER}

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUSES = {STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_SKIPPED, STATUS_CANCELLED, STATUS_FAILED}

REASON_NOTHING_NEW = "nothing_new"
REASON_DISABLED = "disabled"
REASON_BUDGET = "budget"
REASON_STALE = "stale"
REASON_INVALID_OUTPUT = "invalid_output"
REASON_NO_MODEL = "no_model"


class DreamRun(BaseSchema):
    """One nightly reflection: an agent dream (instructions for one agent) or a
    user dream (memory, open threads, planned check-ins and habit offers for
    one user). The audit record of what a dream read, decided and changed.

    Privacy: for ``kind='user'`` the text inside ``tool_calls``/``outputs`` is
    readable only by that user; everyone else gets counts (see
    ``DreamRunService.public_view``).
    """

    __tablename__ = "dream_runs"
    __table_args__ = (
        Index("ix_dream_runs_org_date", "organization_id", "local_date"),
        Index("ix_dream_runs_user_kind", "user_id", "kind"),
        Index("ix_dream_runs_ds_kind", "data_source_id", "kind"),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    kind = Column(String(8), nullable=False)
    # Exactly one of these is set, depending on ``kind``.
    data_source_id = Column(String(36), ForeignKey("data_sources.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    # The org-local night this run belongs to (YYYY-MM-DD).
    local_date = Column(String(10), nullable=False)

    status = Column(String(12), nullable=False, default=STATUS_QUEUED, index=True)
    status_reason = Column(String(64), nullable=True)

    # Counts only (e.g. {"drafts": 14, "feedback": 5, "sessions": 3}).
    inputs_summary = Column(JSON, nullable=True)
    # What the model proposed and what code applied / refused.
    tool_calls = Column(JSON, nullable=True)
    outputs = Column(JSON, nullable=True)

    tokens = Column(Integer, nullable=True)
    cost_usd = Column(Float, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
