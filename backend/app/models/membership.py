from sqlalchemy import Column, ForeignKey, Table, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
import uuid

# Define base permissions for each role
MEMBER_PERMISSIONS = {
    'view_data_source',
    'view_reports',
    'create_reports',
    'update_reports',
    'delete_reports',
    'publish_reports',
    'rerun_report_steps',
    'view_files',
    'upload_files',
    'delete_files',
    'export_widgets',
    'create_text_widgets',
    'update_text_widgets',
    'view_text_widgets',
    'delete_text_widgets',
    'create_widgets',
    'update_widgets',
    'delete_widgets',
    'view_widgets',
    'view_memories',
    'create_memories',
    'update_memories',
    'delete_memories',
    'rerun_memory_step',
    'view_organizations',
    'view_llm_settings',
    'view_organization_members',
    'view_files'
}

ADMIN_PERMISSIONS = {
    'create_data_source',
    'delete_data_source',
    'update_data_source',
    'view_settings',
    'modify_settings',
    'add_organization_members',
    'update_organization_members',
    'remove_organization_members',
    'view_organization_members',
    'manage_llm_settings',
    'view_data_source_full_schema',
    'manage_organization_settings',
    'view_organization_settings'
}

# Combine permissions for roles
ROLES_PERMISSIONS = {
    'member': MEMBER_PERMISSIONS,
    'admin': ADMIN_PERMISSIONS | MEMBER_PERMISSIONS,  # Combine admin and member permissions
}

class Membership(BaseSchema):
    __tablename__ = 'memberships'

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), primary_key=True)
    email = Column(String, nullable=True)
    invite_token = Column(String(36), nullable=True, unique=True, default=lambda: str(uuid.uuid4()))

    user = relationship("User", back_populates="memberships")
    organization = relationship("Organization", back_populates="memberships")

    role = Column(String, nullable=False, default='member')