"""merge saml1001 and artres01 heads

SAML identities (saml1001) and artifact resources (artres01) both branched
from umbr0928. This no-op revision joins them so the tree has a single head.

Revision ID: mrg1003
Revises: saml1001, artres01
Create Date: 2026-10-03
"""


revision = "mrg1003"
down_revision = ("saml1001", "artres01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
