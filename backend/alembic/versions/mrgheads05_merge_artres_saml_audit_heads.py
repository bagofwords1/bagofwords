"""merge artssomrg01, auditstrm01 and mrg1003 heads

All three revisions branch from (artres01, saml1001), which left three heads
after main was merged into codex/artifact-resources. This no-op revision joins
them so the tree has a single head again.

Revision ID: mrgheads05
Revises: artssomrg01, auditstrm01, mrg1003
Create Date: 2026-10-03
"""


revision = "mrgheads05"
down_revision = ("artssomrg01", "auditstrm01", "mrg1003")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
