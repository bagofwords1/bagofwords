"""add note column to memberships

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-05-13 00:00:00.000000

Per-org note about each member, written by org admins. Surfaced to the
AI planner as <user_profile> context (org-specific, so it lives on the
membership row, not the user row, since a user can belong to multiple
orgs).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memberships", sa.Column("note", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("memberships", "note")
