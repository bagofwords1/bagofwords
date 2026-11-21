"""Persist folder choices for automation and application scopes."""
from alembic import op
import sqlalchemy as sa

revision = "folder10"
down_revision = "folder09"
branch_labels = depends_on = None

tables = ("connected_applications", "webhooks", "scheduled_prompts")


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table in tables:
        if "agent_folder_ids" not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("agent_folder_ids", sa.JSON(), nullable=True))
    if inspector.has_table("external_platform_folder_scopes"):
        return
    op.create_table(
        "external_platform_folder_scopes",
        sa.Column("platform_id", sa.String(36), sa.ForeignKey("external_platforms.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("external_channel_id", sa.String(255), primary_key=True),
        sa.Column("folder_id", sa.String(36), primary_key=True),
    )


def downgrade():
    db = op.get_bind()
    if db.scalar(sa.text("SELECT COUNT(*) FROM external_platform_folder_scopes")):
        raise RuntimeError("Clear channel folder selections before downgrading")
    for name in tables:
        table = sa.table(name, sa.column("agent_folder_ids", sa.JSON()))
        if any(db.scalars(sa.select(table.c.agent_folder_ids))):
            raise RuntimeError("Clear folder selections before downgrading")
    op.drop_table("external_platform_folder_scopes")
    for table in tables:
        # A native DROP COLUMN, not batch_alter_table: on SQLite batch mode
        # rebuilds the table, and with foreign keys on, dropping the old
        # connected_applications cascades into its OAuth and embedding
        # configs. The column has no index or constraint, which SQLite's
        # DROP COLUMN requires.
        op.drop_column(table, "agent_folder_ids")
