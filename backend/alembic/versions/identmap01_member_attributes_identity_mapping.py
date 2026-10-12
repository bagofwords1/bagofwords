"""admin-set member attributes and per-connection identity mapping

Adds two nullable JSON columns:

* ``memberships.custom_attributes`` — key/value attributes an org admin sets on
  a member (e.g. ``qlik_user_id``). Kept apart from ``profile_attributes``,
  which the identity-provider sync overwrites wholesale on every login.
* ``connections.identity_mapping`` — slot → source expression rules that pin
  the identity a connection acts as for the signed-in user (e.g. Postgres
  ``session_role`` ← ``membership.attr:pg_role``).

Revision ID: identmap01
Revises: mrgheads07
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op


revision = "identmap01"
down_revision = "mrgheads07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("memberships") as batch_op:
        batch_op.add_column(sa.Column("custom_attributes", sa.JSON(), nullable=True))
    with op.batch_alter_table("connections") as batch_op:
        batch_op.add_column(sa.Column("identity_mapping", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("connections") as batch_op:
        batch_op.drop_column("identity_mapping")
    with op.batch_alter_table("memberships") as batch_op:
        batch_op.drop_column("custom_attributes")
