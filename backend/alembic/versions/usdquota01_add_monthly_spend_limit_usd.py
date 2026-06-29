"""add monthly spend limit (USD) to usage policies

Revision ID: usdquota01
Revises: la1a2b3c4d5e
Create Date: 2026-06-29 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "usdquota01"
down_revision: Union[str, None] = "la1a2b3c4d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_policies",
        sa.Column("monthly_spend_limit_usd", sa.Numeric(18, 6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usage_policies", "monthly_spend_limit_usd")
