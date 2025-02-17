"""users in completions and files

Revision ID: 6883ef8e3d7c
Revises: 42823eea6256
Create Date: 2024-08-18 15:12:40.450968

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column

# revision identifiers, used by Alembic.
revision: str = '6883ef8e3d7c'
down_revision: Union[str, None] = '42823eea6256'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # SQLite path using batch operations
        with op.batch_alter_table('completions') as batch_op:
            batch_op.add_column(sa.Column('user_id', get_uuid_column(), nullable=True))
            batch_op.drop_index('ix_completions_id')
            batch_op.create_index('ix_completions_id', ['id'], unique=True)
            batch_op.create_foreign_key(
                'fk_completions_user_id', 'users', ['user_id'], ['id']
            )

        with op.batch_alter_table('files') as batch_op:
            batch_op.add_column(sa.Column('user_id', get_uuid_column(), nullable=False))
            batch_op.create_index('ix_files_id', ['id'], unique=True)
            batch_op.create_foreign_key(
                'fk_files_user_id', 'users', ['user_id'], ['id']
            )
    else:
        # PostgreSQL path using direct operations
        op.add_column('completions', sa.Column('user_id', get_uuid_column(), nullable=True))
        op.drop_index('ix_completions_id', table_name='completions')
        op.create_index(op.f('ix_completions_id'), 'completions', ['id'], unique=True)
        op.create_foreign_key(
            'fk_completions_user_id', 'completions', 'users', ['user_id'], ['id']
        )
        
        op.add_column('files', sa.Column('user_id', get_uuid_column(), nullable=False))
        op.create_index(op.f('ix_files_id'), 'files', ['id'], unique=True)
        op.create_foreign_key(
            'fk_files_user_id', 'files', 'users', ['user_id'], ['id']
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == 'sqlite':
        # SQLite path
        with op.batch_alter_table('files') as batch_op:
            batch_op.drop_constraint('fk_files_user_id', type_='foreignkey')
            batch_op.drop_index('ix_files_id')
            batch_op.drop_column('user_id')

        with op.batch_alter_table('completions') as batch_op:
            batch_op.drop_constraint('fk_completions_user_id', type_='foreignkey')
            batch_op.drop_index('ix_completions_id')
            batch_op.create_index('ix_completions_id', ['id'], unique=False)
            batch_op.drop_column('user_id')
    else:
        # PostgreSQL path
        op.drop_constraint('fk_files_user_id', 'files', type_='foreignkey')
        op.drop_index(op.f('ix_files_id'), table_name='files')
        op.drop_column('files', 'user_id')
        op.drop_constraint('fk_completions_user_id', 'completions', type_='foreignkey')
        op.drop_index(op.f('ix_completions_id'), table_name='completions')
        op.create_index('ix_completions_id', 'completions', ['id'], unique=False)
        op.drop_column('completions', 'user_id')
    # ### end Alembic commands ###
