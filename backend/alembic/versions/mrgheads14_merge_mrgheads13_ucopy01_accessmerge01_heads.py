"""merge mrgheads13, ucopy01 and accessmerge01 heads

ucopy01 (#1947) and fabrls01 (#1948, joined by accessmerge01) branch from
mrgheads12 alongside the revisions mrgheads13 joined, all shipped together in
release 0.0.639. This no-op revision joins the three heads so the tree has a
single head again.

Revision ID: mrgheads14
Revises: mrgheads13, ucopy01, accessmerge01
Create Date: 2026-11-28
"""


revision = "mrgheads14"
down_revision = ("mrgheads13", "ucopy01", "accessmerge01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
