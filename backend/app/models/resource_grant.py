from sqlalchemy import Column, String, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class ResourceGrant(BaseSchema):
    __tablename__ = 'resource_grants'
    __table_args__ = (
        UniqueConstraint('resource_type', 'resource_id', 'principal_type', 'principal_id',
                         name='uq_resource_grant'),
    )

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    resource_type = Column(String, nullable=False)    # "data_source" | "connection"
    resource_id = Column(String(36), nullable=False)
    principal_type = Column(String, nullable=False)   # "user" | "group"
    principal_id = Column(String(36), nullable=False)
    permissions = Column(JSON, nullable=False, default=list)  # ["query", "view_schema", "manage"]
