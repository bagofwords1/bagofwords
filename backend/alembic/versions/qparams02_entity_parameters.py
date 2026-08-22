"""entity parameters + per-viewer entity results

Revision ID: qparams02
Revises: qparams01
Create Date: 2026-08-22 00:00:00.000000

Identity-scoped entities, mirroring steps:
- entities.parameters / applied_params: declared ParamSpec list carried over
  from the source query on promotion, and the values the shared snapshot ran
  with (defaults + owner identity).
- entity_user_results: one cached slice per (entity, viewer, values),
  executed with the viewer's identity binding; stale once the shared
  snapshot refreshes past it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'qparams02'
down_revision: Union[str, None] = 'qparams01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parameters', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('applied_params', sa.JSON(), nullable=True))

    op.create_table(
        'entity_user_results',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('entity_id', sa.String(length=36), sa.ForeignKey('entities.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('organization_id', sa.String(length=36), sa.ForeignKey('organizations.id'), nullable=False, index=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='success'),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('params_fingerprint', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('applied_params', sa.JSON(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('entity_id', 'user_id', 'params_fingerprint', name='uq_entity_user_results_entity_user_params'),
    )


def downgrade() -> None:
    op.drop_table('entity_user_results')
    with op.batch_alter_table('entities', schema=None) as batch_op:
        batch_op.drop_column('applied_params')
        batch_op.drop_column('parameters')
