from sqlalchemy import Column, String, Boolean, Text, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class Role(BaseSchema):
    __tablename__ = 'roles'
    __table_args__ = (
        UniqueConstraint('organization_id', 'name', name='uq_roles_org_name'),
    )

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)  # NULL = system built-in
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    permissions = Column(JSON, nullable=False, default=list)  # ["view_reports", ...]
    is_system = Column(Boolean, nullable=False, default=False)
    # When set, this is an organization's editable OVERRIDE of a built-in system
    # role (currently only "member"): an org-scoped, is_system=False copy whose
    # permissions an admin may customize. Holds the base system-role name it
    # overrides ("member"). NULL for the global system rows and for ordinary
    # custom roles. Used by resolve_assignable_system_role so that member
    # assignments in this org target the override instead of the global default.
    overrides_system_role = Column(String(50), nullable=True)

    organization = relationship("Organization", back_populates="roles")
    role_assignments = relationship("RoleAssignment", back_populates="role", cascade="all, delete-orphan")
