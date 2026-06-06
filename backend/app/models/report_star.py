from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ReportStar(BaseSchema):
    """
    Per-user star (favorite) for a report.

    Each row marks that a specific user has starred a report. Starring is
    personal: two users viewing the same shared report star it independently.
    Starred reports are surfaced at the top of the user's reports list.
    """
    __tablename__ = 'report_stars'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)

    report = relationship("Report", back_populates="stars", lazy="selectin")
    user = relationship("User", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('report_id', 'user_id', name='uq_report_star'),
    )
