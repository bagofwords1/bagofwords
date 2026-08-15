"""add OAuth app client trust

Revision ID: oauthapp01
Revises: rptperf002
Create Date: 2026-08-15 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "oauthapp01"
down_revision: str | None = "rptperf002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "oauth_mcp_clients",
        sa.Column("trusted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("oauth_mcp_clients", "trusted")
