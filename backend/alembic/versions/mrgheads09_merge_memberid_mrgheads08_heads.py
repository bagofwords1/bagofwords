"""merge memberid01 and mrgheads08 heads

memberid01 (Qlik per-member identity) branches from membattr01, which
mrgheads08 already joined with subagents01, so the release left two heads
and broke every `alembic upgrade head`. This no-op revision joins them so
the tree has a single head again.

Revision ID: mrgheads09
Revises: memberid01, mrgheads08
Create Date: 2026-10-13
"""


revision = "mrgheads09"
down_revision = ("memberid01", "mrgheads08")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
