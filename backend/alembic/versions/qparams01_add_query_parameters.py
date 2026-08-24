"""add query parameters, step applied_params, per-params viewer results

Revision ID: qparams01
Revises: shrdata01
Create Date: 2026-08-22 00:00:00.000000

Parameterized queries:
- queries.parameters: declared ParamSpec list (the Query is the stable
  identity; declarations live here).
- steps.applied_params: the concrete values the shared snapshot ran with.
- step_user_results.params_fingerprint + applied_params: one cached result
  per (step, user, parameter combination). The old (step_id, user_id) unique
  key becomes (step_id, user_id, params_fingerprint); existing rows get
  fingerprint '' (the "defaults/no params" slot), preserving behavior.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'qparams01'
down_revision: Union[str, None] = 'shrdata01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('queries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parameters', sa.JSON(), nullable=True))

    with op.batch_alter_table('steps', schema=None) as batch_op:
        batch_op.add_column(sa.Column('applied_params', sa.JSON(), nullable=True))

    with op.batch_alter_table('step_user_results', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('params_fingerprint', sa.String(length=64), nullable=False, server_default='')
        )
        batch_op.add_column(sa.Column('applied_params', sa.JSON(), nullable=True))
        try:
            batch_op.drop_constraint('uq_step_user_results_step_user', type_='unique')
        except Exception:
            pass
        batch_op.create_unique_constraint(
            'uq_step_user_results_step_user_params',
            ['step_id', 'user_id', 'params_fingerprint'],
        )


def downgrade() -> None:
    with op.batch_alter_table('step_user_results', schema=None) as batch_op:
        try:
            batch_op.drop_constraint('uq_step_user_results_step_user_params', type_='unique')
        except Exception:
            pass
        batch_op.create_unique_constraint(
            'uq_step_user_results_step_user', ['step_id', 'user_id']
        )
        batch_op.drop_column('applied_params')
        batch_op.drop_column('params_fingerprint')

    with op.batch_alter_table('steps', schema=None) as batch_op:
        batch_op.drop_column('applied_params')

    with op.batch_alter_table('queries', schema=None) as batch_op:
        batch_op.drop_column('parameters')
