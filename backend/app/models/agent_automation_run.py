from sqlalchemy import Column, String, Integer, JSON, ForeignKey, DateTime
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


# Why an automation loop ran for an agent.
TRIGGER_MANUAL = "manual"               # a human kicked it from the UI
TRIGGER_TABLE_CHANGE = "table_change"   # a table was activated / columns changed
TRIGGER_INSTRUCTION_CHANGE = "instruction_change"  # the agent's own build changed
TRIGGER_GLOBAL_CHANGE = "global_change"  # a global instruction build was promoted
TRIGGERS = {
    TRIGGER_MANUAL,
    TRIGGER_TABLE_CHANGE,
    TRIGGER_INSTRUCTION_CHANGE,
    TRIGGER_GLOBAL_CHANGE,
}

# Terminal / transient states of one loop.
STATUS_RUNNING = "running"
STATUS_PASSED = "passed"            # evals green (possibly after training); done
STATUS_PASSED_PENDING = "passed_pending"  # green, but candidate build awaits human approve
STATUS_GAVE_UP = "gave_up"         # couldn't fix within max_iterations; outcome applied
STATUS_NO_EVALS = "no_evals"       # nothing to measure against; no-op
STATUS_SKIPPED = "skipped"         # trigger autonomy was off
STATUS_ERROR = "error"             # the loop itself blew up
STATUSES = {
    STATUS_RUNNING,
    STATUS_PASSED,
    STATUS_PASSED_PENDING,
    STATUS_GAVE_UP,
    STATUS_NO_EVALS,
    STATUS_SKIPPED,
    STATUS_ERROR,
}


class AgentAutomationRun(BaseSchema):
    """Audit record for one execution of the agent-reliability loop.

    One row per trigger firing. Links to the instruction build it produced and
    the eval test-runs it spawned so the UI can show "3 columns changed -> ran
    evals -> 2 failed -> trained -> promoted build #14" with full drill-down.
    """

    __tablename__ = "agent_automation_runs"

    organization_id = Column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    data_source_id = Column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    trigger = Column(String, nullable=False)
    status = Column(String, nullable=False, default=STATUS_RUNNING, index=True)

    # How many train -> re-eval iterations were performed.
    iterations = Column(Integer, nullable=False, default=0)

    # The candidate instruction build this loop created/used, if any.
    build_id = Column(
        String(36), ForeignKey("instruction_builds.id"), nullable=True, index=True
    )

    # Ordered list of TestRun ids spawned by this loop (baseline first).
    test_run_ids_json = Column(JSON, nullable=True, default=list)

    # Free-form structured trace: per-iteration pass/fail counts, the action
    # taken on give-up (training / development), human-readable reason, etc.
    detail_json = Column(JSON, nullable=True, default=dict)

    # Who/what initiated it (a user id for manual runs; None for system triggers).
    requested_by_user_id = Column(
        String(36), ForeignKey("users.id"), nullable=True, index=True
    )

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    data_source = relationship("DataSource", lazy="noload")
