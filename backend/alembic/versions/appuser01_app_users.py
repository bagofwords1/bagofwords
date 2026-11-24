"""app users: access_workspace permission, app_user system role, host new-user role

Revision ID: appuser01
Revises: dailyquota01
Create Date: 2026-11-24

An *app user* is an organization member who may use the published artifacts
shared with them (embedded in a host application, at /d/{ref}, or from the
/apps launcher) but not the BOW workspace itself. The difference is one
withholdable org permission, `access_workspace`.

1. Backfill `access_workspace` onto EVERY existing role that is not a full
   admin (the codeperm01 pattern): a new non-baseline permission defaults to
   not granted, and the resolver now grants the baseline (reports, files) only
   alongside it, so without this every existing member would lose the
   workspace on upgrade.
2. Seed the global `app_user` system role: the role without it.
3. `application_embedding_config.new_user_role`: the role a host application
   gives the users it creates on first sign-in. Existing host applications keep
   creating members (their behavior before this migration); new ones default
   to `app_user`.
4. `memberships.created_via_host_application_id`: which host application a
   membership arrived through. Display and audit only, never authorization. A
   soft reference (no FK), like `default_llm_model_id`.

Idempotent in both directions (the dashboard test corpus replays schema-only
revisions unstamped, see tests/fixtures/dashboard_corpus/alembic_at.py).
"""
import json
import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = 'appuser01'
down_revision = 'dailyquota01'
branch_labels = None
depends_on = None

WORKSPACE_ACCESS = 'access_workspace'
FULL_ADMIN = 'full_admin_access'
APP_USER_ROLE = 'app_user'


def _load(raw):
    """Roles store permissions as JSON; SQLite hands back a string."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    try:
        parsed = json.loads(raw)
        return list(parsed) if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _has_column(conn, table: str, column: str) -> bool:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Every existing non-admin role keeps the workspace.
    for role_id, raw in conn.execute(sa.text("SELECT id, permissions FROM roles")).fetchall():
        perms = _load(raw)
        if FULL_ADMIN in perms or WORKSPACE_ACCESS in perms:
            continue
        conn.execute(
            sa.text("UPDATE roles SET permissions = :perms WHERE id = :id"),
            {"perms": json.dumps(perms + [WORKSPACE_ACCESS]), "id": role_id},
        )

    # 2. The app_user system role. uq_roles_org_name does not stop a duplicate
    #    global row (NULL organization ids are distinct), so guard by hand.
    exists = conn.execute(sa.text(
        "SELECT 1 FROM roles WHERE name = :name AND organization_id IS NULL AND is_system = :yes"
    ), {"name": APP_USER_ROLE, "yes": True}).first()
    if not exists:
        now = datetime.utcnow()
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
        op.bulk_insert(roles_table, [{
            'id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'organization_id': None,
            'name': APP_USER_ROLE,
            'description': 'Can use the apps shared with them, not the workspace',
            'permissions': [],
            'is_system': True,
        }])

    # 3. Existing host applications keep creating members.
    if not _has_column(conn, 'application_embedding_config', 'new_user_role'):
        op.add_column('application_embedding_config', sa.Column(
            'new_user_role', sa.String(50), nullable=False, server_default=APP_USER_ROLE))
        conn.execute(sa.text("UPDATE application_embedding_config SET new_user_role = 'member'"))

    # 4. Where a membership came from.
    if not _has_column(conn, 'memberships', 'created_via_host_application_id'):
        op.add_column('memberships', sa.Column('created_via_host_application_id', sa.String(36), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, 'memberships', 'created_via_host_application_id'):
        op.drop_column('memberships', 'created_via_host_application_id')
    if _has_column(conn, 'application_embedding_config', 'new_user_role'):
        op.drop_column('application_embedding_config', 'new_user_role')

    # App users become members again: without the gate the role means nothing,
    # and a membership whose only role is gone would fall to the legacy net.
    role = conn.execute(sa.text(
        "SELECT id FROM roles WHERE name = :name AND organization_id IS NULL"
    ), {"name": APP_USER_ROLE}).first()
    member = conn.execute(sa.text(
        "SELECT id FROM roles WHERE name = 'member' AND organization_id IS NULL"
    )).first()
    if role:
        if member:
            conn.execute(sa.text("UPDATE role_assignments SET role_id = :m WHERE role_id = :r"),
                         {"m": member[0], "r": role[0]})
        else:
            conn.execute(sa.text("DELETE FROM role_assignments WHERE role_id = :r"), {"r": role[0]})
        conn.execute(sa.text("DELETE FROM roles WHERE id = :r"), {"r": role[0]})
    conn.execute(sa.text("UPDATE memberships SET role = 'member' WHERE role = :name"), {"name": APP_USER_ROLE})

    for role_id, raw in conn.execute(sa.text("SELECT id, permissions FROM roles")).fetchall():
        perms = _load(raw)
        if WORKSPACE_ACCESS not in perms:
            continue
        conn.execute(
            sa.text("UPDATE roles SET permissions = :perms WHERE id = :id"),
            {"perms": json.dumps([p for p in perms if p != WORKSPACE_ACCESS]), "id": role_id},
        )
