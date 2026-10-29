"""merge toolconfresp01 and dstexclcols01 heads

Both revisions branch from mrgheads06, which left two heads on main and
broke every `alembic upgrade head` (including the test conftest). This no-op
revision joins them so the tree has a single head again.

Revision ID: mrgheads07
Revises: toolconfresp01, dstexclcols01
Create Date: 2026-10-08
"""


revision = "mrgheads07"
down_revision = ("toolconfresp01", "dstexclcols01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
