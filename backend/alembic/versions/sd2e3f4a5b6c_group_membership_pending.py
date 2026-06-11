"""Allow pending (unregistered) memberships in group_memberships

Adds ``membership_id`` to ``group_memberships`` and makes ``user_id``
nullable so an org admin can pre-assign an invited-but-not-yet-registered
member to a group. When the invitee registers, the pending row is
materialized into a user-keyed row.

Revision ID: sd2e3f4a5b6c
Revises: sc1d2e3f4a5b
Create Date: 2026-06-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "sd2e3f4a5b6c"
down_revision: Union[str, None] = "sc1d2e3f4a5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("group_memberships", schema=None) as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.String(length=36), nullable=True)
        batch_op.add_column(sa.Column("membership_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_group_memberships_membership_id",
            "memberships",
            ["membership_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_group_membership_pending", ["group_id", "membership_id"]
        )


def downgrade() -> None:
    # Pending (user_id IS NULL) rows cannot exist under the old schema where
    # user_id is NOT NULL — drop them before tightening the column back.
    op.execute("DELETE FROM group_memberships WHERE user_id IS NULL")
    with op.batch_alter_table("group_memberships", schema=None) as batch_op:
        batch_op.drop_constraint("uq_group_membership_pending", type_="unique")
        batch_op.drop_constraint("fk_group_memberships_membership_id", type_="foreignkey")
        batch_op.drop_column("membership_id")
        batch_op.alter_column("user_id", existing_type=sa.String(length=36), nullable=False)
