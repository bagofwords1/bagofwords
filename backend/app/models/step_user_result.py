from sqlalchemy import JSON, Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class StepUserResult(BaseSchema):
    """Per-viewer snapshot of a step's execution result.

    `Step.data` is the single shared snapshot every viewer of a published
    artifact sees, materialized under whichever identity produced it. When a
    viewer of a shared artifact re-runs the dashboard's queries (either with
    their own data-source credentials or on behalf of the report creator),
    the result lands here — keyed by (step_id, user_id) — so one viewer's
    run never changes what the owner or other viewers see.

    Rows are a cache of derived data: they are hard-deleted whenever the
    underlying step's shared snapshot is rewritten (owner rerun, scheduled
    refresh, new step version) rather than soft-deleted.
    """
    __tablename__ = 'step_user_results'
    __table_args__ = (
        UniqueConstraint('step_id', 'user_id', name='uq_step_user_results_step_user'),
    )

    step_id = Column(String(36), ForeignKey('steps.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    # Denormalized for cheap per-report invalidation/cleanup
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)

    # 'success' | 'error'
    status = Column(String(20), nullable=False, default='success')
    status_reason = Column(Text, nullable=True)

    # Same shape as Step.data ({"rows": [...], "columns": [...]})
    data = Column(JSON, nullable=True, default=dict)

    # Whose credentials executed the query: 'viewer' | 'creator'
    executed_as = Column(String(20), nullable=False, default='viewer')

    last_run_at = Column(DateTime, nullable=True)

    step = relationship("Step", lazy="selectin")
    user = relationship("User", lazy="selectin")
