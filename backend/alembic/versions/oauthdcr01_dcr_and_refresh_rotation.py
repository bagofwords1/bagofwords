"""OAuth server: dynamic client registration + refresh-token families

Revision ID: oauthdcr01
Revises: umbr0928
Create Date: 2026-10-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "oauthdcr01"
down_revision: str | None = "umbr0928"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("oauth_mcp_clients", schema=None) as batch_op:
        batch_op.alter_column("organization_id", existing_type=sa.String(36), nullable=True)
        batch_op.alter_column("client_secret_hash", existing_type=sa.String(64), nullable=True)
        batch_op.add_column(
            sa.Column("registration_type", sa.String(16), nullable=False, server_default="static")
        )
        batch_op.add_column(sa.Column("token_endpoint_auth_method", sa.String(32), nullable=True))

    with op.batch_alter_table("oauth_mcp_access_tokens", schema=None) as batch_op:
        batch_op.add_column(sa.Column("family_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("session_started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("rotated_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_oauth_mcp_access_tokens_family_id", ["family_id"])


def downgrade() -> None:
    with op.batch_alter_table("oauth_mcp_access_tokens", schema=None) as batch_op:
        batch_op.drop_index("ix_oauth_mcp_access_tokens_family_id")
        batch_op.drop_column("rotated_at")
        batch_op.drop_column("session_started_at")
        batch_op.drop_column("family_id")

    # Self-registered clients have no org and may have no secret; they cannot
    # be represented once those columns are NOT NULL again.
    op.execute("DELETE FROM oauth_mcp_clients WHERE registration_type = 'dynamic'")
    with op.batch_alter_table("oauth_mcp_clients", schema=None) as batch_op:
        batch_op.drop_column("token_endpoint_auth_method")
        batch_op.drop_column("registration_type")
        batch_op.alter_column("client_secret_hash", existing_type=sa.String(64), nullable=False)
        batch_op.alter_column("organization_id", existing_type=sa.String(36), nullable=False)
