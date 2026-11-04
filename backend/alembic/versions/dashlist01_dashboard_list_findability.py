"""Dashboards list: when a dashboard was edited, stars, and "seen"

Revision ID: dashlist01
Revises: auditcontract01
Create Date: 2026-11-04

- `artifacts.content_updated_at`: when the dashboard's content last
  changed. `updated_at` also moves on every data refresh, so it cannot say
  "edited". Backfilled from the newest completed, non-deleted version; else
  the live release's publish time; else the dashboard's creation; else (no
  created_at) the upgrade's own time, in naive UTC. Known
  imprecisions: the newest version may be a copy made for editing (later
  than the real edit, never earlier), and dashboards migrated by dashfc08
  without a version left get that migration's publish time.
- `artifact_stars`: one row per (dashboard, user) who starred it.
- `artifact_list_seen`: when each user last looked at Shared, per org; the
  watermark for "New".

Additive. Idempotent both ways: the dashboard corpus applies the upgrade
unstamped before `upgrade head` and undoes it unstamped, so every step
checks first. The backfill always runs (only over NULLs), because rows can
arrive between the unstamped apply and the stamped one. On the plain
`corpus` test fixture the backfill therefore runs unstamped at dashfc07,
before dashfc08's data migration, and the later stamped run fills NULLs
only: a test that checks backfilled edited times after the data migration
must use `migrated_corpus`, or downgrade below this revision first (as
test_schema_dashlist01 does). Each table and index is checked on its own:
SQLite DDL is not transactional, so a run that stopped halfway is finished
by the next one.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashlist01"
down_revision: str | None = "auditcontract01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)
EDITED_INDEX = "ix_artifacts_content_updated_at"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def _timestamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    ]


def upgrade() -> None:
    if not _has_column("artifacts", "content_updated_at"):
        with op.batch_alter_table("artifacts") as batch:
            batch.add_column(sa.Column("content_updated_at", sa.DateTime(), nullable=True))
    if not _has_index("artifacts", EDITED_INDEX):
        op.create_index(EDITED_INDEX, "artifacts", ["content_updated_at"])

    if not _has_table("artifact_stars"):
        op.create_table(
            "artifact_stars",
            sa.Column("id", S36, nullable=False),
            sa.Column("artifact_id", S36, nullable=False),
            sa.Column("user_id", S36, nullable=False),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id", name="pk_artifact_stars"),
            sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], name="fk_artifact_stars_artifact_id",
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_artifact_stars_user_id",
                                    ondelete="CASCADE"),
            sa.UniqueConstraint("artifact_id", "user_id", name="uq_artifact_stars_artifact_user"),
        )
    for name, column in (("ix_artifact_stars_artifact_id", "artifact_id"), ("ix_artifact_stars_user_id", "user_id")):
        if not _has_index("artifact_stars", name):
            op.create_index(name, "artifact_stars", [column])

    if not _has_table("artifact_list_seen"):
        op.create_table(
            "artifact_list_seen",
            sa.Column("id", S36, nullable=False),
            sa.Column("user_id", S36, nullable=False),
            sa.Column("organization_id", S36, nullable=False),
            sa.Column("shared_seen_at", sa.DateTime(), nullable=False),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id", name="pk_artifact_list_seen"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_artifact_list_seen_user_id",
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                    name="fk_artifact_list_seen_organization_id", ondelete="CASCADE"),
            sa.UniqueConstraint("user_id", "organization_id", name="uq_artifact_list_seen_user_org"),
        )

    # Always, and only over NULLs: a value already set is kept. The last
    # fallback is now in naive UTC, as the app writes every timestamp:
    # SQLite's CURRENT_TIMESTAMP is UTC; Postgres's is in the session's zone.
    now_utc = ("(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')" if op.get_bind().dialect.name == "postgresql"
               else "CURRENT_TIMESTAMP")
    op.execute(sa.text(
        "UPDATE artifacts SET content_updated_at = COALESCE("
        "(SELECT MAX(v.created_at) FROM artifact_versions v"
        " WHERE v.artifact_id = artifacts.id AND v.status = 'completed' AND v.deleted_at IS NULL),"
        " (SELECT r.published_at FROM artifact_releases r WHERE r.id = artifacts.published_release_id),"
        f" artifacts.created_at, {now_utc})"
        " WHERE content_updated_at IS NULL"
    ))


def downgrade() -> None:
    # Each object only if it exists (lesson 21): the corpus undoes this
    # revision unstamped, on databases that may never have had it.
    if _has_table("artifact_list_seen"):
        op.drop_table("artifact_list_seen")
    if _has_table("artifact_stars"):
        op.drop_table("artifact_stars")  # takes its indexes with it
    if _has_index("artifacts", EDITED_INDEX):
        op.drop_index(EDITED_INDEX, table_name="artifacts")
    if _has_column("artifacts", "content_updated_at"):
        with op.batch_alter_table("artifacts") as batch:
            batch.drop_column("content_updated_at")
