"""admin-set member attributes

Adds ``memberships.custom_attributes`` (nullable JSON): key/value attributes an
org admin sets on a member (e.g. ``qlik_user_id``). Kept apart from
``profile_attributes``, which the identity-provider sync replaces wholesale on
every login.

Revision ID: membattr01
Revises: mrgheads07
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op


revision = "membattr01"
down_revision = "mrgheads07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("memberships") as batch_op:
        batch_op.add_column(sa.Column("custom_attributes", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("memberships") as batch_op:
        batch_op.drop_column("custom_attributes")
