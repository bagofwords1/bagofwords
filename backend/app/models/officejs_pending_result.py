from sqlalchemy import Column, DateTime, ForeignKey, String

from app.ee.encryption import EncryptedJSON
from app.models.base import BaseSchema


class OfficeJsPendingResult(BaseSchema):
    """A pending Office.js execution dispatched to the Excel taskpane.

    The row — not an in-process dict — is the source of truth, because the run
    that dispatches the code and the HTTP POST that returns its result land on
    *different uvicorn workers* (``start.sh`` passes ``--workers``): a worker
    that never registered the pending call cannot see it, so an in-memory-only
    registry made the taskpane's result POST 404 and the tool hang until its
    timeout. The waiting tool polls this row, so any worker can resolve it.

    The in-process registry (``app.ai.tools.officejs_registry``) is kept purely
    as a same-worker fast path so a local result wakes the tool instantly
    instead of on the next poll.

    ``status`` transitions pending → resolved, once. ``user_id`` /
    ``completion_id`` bind the pending call to the run that dispatched it, so
    only the initiating user may resolve it and only via the completion the
    action was issued for.
    """

    __tablename__ = "officejs_pending_results"

    STATUS_PENDING = "pending"
    STATUS_RESOLVED = "resolved"

    tool_call_id = Column(String(64), nullable=False, unique=True, index=True)
    status = Column(String, nullable=False, default=STATUS_PENDING, index=True)

    completion_id = Column(
        String(36), ForeignKey("completions.id"), nullable=True, index=True
    )
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)

    # The officeJsResult payload posted back by the taskpane (may contain
    # spreadsheet data, hence encrypted like tool_executions.result_json).
    result = Column(EncryptedJSON(none_as_null=True), nullable=True)

    resolved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    @property
    def is_pending(self) -> bool:
        return self.status == self.STATUS_PENDING
