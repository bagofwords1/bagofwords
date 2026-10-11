"""Dashboards as first-class entities: schema only

Revision ID: dashfc01
Revises: mrgheads07
Create Date: 2026-10-11

Phase 1 of docs/design/dashboards-first-class.md. Adds the structure a
dashboard needs to stand on its own -- its own settings and sharing, a frozen
published release, and an owner column on the copies of queries and charts --
without reading or filling any of it. Every new column is nullable or has the
same server default as its counterpart on `reports`, so code that does not
know these columns (including the previous release, still serving during a
rolling update) keeps working unchanged.

Constraints are named explicitly: SQLite batch mode only carries named
constraints through a table rebuild (named CHECKs included, alembic
batch.py), and Postgres needs the names to drop them in the downgrade. Each
existing table is changed in one batch block, so SQLite rebuilds it once.

`artifacts.published_release_id` and `artifact_releases.artifact_id` point at
each other. The release table is created first; the pointer is added to
`artifacts` afterwards with ON DELETE SET NULL, so deleting a release clears
the pointer instead of failing.

The downgrade restores main's schema exactly. It refuses -- with a message,
never by deleting -- when data already depends on the new structure: a
dashboard, chart or viewer result without a conversation, any release, share
or release-owned copy, or a value set in one of the dashboard columns it
would drop. Removing those would lose data the previous schema cannot hold.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashfc01"
down_revision: str | None = "mrgheads07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S36 = sa.String(length=36)


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    ]


def upgrade() -> None:
    # ── new tables ───────────────────────────────────────────────────
    op.create_table(
        "artifact_releases",
        sa.Column("id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("organization_id", S36, nullable=False),
        sa.Column("source_version_id", S36, nullable=True),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="publishing"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("published_by", S36, nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("bow_source_access", sa.JSON(), nullable=True),
        sa.Column("theme_name", sa.String(), nullable=True),
        sa.Column("theme_overrides", sa.JSON(), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=512), nullable=True),
        sa.Column("pptx_path", sa.String(length=512), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_releases"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"],
                                name="fk_artifact_releases_artifact_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"],
                                name="fk_artifact_releases_organization_id"),
        sa.ForeignKeyConstraint(["source_version_id"], ["artifact_versions.id"],
                                name="fk_artifact_releases_source_version_id", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], name="fk_artifact_releases_published_by"),
    )
    op.create_index("ix_artifact_releases_artifact_id", "artifact_releases", ["artifact_id"])
    op.create_index("ix_artifact_releases_organization_id", "artifact_releases", ["organization_id"])

    op.create_table(
        "artifact_release_data_source_association",
        sa.Column("artifact_release_id", S36, nullable=False),
        sa.Column("data_source_id", S36, nullable=False),
        sa.PrimaryKeyConstraint("artifact_release_id", "data_source_id", name="pk_artifact_release_ds"),
        sa.ForeignKeyConstraint(["artifact_release_id"], ["artifact_releases.id"],
                                name="fk_artifact_release_ds_release_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["data_source_id"], ["data_sources.id"],
                                name="fk_artifact_release_ds_data_source_id", ondelete="CASCADE"),
    )
    op.create_table(
        "artifact_release_file_association",
        sa.Column("artifact_release_id", S36, nullable=False),
        sa.Column("file_id", S36, nullable=False),
        sa.PrimaryKeyConstraint("artifact_release_id", "file_id", name="pk_artifact_release_file"),
        sa.ForeignKeyConstraint(["artifact_release_id"], ["artifact_releases.id"],
                                name="fk_artifact_release_file_release_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"],
                                name="fk_artifact_release_file_file_id", ondelete="CASCADE"),
    )

    op.create_table(
        "artifact_shares",
        sa.Column("id", S36, nullable=False),
        sa.Column("artifact_id", S36, nullable=False),
        sa.Column("user_id", S36, nullable=True),
        sa.Column("group_id", S36, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_shares"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"],
                                name="fk_artifact_shares_artifact_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_artifact_shares_user_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"],
                                name="fk_artifact_shares_group_id", ondelete="CASCADE"),
        sa.UniqueConstraint("artifact_id", "user_id", name="uq_artifact_share_user"),
        sa.UniqueConstraint("artifact_id", "group_id", name="uq_artifact_share_group"),
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)",
            name="ck_artifact_shares_one_principal",
        ),
    )
    op.create_index("ix_artifact_shares_artifact_id", "artifact_shares", ["artifact_id"])
    op.create_index("ix_artifact_shares_user_id", "artifact_shares", ["user_id"])
    op.create_index("ix_artifact_shares_group_id", "artifact_shares", ["group_id"])

    # ── existing tables: one batch block each ────────────────────────
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("owner_id", S36, nullable=True))
        batch.add_column(sa.Column("project_id", S36, nullable=True))
        batch.add_column(sa.Column("visibility", sa.String(length=20), nullable=False, server_default="none"))
        batch.add_column(sa.Column("slug", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("shared_run_identity", sa.String(length=20), nullable=False,
                                   server_default="viewer"))
        batch.add_column(sa.Column("include_data_tab", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("chat_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("chat_data_source_ids", sa.JSON(), nullable=True))
        # A soft reference like reports.artifact_chat_model_id: an llm_models id
        # or the sentinel 'org_default', so no foreign key.
        batch.add_column(sa.Column("chat_model_id", S36, nullable=True))
        batch.add_column(sa.Column("cron_schedule", sa.String(), nullable=True))
        batch.add_column(sa.Column("refresh_on_view", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("last_run_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("notification_subscribers", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("published_release_id", S36, nullable=True))
        batch.alter_column("report_id", existing_type=S36, nullable=True)
        batch.create_foreign_key("fk_artifacts_owner_id", "users", ["owner_id"], ["id"])
        batch.create_foreign_key("fk_artifacts_project_id", "projects", ["project_id"], ["id"])
        batch.create_foreign_key("fk_artifacts_published_release_id", "artifact_releases",
                                 ["published_release_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_artifacts_owner_id", "artifacts", ["owner_id"])
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_published_release_id", "artifacts", ["published_release_id"])
    # /d/{slug} carries no organization, so a slug names one dashboard in the
    # whole installation. NULLs (no slug yet) do not collide.
    op.create_index("uq_artifacts_slug", "artifacts", ["slug"], unique=True)

    with op.batch_alter_table("queries") as batch:
        batch.add_column(sa.Column("artifact_release_id", S36, nullable=True))
        batch.create_foreign_key("fk_queries_artifact_release_id", "artifact_releases",
                                 ["artifact_release_id"], ["id"])
        batch.create_check_constraint("ck_queries_single_owner",
                                      "report_id IS NULL OR artifact_release_id IS NULL")
    op.create_index("ix_queries_artifact_release_id", "queries", ["artifact_release_id"])

    with op.batch_alter_table("visualizations") as batch:
        batch.add_column(sa.Column("artifact_release_id", S36, nullable=True))
        batch.alter_column("report_id", existing_type=S36, nullable=True)
        batch.create_foreign_key("fk_visualizations_artifact_release_id", "artifact_releases",
                                 ["artifact_release_id"], ["id"])
        batch.create_check_constraint("ck_visualizations_single_owner",
                                      "report_id IS NULL OR artifact_release_id IS NULL")
    op.create_index("ix_visualizations_artifact_release_id", "visualizations", ["artifact_release_id"])

    with op.batch_alter_table("step_user_results") as batch:
        batch.alter_column("report_id", existing_type=S36, nullable=True)

    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("source_artifact_id", S36, nullable=True))
        batch.create_foreign_key("fk_reports_source_artifact_id", "artifacts", ["source_artifact_id"], ["id"])
    op.create_index("ix_reports_source_artifact_id", "reports", ["source_artifact_id"])


# Data that exists only under the new schema; the downgrade refuses rather
# than delete it. Rows the previous schema cannot hold at all, and values in
# the dashboard columns the downgrade drops (anything but the defaults they
# were added with).
_DEPENDENT_DATA = (
    ("artifacts without a conversation", "SELECT count(*) FROM artifacts WHERE report_id IS NULL"),
    ("charts without a conversation", "SELECT count(*) FROM visualizations WHERE report_id IS NULL"),
    ("viewer results without a conversation", "SELECT count(*) FROM step_user_results WHERE report_id IS NULL"),
    ("published releases", "SELECT count(*) FROM artifact_releases"),
    ("dashboard shares", "SELECT count(*) FROM artifact_shares"),
    ("queries owned by a release", "SELECT count(*) FROM queries WHERE artifact_release_id IS NOT NULL"),
    ("charts owned by a release", "SELECT count(*) FROM visualizations WHERE artifact_release_id IS NOT NULL"),
    ("viewer chats tied to a dashboard", "SELECT count(*) FROM reports WHERE source_artifact_id IS NOT NULL"),
    ("dashboards with their own settings",
     "SELECT count(*) FROM artifacts WHERE owner_id IS NOT NULL OR project_id IS NOT NULL OR slug IS NOT NULL"
     " OR visibility <> 'none' OR shared_run_identity <> 'viewer' OR include_data_tab = :f OR chat_enabled = :t"
     " OR chat_data_source_ids IS NOT NULL OR chat_model_id IS NOT NULL OR cron_schedule IS NOT NULL"
     " OR refresh_on_view = :f OR last_run_at IS NOT NULL OR notification_subscribers IS NOT NULL"
     " OR published_release_id IS NOT NULL"),
)


def _refuse_if_data_depends_on_new_schema() -> None:
    from alembic import context

    if context.is_offline_mode():
        # `alembic downgrade --sql` cannot look at the data; the generated
        # script carries the warning instead.
        op.execute("-- dashfc01: check first that no data depends on the dashboards schema")
        return
    conn = op.get_bind()
    found = []
    for label, sql in _DEPENDENT_DATA:
        n = conn.execute(sa.text(sql), {"t": True, "f": False}).scalar() or 0
        if n:
            found.append(f"{n} {label}")
    if found:
        raise RuntimeError(
            "dashfc01 downgrade refused: this data exists only under the dashboards schema and the "
            "previous schema cannot hold it: " + "; ".join(found) + ". Nothing was changed. Restore the "
            "pre-upgrade backup instead, or remove this data deliberately first."
        )


def downgrade() -> None:
    _refuse_if_data_depends_on_new_schema()

    op.drop_index("ix_reports_source_artifact_id", table_name="reports")
    with op.batch_alter_table("reports") as batch:
        batch.drop_constraint("fk_reports_source_artifact_id", type_="foreignkey")
        batch.drop_column("source_artifact_id")

    with op.batch_alter_table("step_user_results") as batch:
        batch.alter_column("report_id", existing_type=S36, nullable=False)

    op.drop_index("ix_visualizations_artifact_release_id", table_name="visualizations")
    with op.batch_alter_table("visualizations") as batch:
        batch.drop_constraint("ck_visualizations_single_owner", type_="check")
        batch.drop_constraint("fk_visualizations_artifact_release_id", type_="foreignkey")
        batch.drop_column("artifact_release_id")
        batch.alter_column("report_id", existing_type=S36, nullable=False)

    op.drop_index("ix_queries_artifact_release_id", table_name="queries")
    with op.batch_alter_table("queries") as batch:
        batch.drop_constraint("ck_queries_single_owner", type_="check")
        batch.drop_constraint("fk_queries_artifact_release_id", type_="foreignkey")
        batch.drop_column("artifact_release_id")

    for name in ("uq_artifacts_slug", "ix_artifacts_published_release_id",
                 "ix_artifacts_project_id", "ix_artifacts_owner_id"):
        op.drop_index(name, table_name="artifacts")
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_constraint("fk_artifacts_published_release_id", type_="foreignkey")
        batch.drop_constraint("fk_artifacts_project_id", type_="foreignkey")
        batch.drop_constraint("fk_artifacts_owner_id", type_="foreignkey")
        for column in ("published_release_id", "notification_subscribers", "last_run_at",
                       "refresh_on_view", "cron_schedule", "chat_model_id", "chat_data_source_ids",
                       "chat_enabled", "include_data_tab", "shared_run_identity", "slug",
                       "visibility", "project_id", "owner_id"):
            batch.drop_column(column)
        batch.alter_column("report_id", existing_type=S36, nullable=False)

    op.drop_table("artifact_shares")
    op.drop_table("artifact_release_file_association")
    op.drop_table("artifact_release_data_source_association")
    op.drop_table("artifact_releases")
