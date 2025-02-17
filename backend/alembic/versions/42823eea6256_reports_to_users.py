from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column

# revision identifiers, used by Alembic.
revision: str = '42823eea6256'
down_revision: Union[str, None] = '4ad4807cbb69'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # SQLite path using batch operations
        with op.batch_alter_table('reports') as batch_op:
            batch_op.add_column(sa.Column('user_id', get_uuid_column(), nullable=False))
            batch_op.create_foreign_key(
                'fk_reports_user_id', 'users', ['user_id'], ['id'], ondelete='SET NULL'
            )
            batch_op.create_index('ix_reports_user_id', ['user_id'])
    else:
        # PostgreSQL path using direct operations
        op.add_column('reports', sa.Column('user_id', get_uuid_column(), nullable=False))
        op.create_foreign_key(
            'fk_reports_user_id', 'reports', 'users', ['user_id'], ['id'], ondelete='SET NULL'
        )
        op.create_index('ix_reports_user_id', 'reports', ['user_id'])


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # SQLite path
        with op.batch_alter_table('reports') as batch_op:
            batch_op.drop_index('ix_reports_user_id')
            batch_op.drop_constraint('fk_reports_user_id', type_='foreignkey')
            batch_op.drop_column('user_id')
    else:
        # PostgreSQL path
        op.drop_constraint('fk_reports_user_id', 'reports', type_='foreignkey')
        op.drop_index('ix_reports_user_id', table_name='reports')
        op.drop_column('reports', 'user_id')