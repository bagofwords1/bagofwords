from sqlalchemy import Column, ForeignKey, Index, String

from app.models.base import BaseSchema

THREAD_OPEN = "open"
THREAD_RESOLVED = "resolved"
THREAD_DISMISSED = "dismissed"


class UserOpenThread(BaseSchema):
    """Something a user left unfinished in a report, noted by their nightly
    reflection and shown in the session-start briefing. Private to the user."""

    __tablename__ = "user_open_threads"
    __table_args__ = (
        Index("ix_user_open_threads_org_user_status", "organization_id", "user_id", "status"),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=False, index=True)
    text = Column(String(200), nullable=False)
    unblocked_by = Column(String(200), nullable=True)
    dream_run_id = Column(String(36), ForeignKey("dream_runs.id"), nullable=True)
    status = Column(String(12), nullable=False, default=THREAD_OPEN)
