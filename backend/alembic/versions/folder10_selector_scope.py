"""Persist folder choices for automation and application scopes."""
from alembic import op
import sqlalchemy as sa

revision = "folder10"
down_revision = "folder09"
branch_labels = depends_on = None

tables = ("connected_applications", "webhooks", "scheduled_prompts")


def upgrade():
    for table in tables:
        op.add_column(table, sa.Column("agent_folder_ids", sa.JSON(), nullable=True))
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
        with op.batch_alter_table(table) as batch:
            batch.drop_column("agent_folder_ids")
