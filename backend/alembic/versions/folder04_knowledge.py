"""Folder knowledge attachments and instruction target snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "folder04"
down_revision = "folder03"
branch_labels = None
depends_on = None

LINKS = (
    ("folder_instructions", "instruction_id", "instructions"),
    ("folder_files", "file_id", "files"),
    ("folder_eval_suites", "suite_id", "test_suites"),
)


def _applied():
    """Idempotent: the dashboard test corpus replays this revision unstamped
    (tests/fixtures/dashboard_corpus/alembic_at.py), then `upgrade head` runs
    it again."""
    return "owner_folder_id" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("files")}


def upgrade():
    if _applied():
        return
    for table in ("files", "test_suites"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("owner_folder_id", sa.String(36)))
            batch.create_foreign_key("fk_" + table + "_owner_folder", "folders", ["owner_folder_id"], ["id"])
            batch.create_index("ix_" + table + "_owner_folder_id", ["owner_folder_id"])
    op.add_column("test_results", sa.Column("folder_id", sa.String(36)))
    for name, key, target in LINKS:
        op.create_table(
            name,
            sa.Column("folder_id", sa.String(36), sa.ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True),
            sa.Column(key, sa.String(36), sa.ForeignKey(target + ".id", ondelete="CASCADE"), primary_key=True),
        )
        op.create_index("ix_" + name + "_item", name, [key])
    op.add_column("instructions", sa.Column("scope_mode", sa.String(16), server_default="global", nullable=False))
    op.execute(
        "UPDATE instructions SET scope_mode='targeted' WHERE id IN (SELECT instruction_id FROM instruction_data_source_association)"
    )
    op.add_column("instruction_versions", sa.Column("folder_ids", sa.JSON()))
    op.add_column("instruction_versions", sa.Column("scope_mode", sa.String(16)))


def downgrade():
    if not _applied():
        return
    # Older readers interpret empty agent targets as global, including build
    # history. Refuse lossy downgrades before changing any schema.
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT 1 FROM instructions WHERE scope_mode='targeted' AND NOT EXISTS "
                           "(SELECT 1 FROM instruction_data_source_association a "
                           "WHERE a.instruction_id=instructions.id) LIMIT 1")):
        raise RuntimeError("Cannot downgrade folder-only instruction scope. Restore a pre-upgrade backup.")
    versions = sa.table("instruction_versions", sa.column("scope_mode"), sa.column("data_source_ids", sa.JSON))
    for (targets,) in bind.execute(sa.select(versions.c.data_source_ids).where(versions.c.scope_mode == "targeted")):
        if not targets:
            raise RuntimeError("Cannot downgrade folder-only instruction history. Restore a pre-upgrade backup.")
    op.drop_column("test_results", "folder_id")
    for table in ("files", "test_suites"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint("fk_" + table + "_owner_folder", type_="foreignkey")
            batch.drop_index("ix_" + table + "_owner_folder_id")
            batch.drop_column("owner_folder_id")
    op.drop_column("instruction_versions", "scope_mode")
    op.drop_column("instruction_versions", "folder_ids")
    op.drop_column("instructions", "scope_mode")
    for name, _, _ in reversed(LINKS):
        op.drop_table(name)
