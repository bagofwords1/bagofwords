"""Add invite_expires_at to memberships (invite link expiry)

Revision ID: se3f4a5b6c7d
Revises: sd2e3f4a5b6c
Create Date: 2026-06-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "se3f4a5b6c7d"
down_revision: Union[str, None] = "sd2e3f4a5b6c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.add_column(sa.Column("invite_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.drop_column("invite_expires_at")
