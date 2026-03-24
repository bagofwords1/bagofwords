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
    'export_query',
    'view_organizations',
    'view_llm_settings',
    'view_organization_members',
    'manage_organization_external_platforms',
    'view_instructions',
    'view_global_instructions',
    'suggest_instructions',
    'create_completion_feedback',
    'view_entities',
    'refresh_entities',
    'suggest_entities',
    'withdraw_entities',
    'view_evals',
    'run_evals',
    'view_builds',
}

ADMIN_PERMISSIONS = {
    'create_data_source',
    'delete_data_source',
    'update_data_source',
    'manage_connections',
    'view_connections',
    'view_settings',
    'modify_settings',
    'add_organization_members',
    'update_organization_members',
    'remove_organization_members',
    'view_organization_members',
    'manage_llm_settings',
    'view_data_source_full_schema',
    'manage_organization_settings',
    'view_organization_settings',
    'view_organization_overview',
    'create_instructions',
    'update_instructions',
    'delete_instructions',
    'view_hidden_instructions',
    'view_all_completion_feedbacks',
    'manage_data_source_memberships',
    'view_entities',
    'create_entities',
    'update_entities',
    'delete_entities',
    'approve_entities',
    'reject_entities',
    'manage_evals',
    'view_builds',
    'create_builds',
    'train_mode',
    # Enterprise features
    'view_audit_logs',
    'manage_scim',
    # RBAC management
    'manage_roles',
    'manage_groups',
    'manage_role_assignments',
    'manage_resource_grants',
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