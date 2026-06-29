from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ServiceAccount(BaseSchema):
    """A non-human, org-managed API principal.

    A service account is a first-class identity backed by a hidden ``users``
    row (``user_id``). The backing user is created with ``is_active=False`` and
    ``is_service_account=True`` so it can never log in interactively, but its
    API keys (rows in ``api_keys`` pointing at ``user_id``) keep working. RBAC
    permissions are granted to the backing user via ``RoleAssignment`` with
    ``principal_type='user'`` — no ``Membership`` row, so it consumes no seat
    and never leaks into member lists.

    Org binding lives here (``organization_id``), which is what the permission
    decorators consult instead of a ``Membership`` for service-account
    principals.
    """

    __tablename__ = "service_accounts"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    # The hidden backing users row. Unique: one service account per user row.
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    # The human admin who created the account (NULL if they were later deleted).
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    # When set, the account is disabled: all its keys are rejected at auth time.
    disabled_at = Column(DateTime, nullable=True)

    organization = relationship("Organization")
    user = relationship("User", foreign_keys=[user_id])
    created_by = relationship("User", foreign_keys=[created_by_user_id])
