from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ReportShare(BaseSchema):
    """
    Per-user sharing grants for reports.

    Each row grants a specific user access to either the artifact (dashboard)
    or the conversation of a report, depending on share_type.

    share_type: 'artifact' | 'conversation'
    """
    __tablename__ = 'report_shares'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False, index=True)
    share_type = Column(String(20), nullable=False, index=True)  # 'artifact' or 'conversation'

    report = relationship("Report", back_populates="shares", lazy="selectin")
    user = relationship("User", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('report_id', 'user_id', 'share_type', name='uq_report_share'),
    )
