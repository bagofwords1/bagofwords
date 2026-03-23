"""add rbac tables

Revision ID: a1b2c3d4e5f6
Revises: z0a1b2c3d4e5
Create Date: 2026-03-23 00:00:00.000000

Creates roles, groups, group_memberships, role_assignments, and resource_grants
tables for the enterprise RBAC system. Seeds system admin/member roles and
migrates existing membership roles and data_source_memberships to the new tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid
from datetime import datetime


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'z0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Current member permissions (copied from membership.py for migration stability)
_MEMBER_PERMISSIONS = [
    'view_data_source', 'view_reports', 'create_reports', 'update_reports',
    'delete_reports', 'publish_reports', 'rerun_report_steps', 'view_files',
    'upload_files', 'delete_files', 'export_widgets', 'create_text_widgets',
    'update_text_widgets', 'view_text_widgets', 'delete_text_widgets',
    'create_widgets', 'update_widgets', 'delete_widgets', 'view_widgets',
    'view_organizations', 'view_llm_settings', 'view_organization_members',
    'manage_organization_external_platforms', 'view_instructions',
    'create_private_instructions', 'update_private_instructions',
    'delete_private_instructions', 'view_global_instructions',
    'view_private_instructions', 'suggest_instructions',
    'create_completion_feedback', 'view_entities', 'refresh_entities',
    'suggest_entities', 'withdraw_entities', 'view_builds',
]


def upgrade() -> None:
    # --- Create tables ---
    op.create_table(
        'roles',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('permissions', sa.JSON(), nullable=False),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default='false'),
        sa.UniqueConstraint('organization_id', 'name', name='uq_roles_org_name'),
    )

    op.create_table(
        'groups',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('external_id', sa.String(), nullable=True),
        sa.Column('external_provider', sa.String(), nullable=True),
        sa.UniqueConstraint('organization_id', 'name', name='uq_groups_org_name'),
    )

    op.create_table(
        'group_memberships',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('group_id', sa.String(36), sa.ForeignKey('groups.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.UniqueConstraint('group_id', 'user_id', name='uq_group_membership'),
    )

    op.create_table(
        'role_assignments',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('role_id', sa.String(36), sa.ForeignKey('roles.id'), nullable=False),
        sa.Column('principal_type', sa.String(), nullable=False),
        sa.Column('principal_id', sa.String(36), nullable=False),
        sa.UniqueConstraint('organization_id', 'role_id', 'principal_type', 'principal_id', name='uq_role_assignment'),
    )

    op.create_table(
        'resource_grants',
        sa.Column('id', sa.String(36), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('organization_id', sa.String(36), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('resource_type', sa.String(), nullable=False),
        sa.Column('resource_id', sa.String(36), nullable=False),
        sa.Column('principal_type', sa.String(), nullable=False),
        sa.Column('principal_id', sa.String(36), nullable=False),
        sa.Column('permissions', sa.JSON(), nullable=False),
        sa.UniqueConstraint('resource_type', 'resource_id', 'principal_type', 'principal_id', name='uq_resource_grant'),
    )

    # --- Seed system roles ---
    now = datetime.utcnow()
    admin_role_id = str(uuid.uuid4())
    member_role_id = str(uuid.uuid4())

    roles_table = sa.table(
        'roles',
        sa.column('id', sa.String),
        sa.column('created_at', sa.DateTime),
        sa.column('updated_at', sa.DateTime),
        sa.column('organization_id', sa.String),
        sa.column('name', sa.String),
        sa.column('description', sa.Text),
        sa.column('permissions', sa.JSON),
        sa.column('is_system', sa.Boolean),
    )

    op.bulk_insert(roles_table, [
        {
            'id': admin_role_id,
            'created_at': now,
            'updated_at': now,
            'organization_id': None,
            'name': 'admin',
            'description': 'Full administrator access',
            'permissions': ['full_admin_access'],
            'is_system': True,
        },
        {
            'id': member_role_id,
            'created_at': now,
            'updated_at': now,
            'organization_id': None,
            'name': 'member',
            'description': 'Standard member access',
            'permissions': _MEMBER_PERMISSIONS,
            'is_system': True,
        },
    ])

    # --- Migrate existing memberships to role_assignments ---
    conn = op.get_bind()
    memberships = conn.execute(
        sa.text("SELECT id, user_id, organization_id, role FROM memberships WHERE user_id IS NOT NULL AND deleted_at IS NULL")
    ).fetchall()

    role_assignments_table = sa.table(
        'role_assignments',
        sa.column('id', sa.String),
        sa.column('created_at', sa.DateTime),
        sa.column('updated_at', sa.DateTime),
        sa.column('organization_id', sa.String),
        sa.column('role_id', sa.String),
        sa.column('principal_type', sa.String),
        sa.column('principal_id', sa.String),
    )

    assignment_rows = []
    for m in memberships:
        role_id = admin_role_id if m.role == 'admin' else member_role_id
        assignment_rows.append({
            'id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'organization_id': m.organization_id,
            'role_id': role_id,
            'principal_type': 'user',
            'principal_id': m.user_id,
        })

    if assignment_rows:
        op.bulk_insert(role_assignments_table, assignment_rows)

    # --- Migrate data_source_memberships to resource_grants ---
    ds_memberships = conn.execute(
        sa.text("""
            SELECT dsm.id, dsm.data_source_id, dsm.principal_type, dsm.principal_id, ds.organization_id
            FROM data_source_memberships dsm
            JOIN data_sources ds ON ds.id = dsm.data_source_id
            WHERE dsm.deleted_at IS NULL
        """)
    ).fetchall()

    resource_grants_table = sa.table(
        'resource_grants',
        sa.column('id', sa.String),
        sa.column('created_at', sa.DateTime),
        sa.column('updated_at', sa.DateTime),
        sa.column('organization_id', sa.String),
        sa.column('resource_type', sa.String),
        sa.column('resource_id', sa.String),
        sa.column('principal_type', sa.String),
        sa.column('principal_id', sa.String),
        sa.column('permissions', sa.JSON),
    )

    grant_rows = []
    for dsm in ds_memberships:
        grant_rows.append({
            'id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'organization_id': dsm.organization_id,
            'resource_type': 'data_source',
            'resource_id': dsm.data_source_id,
            'principal_type': dsm.principal_type,
            'principal_id': dsm.principal_id,
            'permissions': ['query', 'view_schema'],
        })

    if grant_rows:
        op.bulk_insert(resource_grants_table, grant_rows)


def downgrade() -> None:
    op.drop_table('resource_grants')
    op.drop_table('role_assignments')
    op.drop_table('group_memberships')
    op.drop_table('groups')
    op.drop_table('roles')
