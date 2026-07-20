"""add profile_attributes to memberships

Stores per-user, per-org profile attributes synced from the org's identity
provider (Entra ID Graph /me — job title, department, etc.). NULL/{} means
nothing synced. Rendered into the agent's <user_profile> context block.

Revision ID: entraprof01
Revises: img2gen3ovr4
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa


revision = "entraprof01"
down_revision = "img2gen3ovr4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memberships", sa.Column("profile_attributes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("memberships", "profile_attributes")
