"""enforce a single live membership per (user, organization)

Revision ID: memberdup01
Revises: atlv201
Create Date: 2026-11-02

A ``memberships`` row binds a user to an organization. The "one row per
(user, org)" rule was assumed everywhere but never enforced: nothing at the
database level stopped two live rows for the same pair, and several provisioning
paths could create them. The classic trigger was an admin invite (a row with
``email`` set, ``user_id`` NULL) for someone who then signed in through SSO:
the SSO path (SCIM/OIDC/LDAP) created a ``user_id``-keyed row while the still-open
invite was later claimed by ``_attach_open_memberships`` into a SECOND
``user_id``-keyed row.

That state hard-broke the org for the affected user: ``principal_belongs_to_org``
read the membership with ``scalar_one_or_none()``, which raises
MultipleResultsFound on two rows, so ``get_current_organization`` 500'd and every
org-scoped route it gates failed — presenting as "no available models" / unable
to query until an admin deleted one of the rows.

The code side now claims open invites instead of creating second rows (see
``permission_resolver.claim_pending_invite`` and its callers) and reads the
binding with a duplicate-tolerant existence check. This migration repairs any org
already carrying duplicates (keeping the most-privileged, then oldest, row and
soft-deleting the rest) and then adds a partial unique index so the duplicate
cannot be created again.

Scope notes:
- Only ``user_id IS NOT NULL`` rows are constrained. Open invites (``user_id``
  NULL) are intentionally left unconstrained — several can coexist by email and
  are resolved at claim time.
- ``deleted_at IS NULL`` so a retired/soft-deleted former row never blocks a
  legitimate re-add. Both SQLite and PostgreSQL support partial indexes.
"""
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "memberdup01"
down_revision: Union[str, None] = "atlv201"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "uq_memberships_one_live_per_user_org"


def upgrade() -> None:
    bind = op.get_bind()

    # Repair: for each (user_id, organization_id) keep ONE live row and
    # soft-delete the rest. Ordering keeps the most-privileged role, breaking
    # ties on the oldest row (the original invite-derived membership that carries
    # the materialized RBAC), then id, so the choice is deterministic.
    rows = bind.execute(
        sa.text(
            "SELECT id, user_id, organization_id FROM memberships "
            "WHERE user_id IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY user_id, organization_id, "
            "CASE role WHEN 'admin' THEN 0 WHEN 'member' THEN 1 ELSE 2 END, "
            "created_at ASC, id ASC"
        )
    ).fetchall()

    seen = set()
    demote = []
    for row_id, user_id, org_id in rows:
        key = (user_id, org_id)
        if key in seen:
            demote.append(row_id)
        else:
            seen.add(key)

    if demote:
        ts = datetime.utcnow()
        for row_id in demote:
            bind.execute(
                sa.text(
                    "UPDATE memberships SET deleted_at = :ts WHERE id = :id"
                ),
                {"ts": ts, "id": row_id},
            )

    op.create_index(
        INDEX_NAME,
        "memberships",
        ["user_id", "organization_id"],
        unique=True,
        sqlite_where=sa.text("user_id IS NOT NULL AND deleted_at IS NULL"),
        postgresql_where=sa.text("user_id IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="memberships")
