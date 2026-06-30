"""add files.source_kind (upload | connector)

Revision ID: filesrc01
Revises: c2d3e4f5a6b7
Create Date: 2026-06-29

Marks files materialized from a connector download (ephemeral, per-turn) vs
user uploads (durable). Defaults existing rows to "upload".
"""
from alembic import op
import sqlalchemy as sa


revision = "filesrc01"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("files") as batch:
        batch.add_column(
            sa.Column("source_kind", sa.String(), nullable=False, server_default="upload")
        )
        batch.create_index("ix_files_source_kind", ["source_kind"])


def downgrade() -> None:
    with op.batch_alter_table("files") as batch:
        batch.drop_index("ix_files_source_kind")
        batch.drop_column("source_kind")
