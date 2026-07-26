from enum import Enum as PyEnum

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import BaseSchema


class ConnectionIndexingStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_INDEXING_STATUSES = {
    ConnectionIndexingStatus.COMPLETED.value,
    ConnectionIndexingStatus.FAILED.value,
    ConnectionIndexingStatus.CANCELLED.value,
}


class ConnectionIndexing(BaseSchema):
    """Tracks an in-flight or completed schema discovery run for a Connection.

    A Connection has many ConnectionIndexing rows, one per refresh attempt.
    Only one non-terminal (pending/running) row is allowed to exist at a time
    per connection *per scope* — the service layer enforces this.

    Two scopes share the table:

    - ``user_id IS NULL`` — the org-shared catalog run (service credentials).
    - ``user_id = <user>`` — a per-user catalog run. Sources whose catalog is
      owned per user (OneDrive, personal Drive) have nothing to index with the
      service account; their catalog is built from the signing-in user's own
      token. That build used to run inline in the OAuth callback, so the browser
      sat on the redirect for the length of a full drive walk. It now runs in
      the background against one of these rows, which is what the UI polls.
    """

    __tablename__ = "connection_indexings"

    connection_id = Column(
        String(36),
        ForeignKey("connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # NULL = the org-shared run. Set = a per-user catalog sync for this user.
    # Plain indexed column rather than a declared FK: the column is added by an
    # ALTER, and SQLite (the test/dev backend) cannot ALTER in a constraint, so a
    # declared FK here would describe a constraint the migrated database doesn't
    # have. Rows are read scoped by connection_id + user_id, never joined.
    user_id = Column(String(36), nullable=True, index=True)

    status = Column(
        String,
        nullable=False,
        default=ConnectionIndexingStatus.PENDING.value,
        index=True,
    )
    phase = Column(String(64), nullable=True)
    current_item = Column(String, nullable=True)
    progress_done = Column(Integer, nullable=False, default=0)
    progress_total = Column(Integer, nullable=False, default=0)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    error = Column(Text, nullable=True)
    stats_json = Column(JSON, nullable=True)
    events_json = Column(JSON, nullable=True)  # List[{ts, level, phase, message, done, total}]

    connection = relationship("Connection", back_populates="indexings")

    def mark_running(self) -> None:
        self.status = ConnectionIndexingStatus.RUNNING.value
        self.started_at = func.now()

    def mark_completed(self, stats: dict | None = None) -> None:
        self.status = ConnectionIndexingStatus.COMPLETED.value
        self.finished_at = func.now()
        if stats is not None:
            self.stats_json = stats

    def mark_failed(self, error: str) -> None:
        self.status = ConnectionIndexingStatus.FAILED.value
        self.finished_at = func.now()
        self.error = error

    def mark_cancelled(self) -> None:
        self.status = ConnectionIndexingStatus.CANCELLED.value
        self.finished_at = func.now()

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_INDEXING_STATUSES

    def __repr__(self) -> str:
        scope = f" user_id={self.user_id}" if self.user_id else ""
        return (
            f"<ConnectionIndexing id={self.id} connection_id={self.connection_id}"
            f"{scope} status={self.status} {self.progress_done}/{self.progress_total}>"
        )
