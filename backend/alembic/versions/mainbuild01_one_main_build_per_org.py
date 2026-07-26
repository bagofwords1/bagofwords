"""enforce a single main instruction build per organization

Revision ID: mainbuild01
Revises: entraprof01
Create Date: 2026-07-26

`instruction_builds.is_main` marks the live instruction set — the build every
read path resolves against. The "only one per organization" rule was documented
on the model and implemented in `promote_build`, but nothing at the database
level enforced it, so two promotions racing inside one org (a user instruction
save finalizing while a training session or git sync promoted its own build)
could leave two rows flagged is_main. That state hard-broke the org: readers
used `scalar_one_or_none()`, which raises MultipleResultsFound, so
`GET /instructions` 500'd ("Failed to load instructions"), the badge counts
500'd, and the agent's instruction context failed to load.

This migration repairs any org already in that state (keeping the newest build
as main and demoting the rest) and then adds a partial unique index so the
duplicate cannot be created again. Both SQLite and PostgreSQL support partial
indexes; a plain unique index on is_main would be wrong (many builds legitimately
share is_main=False).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mainbuild01"
down_revision: Union[str, None] = "entraprof01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "uq_instruction_builds_one_main_per_org"


def upgrade() -> None:
    bind = op.get_bind()

    # Repair: keep the highest build_number per org as main, demote the rest.
    # Tie-break on created_at then id so the choice is deterministic and matches
    # what app.core.main_build.resolve_main_build_id picks at read time.
    rows = bind.execute(
        sa.text(
            "SELECT id, organization_id FROM instruction_builds "
            "WHERE is_main = 1 AND deleted_at IS NULL "
            "ORDER BY organization_id, build_number DESC, created_at DESC, id DESC"
            if bind.dialect.name == "sqlite"
            else
            "SELECT id, organization_id FROM instruction_builds "
            "WHERE is_main = true AND deleted_at IS NULL "
            "ORDER BY organization_id, build_number DESC, created_at DESC, id DESC"
        )
    ).fetchall()

    seen_orgs = set()
    demote = []
    for build_id, org_id in rows:
        if org_id in seen_orgs:
            demote.append(build_id)
        else:
            seen_orgs.add(org_id)

    if demote:
        false_literal = "0" if bind.dialect.name == "sqlite" else "false"
        for build_id in demote:
            bind.execute(
                sa.text(
                    f"UPDATE instruction_builds SET is_main = {false_literal} WHERE id = :id"
                ),
                {"id": build_id},
            )

    # Soft-deleted builds are excluded from the app's main-build lookup, so the
    # index must ignore them too — otherwise a deleted former main would block a
    # legitimate promotion.
    op.create_index(
        INDEX_NAME,
        "instruction_builds",
        ["organization_id"],
        unique=True,
        sqlite_where=sa.text("is_main = 1 AND deleted_at IS NULL"),
        postgresql_where=sa.text("is_main AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="instruction_builds")
