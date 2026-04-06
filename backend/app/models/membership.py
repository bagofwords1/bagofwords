from sqlalchemy import Column, ForeignKey, Table, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
import uuid

class Membership(BaseSchema):
    __tablename__ = 'memberships'

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), primary_key=True)
    email = Column(String, nullable=True)
    invite_token = Column(String(36), nullable=True, unique=True, default=lambda: str(uuid.uuid4()))

    user = relationship("User", back_populates="memberships")
    organization = relationship("Organization", back_populates="memberships")

    role = Column(String, nullable=False, default='member')