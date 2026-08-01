from sqlalchemy import Column, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ReportView(BaseSchema):
    """
    Per-user "last viewed" watermark for a report.

    One row per (report, user), bumped every time that user opens the report.
    A report is *unread* for a user when its ``last_activity_at`` is newer
    than their watermark (or no row exists — never visited). Viewing is
    personal, like ``ReportStar``: two users tracking the same shared report
    read it independently.
    """
    __tablename__ = 'report_views'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    last_viewed_at = Column(DateTime, nullable=False)

    report = relationship("Report", foreign_keys=[report_id], lazy="noload")
    user = relationship("User", foreign_keys=[user_id], lazy="noload")

    __table_args__ = (
        UniqueConstraint('report_id', 'user_id', name='uq_report_view'),
    )
