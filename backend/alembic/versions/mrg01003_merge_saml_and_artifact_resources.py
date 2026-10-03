"""merge saml identities and artifact resources heads

Both branched off umbr0928 (saml1001 on main, artres01 on the artifact
resources line). No schema change: this only collapses the tree back to a
single head so `alembic upgrade head` works.

Revision ID: mrg01003
Revises: saml1001, artres01
Create Date: 2026-10-03
"""


revision = "mrg01003"
down_revision = ("saml1001", "artres01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
