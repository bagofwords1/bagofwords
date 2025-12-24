"""ae-build-id

Revision ID: c07deb3aeac8
Revises: g2h3i4j5k6l7
Create Date: 2025-12-24 15:58:49.850840

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'c07deb3aeac8'
down_revision: Union[str, None] = 'g2h3i4j5k6l7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agent_executions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('build_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key('fk_agent_execution_build_id', 'instruction_builds', ['build_id'], ['id'])

    # ### end Alembic commands ###


def downgrade() -> None:
    with op.batch_alter_table('agent_executions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_agent_execution_build_id', type_='foreignkey')
        batch_op.drop_column('build_id')