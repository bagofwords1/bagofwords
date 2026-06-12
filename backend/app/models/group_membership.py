from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class GroupMembership(BaseSchema):
    __tablename__ = 'group_memberships'
    __table_args__ = (
        UniqueConstraint('group_id', 'user_id', name='uq_group_membership'),
        UniqueConstraint('group_id', 'membership_id', name='uq_group_membership_pending'),
    )

    group_id = Column(String(36), ForeignKey('groups.id', ondelete='CASCADE'), nullable=False)
    # Either user_id (registered user) or membership_id (pending invite that
    # hasn't been claimed yet) is set. When the invited user registers, the
    # pending row is materialized into a user-keyed row (see
    # UserManager._attach_open_memberships).
    user_id = Column(String(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=True)
    membership_id = Column(String(36), ForeignKey('memberships.id', ondelete='CASCADE'), nullable=True)

    group = relationship("Group", back_populates="memberships")
    user = relationship("User")
    membership = relationship("Membership")
