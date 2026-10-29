"""Optional external Entra exchange and verified identity bindings."""

import sqlalchemy as sa

from alembic import op

revision = "entraexchange01"
down_revision = "emailclaim01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("oauth_mcp_clients", sa.Column("entra_exchange", sa.JSON(), nullable=True))
    op.add_column("oauth_mcp_access_tokens", sa.Column("exchange_context", sa.JSON(), nullable=True))
    op.add_column("oauth_accounts", sa.Column("entra_identity", sa.JSON(), nullable=True))
    op.add_column("oauth_accounts", sa.Column("entra_subject", sa.String(110), nullable=True))
    op.create_index("ix_oauth_accounts_entra_subject", "oauth_accounts", ["entra_subject"], unique=True)


def downgrade():
    op.drop_index("ix_oauth_accounts_entra_subject", table_name="oauth_accounts")
    op.drop_column("oauth_accounts", "entra_subject")
    op.drop_column("oauth_accounts", "entra_identity")
    op.drop_column("oauth_mcp_access_tokens", "exchange_context")
    op.drop_column("oauth_mcp_clients", "entra_exchange")
