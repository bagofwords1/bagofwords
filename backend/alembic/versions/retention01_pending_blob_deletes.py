"""pending_blob_deletes: file bytes a delete could not remove yet

Retention deletes for good. A file's row goes first and its bytes last; when
removing the bytes fails, the path is kept here and retried every night.

Revision ID: retention01
Revises: mrgheads12
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "retention01"
down_revision = "mrgheads12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_blob_deletes",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pending_blob_deletes_id", "pending_blob_deletes", ["id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_pending_blob_deletes_id", table_name="pending_blob_deletes")
    op.drop_table("pending_blob_deletes")
