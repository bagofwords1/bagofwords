from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)

from app.models.base import BaseSchema

# What started a run.
TRIGGER_CREATE = "create"          # the table was just created
TRIGGER_SCHEDULE = "schedule"      # the table's refresh schedule
TRIGGER_MANUAL = "manual"          # a person's Refresh
TRIGGER_DEFINITION = "definition"  # the query or its refresh settings changed
TRIGGER_RESUME = "resume"          # continuing a first load that paused

# What kind of work it was.
MODE_FULL = "full"                 # the whole table, rebuilt
MODE_INCREMENTAL = "incremental"   # only the rows since the checkpoint

# How it went. `running` until it ends; a row still running long after it
# started reads as interrupted (custom_table_runs).
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"         # a first load paused part-way; it resumes
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


class CustomTableRun(BaseSchema):
    """One refresh of a custom table (migration cqruns01).

    The table row only remembers how its last refresh went; this is the log:
    what started each run, the range of data it covered, what it read from
    the source and what that did to the table.

    `connection_table_id` is a soft reference on purpose: a custom table is
    soft-deleted, and its history is purged by age rather than with it.
    `inputs` names what the run read from — today the connection — so a run
    that reads other custom tables can record them the same way.
    """
    __tablename__ = "custom_table_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_custom_table_runs"),
        Index("ix_custom_table_runs_table_started", "connection_table_id", "started_at"),
    )

    connection_table_id = Column(String(36), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id",
                                                    name="fk_custom_table_runs_organization_id"),
                             nullable=False)
    trigger = Column(String(20), nullable=False)
    mode = Column(String(20), nullable=False, default=MODE_FULL, server_default=MODE_FULL)
    status = Column(String(20), nullable=False, default=STATUS_RUNNING, server_default=STATUS_RUNNING)
    triggered_by_user_id = Column(String(36), ForeignKey("users.id",
                                                         name="fk_custom_table_runs_triggered_by_user_id",
                                                         ondelete="SET NULL"), nullable=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    elapsed_ms = Column(Integer, nullable=True)

    # The cursor range the run covered: where it read from, and the
    # checkpoint it left behind. Typed values (incremental.encode).
    window_from = Column(JSON, nullable=True)
    window_to = Column(JSON, nullable=True)

    rows_read = Column(BigInteger, nullable=True)
    rows_inserted = Column(BigInteger, nullable=True)
    rows_updated = Column(BigInteger, nullable=True)
    rows_deleted = Column(BigInteger, nullable=True)
    row_count = Column(BigInteger, nullable=True)       # the table after the run
    artifact_bytes = Column(BigInteger, nullable=True)

    # The query as it was sent, with the window filled in.
    rendered_query = Column(Text, nullable=True)
    schema_fingerprint = Column(String(32), nullable=True)
    inputs = Column(JSON, nullable=True)
    # A first load done in windows: [{lo, hi, rows, status}].
    chunks = Column(JSON, nullable=True)
    # A short structured log: [{at, level, message}].
    events = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
