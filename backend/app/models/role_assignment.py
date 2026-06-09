from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class RoleAssignment(BaseSchema):
    __tablename__ = 'role_assignments'
    __table_args__ = (
        UniqueConstraint('organization_id', 'role_id', 'principal_type', 'principal_id',
                         name='uq_role_assignment'),
    )

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    role_id = Column(String(36), ForeignKey('roles.id'), nullable=False)
    # "user" | "group" | "membership". A "membership" principal points at a
    # pending (unregistered) invite; it is rewritten to a "user" principal
    # when the invitee registers (see UserManager._attach_open_memberships).
    principal_type = Column(String, nullable=False)
    principal_id = Column(String(36), nullable=False)

    role = relationship("Role", back_populates="role_assignments")
