"""add reports.include_data_tab

Revision ID: shrdata01
Revises: evalsuitefk01
Create Date: 2026-08-20 00:00:00.000000

The share dialog's "Include Data Tab" checkbox: whether viewers of a shared
artifact (/r/{id}) see the Data tab listing the queries behind the report.
Artifact sharing only — the conversation share has no such tab.

Defaults to true so every report that already exists keeps the tab it shows
today; turning it off is an explicit choice by the report owner.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'shrdata01'
down_revision: Union[str, None] = 'evalsuitefk01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('include_data_tab', sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_column('include_data_tab')
