"""Create the shared organization tree and optional agent placement."""

import sqlalchemy as sa
from alembic import op

revision = "folder01"
down_revision = "dashacc01"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("folders"):
        return
    op.create_table(
        "folders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("folders.id")),
        sa.Column("creator_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("inherits_access", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("deleted_at", sa.DateTime()),
    )
    op.create_index(
        "uq_folder_sibling",
        "folders",
        ["organization_id", sa.text("coalesce(parent_id, '')"), sa.text("lower(name)")],
        unique=True,
    )
    with op.batch_alter_table("data_sources") as batch:
        batch.add_column(sa.Column("folder_id", sa.String(36)))
        batch.add_column(sa.Column("inherits_folder_access", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.create_foreign_key("fk_agent_folder", "folders", ["folder_id"], ["id"])
        batch.create_index("ix_agent_folder", ["folder_id"])


def downgrade():
    with op.batch_alter_table("data_sources") as batch:
        batch.drop_constraint("fk_agent_folder", type_="foreignkey")
        batch.drop_index("ix_agent_folder")
        batch.drop_column("inherits_folder_access")
        batch.drop_column("folder_id")
    op.drop_table("folders")
