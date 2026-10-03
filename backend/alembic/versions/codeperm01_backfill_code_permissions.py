"""backfill view_code / run_custom_code onto every existing role

Revision ID: codeperm01
Revises: overlayconn01
Create Date: 2025-02-14

`view_code` and `run_custom_code` are new *withholdable* org permissions, which
means they are deliberately not baseline — a baseline permission is granted to
every member by the resolver and so could never be taken away, which is the
entire point of the feature.

The cost of that choice is this migration. A new non-baseline permission
defaults to NOT granted, so without a backfill every existing role would
silently lose code visibility on upgrade. Seeding only the `member` system role
is not enough: a user whose sole assignment is a custom role never picks up
member's permissions (the resolver adds only BASELINE_PERMISSIONS to them).

So: append both strings to EVERY role row that does not already hold
full_admin_access, system and custom alike. The wildcard roles need nothing —
`full_admin_access` already resolves to everything.

Idempotent: a role that already carries a string is left as-is, so a re-run
(or a partially-applied upgrade) cannot duplicate entries.
"""
import json

import sqlalchemy as sa
from alembic import op

revision = 'codeperm01'
down_revision = 'overlayconn01'
branch_labels = None
depends_on = None

CODE_PERMISSIONS = ['view_code', 'run_custom_code']
FULL_ADMIN = 'full_admin_access'


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


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, permissions FROM roles")).fetchall()

    for role_id, raw in rows:
        perms = _load(raw)
        # A full admin already implies every org permission via the wildcard;
        # appending would just make the stored row noisier than the role is.
        if FULL_ADMIN in perms:
            continue
        missing = [p for p in CODE_PERMISSIONS if p not in perms]
        if not missing:
            continue
        conn.execute(
            sa.text("UPDATE roles SET permissions = :perms WHERE id = :id"),
            {"perms": json.dumps(perms + missing), "id": role_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, permissions FROM roles")).fetchall()

    for role_id, raw in rows:
        perms = _load(raw)
        remaining = [p for p in perms if p not in CODE_PERMISSIONS]
        if len(remaining) == len(perms):
            continue
        conn.execute(
            sa.text("UPDATE roles SET permissions = :perms WHERE id = :id"),
            {"perms": json.dumps(remaining), "id": role_id},
        )
