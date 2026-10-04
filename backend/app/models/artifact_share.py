from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ArtifactShare(BaseSchema):
    """
    Sharing grants for one artifact (dashboard / deck / doc).

    Each row grants a principal — a specific user OR a whole group — access to
    a single artifact while its visibility is 'shared'. Exactly one of
    user_id / group_id is set per row. report_id is denormalized from the
    artifact so report-level questions ("is anything here shared with me")
    stay one indexed lookup.

    Conversation sharing is separate and stays on report_shares.
    """
    __tablename__ = 'artifact_shares'

    artifact_id = Column(String(36), ForeignKey('artifacts.id'), nullable=False, index=True)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True, index=True)
    group_id = Column(String(36), ForeignKey('groups.id'), nullable=True, index=True)

    user = relationship("User", lazy="selectin")
    group = relationship("Group", lazy="selectin")

    __table_args__ = (
        UniqueConstraint('artifact_id', 'user_id', name='uq_artifact_share_user'),
        UniqueConstraint('artifact_id', 'group_id', name='uq_artifact_share_group'),
    )
