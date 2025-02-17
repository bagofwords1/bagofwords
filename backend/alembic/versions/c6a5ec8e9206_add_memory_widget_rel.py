"""add memory-widget rel

Revision ID: c6a5ec8e9206
Revises: c6cef0b221cb
Create Date: 2024-10-26 13:14:50.593510

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column


# revision identifiers, used by Alembic.
revision: str = 'c6a5ec8e9206'
down_revision: Union[str, None] = 'c6cef0b221cb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # SQLite path using batch operations
        with op.batch_alter_table('memories') as batch_op:
            batch_op.add_column(sa.Column('widget_id', get_uuid_column(), nullable=True))
            batch_op.create_foreign_key(
                'fk_memories_widget_id', 'widgets', ['widget_id'], ['id']
            )
    else:
        # PostgreSQL path using direct operations
        op.add_column('memories', sa.Column('widget_id', get_uuid_column(), nullable=True))
        op.create_foreign_key(
            'fk_memories_widget_id', 'memories', 'widgets', ['widget_id'], ['id']
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # SQLite path
        with op.batch_alter_table('memories') as batch_op:
            batch_op.drop_constraint('fk_memories_widget_id', type_='foreignkey')
            batch_op.drop_column('widget_id')
    else:
        # PostgreSQL path
        op.drop_constraint('fk_memories_widget_id', 'memories', type_='foreignkey')
        op.drop_column('memories', 'widget_id')
