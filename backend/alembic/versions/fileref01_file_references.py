"""file_references: durable pins to connector files (A3)

Revision ID: fileref01
Revises: filesrc01
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa


revision = "fileref01"
down_revision = "filesrc01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "file_references",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("report_id", sa.String(length=36), sa.ForeignKey("reports.id"), nullable=False),
        sa.Column("connection_id", sa.String(length=36), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("external_file_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("mime", sa.String(), nullable=True),
        sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_file_references_report_id", "file_references", ["report_id"])
    op.create_index("ix_file_references_connection_id", "file_references", ["connection_id"])
    op.create_index("ix_file_references_organization_id", "file_references", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_file_references_organization_id", "file_references")
    op.drop_index("ix_file_references_connection_id", "file_references")
    op.drop_index("ix_file_references_report_id", "file_references")
    op.drop_table("file_references")
