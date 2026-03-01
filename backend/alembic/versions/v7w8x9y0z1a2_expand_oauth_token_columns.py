"""expand oauth token columns for large Entra ID JWT tokens

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-03-01 00:00:00.000000

Entra ID (Azure AD) OIDC JWT tokens can exceed the previous String(1024)
limit. This migration widens access_token and refresh_token to Text.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v7w8x9y0z1a2'
down_revision: Union[str, None] = 'u6v7w8x9y0z1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('oauth_accounts', schema=None) as batch_op:
        batch_op.alter_column(
            'access_token',
            existing_type=sa.String(length=1024),
            type_=sa.Text(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            'refresh_token',
            existing_type=sa.String(length=1024),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('oauth_accounts', schema=None) as batch_op:
        batch_op.alter_column(
            'refresh_token',
            existing_type=sa.Text(),
            type_=sa.String(length=1024),
            existing_nullable=True,
        )
        batch_op.alter_column(
            'access_token',
            existing_type=sa.Text(),
            type_=sa.String(length=1024),
            existing_nullable=False,
        )
