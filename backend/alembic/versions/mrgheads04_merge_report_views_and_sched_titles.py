"""merge report_views and scheduled-task-title heads

Revision ID: mrgheads04
Revises: rptview01, whproj01
Create Date: 2026-08-01 00:00:00.000000

No-op merge: the report-status branch (rptview01) and the scheduled-task
titles branch (sptitle01 → … → whproj01) both descend from 297905a87c8a.
"""
from typing import Sequence, Union


revision: str = 'mrgheads04'
down_revision: Union[str, Sequence[str], None] = ('rptview01', 'whproj01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
