"""rbac mvp: tables, system roles, membership + ds-membership backfill, ds creator grants

Revision ID: e6f7g8h9i0j1
Revises: a1b2c3d4e5f6, d5e6f7g8h9i0
Create Date: 2026-04-06 00:00:00.000000

Creates the RBAC tables (roles, groups, group_memberships, role_assignments,
resource_grants), seeds system admin/member roles with the MVP permission set,
backfills role_assignments from existing memberships, backfills resource_grants
from data_source_memberships, and grants `manage` on each data_source to its
owner_user_id. Also serves as a merge of the two outstanding alembic heads.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid
from datetime import datetime


# revision identifiers, used by Alembic.
revision: str = 'e6f7g8h9i0j1'
down_revision: Union[str, Sequence[str], None] = ('a1b2c3d4e5f6', 'd5e6f7g8h9i0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# MVP default member permissions (mirrors permissions_registry.DEFAULT_MEMBER_PERMISSIONS)
_MEMBER_PERMISSIONS = [
    'view_reports',
    'create_reports',
    'update_reports',
    'delete_reports',
    'publish_reports',
    'manage_files',
    'view_members',
]

# Legacy data_source_membership perms → MVP data_source resource grants.
# Old DSMs only ever conveyed read/query access, so the MVP equivalent is `view`+`view_schema`.
_LEGACY_DSM_GRANT = ['view', 'view_schema']


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

    conn = op.get_bind()

    # --- Backfill role_assignments from memberships ---
    memberships = conn.execute(
        sa.text(
            "SELECT user_id, organization_id, role FROM memberships "
            "WHERE user_id IS NOT NULL AND deleted_at IS NULL"
        )
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

    assignment_rows = [
        {
            'id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'organization_id': m.organization_id,
            'role_id': admin_role_id if m.role == 'admin' else member_role_id,
            'principal_type': 'user',
            'principal_id': m.user_id,
        }
        for m in memberships
    ]
    if assignment_rows:
        op.bulk_insert(role_assignments_table, assignment_rows)

    # --- Backfill resource_grants from data_source_memberships ---
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

    ds_memberships = conn.execute(
        sa.text(
            """
            SELECT dsm.data_source_id, dsm.principal_type, dsm.principal_id, ds.organization_id
            FROM data_source_memberships dsm
            JOIN data_sources ds ON ds.id = dsm.data_source_id
            WHERE dsm.deleted_at IS NULL
            """
        )
    ).fetchall()

    grant_rows = [
        {
            'id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'organization_id': dsm.organization_id,
            'resource_type': 'data_source',
            'resource_id': dsm.data_source_id,
            'principal_type': dsm.principal_type,
            'principal_id': dsm.principal_id,
            'permissions': list(_LEGACY_DSM_GRANT),
        }
        for dsm in ds_memberships
    ]

    # --- Backfill data_source creator grants (manage) ---
    # Track (resource_id, principal_id) to dedupe against any DSM row above.
    seen = {(g['resource_id'], g['principal_id']) for g in grant_rows}

    creators = conn.execute(
        sa.text(
            "SELECT id, organization_id, owner_user_id FROM data_sources "
            "WHERE owner_user_id IS NOT NULL"
        )
    ).fetchall()

    for ds in creators:
        key = (ds.id, ds.owner_user_id)
        if key in seen:
            # Owner already has a row from DSM backfill — upgrade it to `manage`.
            for row in grant_rows:
                if row['resource_id'] == ds.id and row['principal_id'] == ds.owner_user_id:
                    row['permissions'] = ['manage']
                    break
            continue
        grant_rows.append({
            'id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'organization_id': ds.organization_id,
            'resource_type': 'data_source',
            'resource_id': ds.id,
            'principal_type': 'user',
            'principal_id': ds.owner_user_id,
            'permissions': ['manage'],
        })
        seen.add(key)

    if grant_rows:
        op.bulk_insert(resource_grants_table, grant_rows)


def downgrade() -> None:
    op.drop_table('resource_grants')
    op.drop_table('role_assignments')
    op.drop_table('group_memberships')
    op.drop_table('groups')
    op.drop_table('roles')
