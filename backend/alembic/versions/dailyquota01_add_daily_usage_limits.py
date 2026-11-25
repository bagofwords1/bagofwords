"""add daily usage limits to usage policies

Revision ID: dailyquota01
Revises: colstats01
Create Date: 2026-11-23 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dailyquota01"
down_revision: Union[str, None] = "colstats01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("usage_policies") as batch_op:
        batch_op.add_column(sa.Column("daily_token_limit", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("daily_query_limit", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("daily_data_bytes_limit", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("daily_spend_limit_usd", sa.Numeric(18, 6), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_policies") as batch_op:
        batch_op.drop_column("daily_spend_limit_usd")
        batch_op.drop_column("daily_data_bytes_limit")
        batch_op.drop_column("daily_query_limit")
        batch_op.drop_column("daily_token_limit")
