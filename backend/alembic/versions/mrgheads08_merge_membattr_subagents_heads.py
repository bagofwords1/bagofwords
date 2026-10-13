"""merge membattr01 and subagents01 heads

Both revisions branch from the same parent, which left two heads once the
member-attributes and sub-agents changes landed together and broke every
`alembic upgrade head`. This no-op revision joins them so the tree has a
single head again.

Revision ID: mrgheads08
Revises: membattr01, subagents01
Create Date: 2026-10-13
"""


revision = "mrgheads08"
down_revision = ("membattr01", "subagents01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
