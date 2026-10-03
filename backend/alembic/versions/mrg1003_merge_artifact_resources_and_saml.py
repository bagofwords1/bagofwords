"""merge artifact-resources and saml heads

Revision ID: mrg1003
Revises: artres01, saml1001
Create Date: 2026-10-03 00:00:00.000000

No-op merge: the artifact resources branch (artres01) and the SAML branch
(saml1001) were developed in parallel and each added a head.
"""
from typing import Sequence, Union


revision: str = 'mrg1003'
down_revision: Union[str, Sequence[str], None] = ('artres01', 'saml1001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
