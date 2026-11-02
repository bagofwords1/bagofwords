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
already carrying duplicates (keeping the most-privileged, then oldest, row,
merging the losing rows' directory/attribute data onto it, and HARD-deleting the
rest — membership removal is a hard delete everywhere else) and then adds a
partial unique index so the duplicate cannot be created again.

Scope notes:
- Only ``user_id IS NOT NULL`` rows are constrained. Open invites (``user_id``
  NULL) are intentionally left unconstrained — several can coexist by email and
  are resolved at claim time.
- ``deleted_at IS NULL`` is part of the predicate so a row soft-deleted by some
  other path never blocks a legitimate re-add. Both SQLite and PostgreSQL support
  partial indexes.
"""
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

    # Repair: for each (user_id, organization_id) that has more than one live row,
    # keep ONE and HARD-DELETE the rest. Hard delete (not soft) because membership
    # removal is a hard delete everywhere else — a soft-deleted row would keep its
    # user_id and reappear in the many (user, org) reads that don't filter
    # deleted_at, re-triggering the very MultipleResultsFound this migration fixes.
    #
    # Only the duplicated groups are loaded (GROUP BY ... HAVING), not the whole
    # table. Within a group the kept row is the most-privileged, then oldest, then
    # lowest id — deterministic and identical to claim_pending_invite's ordering.
    dup_groups = bind.execute(
        sa.text(
            "SELECT user_id, organization_id FROM memberships "
            "WHERE user_id IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY user_id, organization_id HAVING COUNT(*) > 1"
        )
    ).fetchall()

    for user_id, org_id in dup_groups:
        rows = bind.execute(
            sa.text(
                "SELECT id, directory_provider, checkins_opt_out FROM memberships "
                "WHERE user_id = :uid AND organization_id = :oid AND deleted_at IS NULL "
                "ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'member' THEN 1 ELSE 2 END, "
                "created_at ASC, id ASC"
            ),
            {"uid": user_id, "oid": org_id},
        ).fetchall()
        keep = rows[0]
        losers = rows[1:]
        if not losers:
            continue
        keep_id = keep[0]

        # Merge the losing rows' data onto the kept row so nothing a directory or
        # an admin set is silently lost: directory_provider (ownership for
        # deprovisioning), checkins_opt_out, and the JSON attribute bags
        # (profile_attributes for RLS, admin-entered custom_attributes).
        merged_dp = keep[1]
        if merged_dp is None:
            for lo in losers:
                if lo[1] is not None:
                    merged_dp = lo[1]
                    break
        merged_co = bool(keep[2]) or any(bool(lo[2]) for lo in losers)
        bind.execute(
            sa.text(
                "UPDATE memberships SET directory_provider = :dp, checkins_opt_out = :co "
                "WHERE id = :id"
            ),
            {"dp": merged_dp, "co": merged_co, "id": keep_id},
        )
        # JSON bags: copy a loser's value into the kept row only when the kept
        # row's is NULL, entirely in-DB so no JSON crosses the Python boundary
        # (asyncpg/psycopg type handling differs by dialect).
        for lo in losers:
            for col in ("profile_attributes", "custom_attributes"):
                bind.execute(
                    sa.text(
                        f"UPDATE memberships SET {col} = "
                        f"(SELECT {col} FROM memberships WHERE id = :lose) "
                        f"WHERE id = :keep AND {col} IS NULL"
                    ),
                    {"lose": lo[0], "keep": keep_id},
                )

        # Hard-delete the losers. group_membership.membership_id is ON DELETE
        # CASCADE; any other membership-keyed RBAC on a live (user_id-set) row was
        # already materialized onto the user, so nothing is orphaned.
        bind.execute(
            sa.text("DELETE FROM memberships WHERE id = :id"),
            [{"id": lo[0]} for lo in losers],
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
