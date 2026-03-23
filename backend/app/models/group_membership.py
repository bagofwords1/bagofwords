from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class GroupMembership(BaseSchema):
    __tablename__ = 'group_memberships'
    __table_args__ = (
        UniqueConstraint('group_id', 'user_id', name='uq_group_membership'),
    )

    group_id = Column(String(36), ForeignKey('groups.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    group = relationship("Group", back_populates="memberships")
    user = relationship("User")
