"""Collaborative dashboards: roles, editing conversations, drafts, presence

Revision ID: collab01
Revises: dashfc08
Create Date: 2026-10-23

docs/feedback-loops/2026-10-23-collaborative-dashboards.md. Additive only:
no existing row changes, nothing is backfilled.

- `artifact_shares.role`: 'view' (every existing grant: all a grant meant
  before) or 'edit'.
- `artifacts.editors_can_share`, `artifacts.notes`: the owner's switch and
  the dashboard's README.
- `artifact_drafts`: named drafts. A version with no draft is on the
  implicit one ("Main"), so existing versions need no row.
- `artifact_conversations`: the conversations editing a dashboard besides
  its own `artifacts.report_id`, which keeps counting as one.
- `artifact_versions.base_version_id` (the version a draft version grew
  from; a soft reference, it outlives the row), `.draft_id`, and `.elements`
  (each chart's fingerprint when the version was made: who changed what).
- `visualizations.element_key`: a chart's key kept by its copies; NULL reads
  as the chart's own id.
- `artifact_presence`: who has the dashboard open in a conversation now.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "collab01"
down_revision: str | None = "dashfc08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    ]


def upgrade() -> None:
    # Idempotent: the dashboard corpus stops before dashfc08's data migration
    # and applies this schema-only revision on its own (the code at head needs
    # it); a later `upgrade head` then finds it done.
    if _has_column("visualizations", "element_key"):
        return
    op.create_table(
        "artifact_drafts",
        sa.Column("id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("created_by", S36, nullable=True),
        sa.Column("promoted_release_id", S36, nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_drafts"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], name="fk_artifact_drafts_artifact_id",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                name="fk_artifact_drafts_organization_id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_artifact_drafts_created_by"),
        sa.UniqueConstraint("artifact_id", "slug", name="uq_artifact_drafts_slug"),
    )
    op.create_index("ix_artifact_drafts_artifact_id", "artifact_drafts", ["artifact_id"])

    op.create_table(
        "artifact_conversations",
        sa.Column("id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("report_id", S36, nullable=False),
        sa.Column("user_id", S36, nullable=False),
        sa.Column("draft_id", S36, nullable=True),
        sa.Column("base_version_id", S36, nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_conversations"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], name="fk_artifact_conversations_artifact_id",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], name="fk_artifact_conversations_report_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_artifact_conversations_user_id"),
        sa.ForeignKeyConstraint(["draft_id"], ["artifact_drafts.id"], name="fk_artifact_conversations_draft_id",
                                ondelete="SET NULL"),
        sa.UniqueConstraint("artifact_id", "report_id", name="uq_artifact_conversations_report"),
    )
    op.create_index("ix_artifact_conversations_artifact_id", "artifact_conversations", ["artifact_id"])
    op.create_index("ix_artifact_conversations_report_id", "artifact_conversations", ["report_id"])

    op.create_table(
        "artifact_presence",
        sa.Column("id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("user_id", S36, nullable=False),
        sa.Column("report_id", S36, nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="viewing"),
        sa.Column("element_keys", sa.JSON(), nullable=True),
        sa.Column("completion_id", S36, nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_presence"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], name="fk_artifact_presence_artifact_id",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_artifact_presence_user_id",
                                ondelete="CASCADE"),
    )
    op.create_index("ix_artifact_presence_artifact_id", "artifact_presence", ["artifact_id"])
    op.create_index("ix_artifact_presence_completion_id", "artifact_presence", ["completion_id"])

    with op.batch_alter_table("artifact_shares") as batch:
        batch.add_column(sa.Column("role", sa.String(length=10), nullable=False, server_default="view"))

    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("editors_can_share", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("notes", sa.Text(), nullable=True))

    with op.batch_alter_table("artifact_versions") as batch:
        batch.add_column(sa.Column("base_version_id", S36, nullable=True))
        batch.add_column(sa.Column("draft_id", S36, nullable=True))
        batch.add_column(sa.Column("elements", sa.JSON(), nullable=True))
        batch.create_foreign_key("fk_artifact_versions_draft_id", "artifact_drafts", ["draft_id"], ["id"],
                                 ondelete="SET NULL")
    op.create_index("ix_artifact_versions_draft_id", "artifact_versions", ["draft_id"])

    with op.batch_alter_table("visualizations") as batch:
        batch.add_column(sa.Column("element_key", S36, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("visualizations") as batch:
        batch.drop_column("element_key")

    op.drop_index("ix_artifact_versions_draft_id", table_name="artifact_versions")
    with op.batch_alter_table("artifact_versions") as batch:
        batch.drop_constraint("fk_artifact_versions_draft_id", type_="foreignkey")
        batch.drop_column("elements")
        batch.drop_column("draft_id")
        batch.drop_column("base_version_id")

    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("notes")
        batch.drop_column("editors_can_share")

    with op.batch_alter_table("artifact_shares") as batch:
        batch.drop_column("role")

    op.drop_index("ix_artifact_presence_completion_id", table_name="artifact_presence")
    op.drop_index("ix_artifact_presence_artifact_id", table_name="artifact_presence")
    op.drop_table("artifact_presence")
    op.drop_index("ix_artifact_conversations_report_id", table_name="artifact_conversations")
    op.drop_index("ix_artifact_conversations_artifact_id", table_name="artifact_conversations")
    op.drop_table("artifact_conversations")
    op.drop_index("ix_artifact_drafts_artifact_id", table_name="artifact_drafts")
    op.drop_table("artifact_drafts")
