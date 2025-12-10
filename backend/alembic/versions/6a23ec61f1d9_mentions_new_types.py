"""mentions-new-types

Revision ID: 6a23ec61f1d9
Revises: e7769f2fca82
Create Date: 2025-12-09 23:03:28.370177

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a23ec61f1d9'
down_revision: Union[str, None] = 'e7769f2fca82'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add TABLE and ENTITY values to the mentiontype enum for PostgreSQL
    # SQLite doesn't support enum types, so skip this migration for SQLite
    dialect = op.get_bind().dialect.name
    if dialect == 'postgresql':
        op.execute("ALTER TYPE mentiontype ADD VALUE IF NOT EXISTS 'TABLE'")
        op.execute("ALTER TYPE mentiontype ADD VALUE IF NOT EXISTS 'ENTITY'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from an enum type.
    # To fully reverse this, you would need to:
    # 1. Create a new enum without TABLE/ENTITY
    # 2. Migrate the column to use the new enum
    # 3. Drop the old enum
    # This is generally not worth the complexity for a downgrade.
    pass
