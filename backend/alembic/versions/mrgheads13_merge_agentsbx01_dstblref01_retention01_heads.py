"""merge agentsbx01, dstblref01 and retention01 heads

All three revisions branch from mrgheads12 (#1941, #1940 and #1943, shipped
together in release 0.0.639), which left three heads and would break every
`alembic upgrade head`. This no-op revision joins them so the tree has a
single head again.

Revision ID: mrgheads13
Revises: agentsbx01, dstblref01, retention01
Create Date: 2026-11-27
"""


revision = "mrgheads13"
down_revision = ("agentsbx01", "dstblref01", "retention01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
