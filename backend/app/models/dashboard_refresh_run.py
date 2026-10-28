from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, PrimaryKeyConstraint, String, Text

from app.models.base import BaseSchema

# What started a refresh.
TRIGGER_SCHEDULE = "schedule"  # the dashboard's schedule
TRIGGER_VIEW = "view"          # someone opened it (refresh when opened)
TRIGGER_MANUAL = "manual"      # a person's Refresh

# How it went. `running` until it ends; a row still running long after it
# started reads as interrupted (dashboard_refresh_history).
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"     # a draft's run: some queries refreshed, some failed
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"     # see skip_reason


class DashboardRefreshRun(BaseSchema):
    """One refresh of a dashboard (migration refhist01, decided 2026-10-25).

    Both engines write it: the published release's (dashboard_refresh,
    `release_id` set) and the draft's (the conversation's rerun, `report_id`
    set). A scheduled run that could not run is a row too (skipped, with
    why); a view that did not refresh is not. Rows go after 30 days.

    `release_id` and `report_id` are soft references: a release is deleted
    after a newer one goes live, and the row outlives it.
    """
    __tablename__ = "dashboard_refresh_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_dashboard_refresh_runs"),
        Index("ix_dashboard_refresh_runs_artifact_started", "artifact_id", "started_at"),
    )

    artifact_id = Column(String(36), ForeignKey("artifacts.id", name="fk_dashboard_refresh_runs_artifact_id",
                                                ondelete="CASCADE"), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id",
                                                    name="fk_dashboard_refresh_runs_organization_id"),
                             nullable=False)
    release_id = Column(String(36), nullable=True)
    report_id = Column(String(36), nullable=True)
    trigger = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default=STATUS_RUNNING, server_default=STATUS_RUNNING)
    # Why a skipped run did not (or did not count): no_runner, publishing,
    # replaced, stopped, nothing_to_run.
    skip_reason = Column(String(40), nullable=True)
    steps_total = Column(Integer, nullable=False, default=0, server_default="0")
    steps_succeeded = Column(Integer, nullable=False, default=0, server_default="0")
    steps_failed = Column(Integer, nullable=False, default=0, server_default="0")
    # The first failure: the query or connection it was in (when the run
    # knows it) and the reason, as the run met it.
    error_source = Column(String(255), nullable=True)
    error = Column(Text, nullable=True)
    # Whose credentials it ran with, and who pressed Refresh (manual only).
    run_as_user_id = Column(String(36), ForeignKey("users.id", name="fk_dashboard_refresh_runs_run_as_user_id",
                                                   ondelete="SET NULL"), nullable=True)
    triggered_by_user_id = Column(String(36), ForeignKey("users.id",
                                                         name="fk_dashboard_refresh_runs_triggered_by_user_id",
                                                         ondelete="SET NULL"), nullable=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
