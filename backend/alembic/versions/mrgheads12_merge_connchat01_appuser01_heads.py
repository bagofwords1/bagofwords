"""merge connchat01 and appuser01 heads

Both revisions branch from dailyquota01 (#1901 and #1914, shipped together in
release 0.0.636), which left two heads and would break every
`alembic upgrade head`. This no-op revision joins them so the tree has a
single head again.

Revision ID: mrgheads12
Revises: connchat01, appuser01
Create Date: 2026-11-24
"""


revision = "mrgheads12"
down_revision = ("connchat01", "appuser01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
