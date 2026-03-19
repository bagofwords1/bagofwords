"""make audit_logs.organization_id nullable for pre-org events like login

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
Create Date: 2026-03-19 00:00:00.000000

Auth events (login, login_failed) happen before org selection, so
organization_id must be nullable. The FK and index remain unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'x9y0z1a2b3c4'
down_revision: Union[str, None] = 'w8x9y0z1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.alter_column(
            'organization_id',
            existing_type=sa.String(36),
            nullable=True,
        )


def downgrade() -> None:
    # Back-fill NULLs before restoring NOT NULL to avoid constraint violation
    op.execute("UPDATE audit_logs SET organization_id = 'UNKNOWN' WHERE organization_id IS NULL")
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.alter_column(
            'organization_id',
            existing_type=sa.String(36),
            nullable=False,
        )
