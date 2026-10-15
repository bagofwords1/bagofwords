"""merge rptapiset01 and onceat01 heads

rptapiset01 (report API settings, #1378) and onceat01 (one-time scheduled
tasks, #1386) both branch from mrgheads09, so releasing them together leaves
two heads. This no-op revision joins them so the tree has a single head again.

Revision ID: mrgheads10
Revises: rptapiset01, onceat01
Create Date: 2026-10-15
"""


revision = "mrgheads10"
down_revision = ("rptapiset01", "onceat01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
