"""merge entraexchange01 and runcache01 heads

Both revisions branch from emailclaim01, which left two heads on main and
broke every `alembic upgrade head` (including the test conftest). This no-op
revision joins them so the tree has a single head again.

Revision ID: mrgheads06
Revises: entraexchange01, runcache01
Create Date: 2026-10-06
"""


revision = "mrgheads06"
down_revision = ("entraexchange01", "runcache01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
