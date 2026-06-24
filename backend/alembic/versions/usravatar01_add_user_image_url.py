"""add image_url to users

Stores the public path to a user's uploaded avatar (served via
/api/users/avatar/{key}). NULL means no avatar — the UI falls back to the
initial-based placeholder.

Revision ID: usravatar01
Revises: judge0001
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa


revision = "usravatar01"
down_revision = "judge0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("image_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "image_url")
