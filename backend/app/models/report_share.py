from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ReportShare(BaseSchema):
    """
    Sharing grants for reports.

    Each row grants a principal — a specific user OR a whole group — access to
    either the artifact (dashboard) or the conversation of a report, depending
    on share_type. Exactly one of user_id / group_id is set per row.

    share_type: 'artifact' | 'conversation'
    """
    __tablename__ = 'report_shares'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True, index=True)
    group_id = Column(String(36), ForeignKey('groups.id'), nullable=True, index=True)
    share_type = Column(String(20), nullable=False, index=True)  # 'artifact' or 'conversation'

    report = relationship("Report", back_populates="shares", lazy="selectin")
    user = relationship("User", lazy="selectin")
    group = relationship("Group", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('report_id', 'user_id', 'share_type', name='uq_report_share'),
        UniqueConstraint('report_id', 'group_id', 'share_type', name='uq_report_share_group'),
    )
