"""backfill role_assignments for memberships that have none

Revision ID: rbacbf01
Revises: perfidx01
Create Date: 2026-07-03 12:00:00.000000

The original RBAC migration (e6f7g8h9i0j1) backfilled role_assignments from
memberships as a one-time event. Memberships created SINCE then by provisioning
paths that only set the legacy ``Membership.role`` string — notably SCIM
``create_user`` (and LDAP/OIDC users not placed in a role-bearing group) — have
NO role_assignment, so the RBAC resolver (the source of truth) returns zero
permissions for them.

This migration heals that: for every registered membership that has NO direct
user role_assignment in its org, it inserts the system-role assignment implied
by ``Membership.role`` ('admin' -> admin role, otherwise member role). It is
idempotent (only inserts where missing) and additive (never removes a grant), so
it is safe to re-run and safe on both SQLite and Postgres.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid
from datetime import datetime


# revision identifiers, used by Alembic.
revision: str = 'rbacbf01'
down_revision: Union[str, Sequence[str], None] = 'perfidx01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.utcnow()

    # System roles are the only roles with organization_id IS NULL, so this
    # identifies them without relying on a dialect-specific boolean literal.
    role_rows = conn.execute(sa.text(
        "SELECT id, name FROM roles "
        "WHERE organization_id IS NULL AND deleted_at IS NULL "
        "AND name IN ('admin', 'member')"
    )).fetchall()
    role_id_by_name = {r.name: r.id for r in role_rows}
    if 'admin' not in role_id_by_name or 'member' not in role_id_by_name:
        # System roles not present (should never happen post-e6f7g8h9i0j1); skip.
        return

    # (org, user) pairs that already hold a direct user role assignment.
    existing = {
        (r.organization_id, r.principal_id)
        for r in conn.execute(sa.text(
            "SELECT organization_id, principal_id FROM role_assignments "
            "WHERE principal_type = 'user' AND deleted_at IS NULL"
        )).fetchall()
    }

    memberships = conn.execute(sa.text(
        "SELECT user_id, organization_id, role FROM memberships "
        "WHERE user_id IS NOT NULL AND deleted_at IS NULL"
    )).fetchall()

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

    rows = []
    seen = set()
    for m in memberships:
        key = (m.organization_id, m.user_id)
        if key in existing or key in seen:
            continue
        seen.add(key)
        role_id = role_id_by_name['admin'] if m.role == 'admin' else role_id_by_name['member']
        rows.append({
            'id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'organization_id': m.organization_id,
            'role_id': role_id,
            'principal_type': 'user',
            'principal_id': m.user_id,
        })

    if rows:
        op.bulk_insert(role_assignments_table, rows)


def downgrade() -> None:
    # Additive, idempotent healing migration — nothing to reverse. Removing the
    # backfilled assignments could strip permissions from live users, so this is
    # intentionally a no-op.
    pass
