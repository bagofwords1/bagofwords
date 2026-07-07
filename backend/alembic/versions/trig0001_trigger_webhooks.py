"""trigger webhooks: spawn-mode run spec + report provenance

Revision ID: trig0001
Revises: rbacbf01
Create Date: 2026-07-07 10:00:00.000000

Evolves webhooks into user-owned triggers (docs/design/agent-triggers.md):
- webhooks.report_id becomes nullable (NULL = standalone trigger, spawn mode)
- webhooks gains a run spec: task_template, mode, model_id
- webhook_data_source_association M2M (trigger's agents)
- reports.webhook_id provenance stamp (plain string, no FK — avoids a
  circular FK with webhooks.report_id) for the trigger origin indicator
  and per-trigger run history
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'trig0001'
down_revision: Union[str, None] = 'rbacbf01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('webhooks') as batch_op:
        batch_op.alter_column('report_id', existing_type=sa.String(length=36), nullable=True)
        batch_op.add_column(sa.Column('task_template', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('mode', sa.String(), nullable=False, server_default='chat'))
        batch_op.add_column(sa.Column('model_id', sa.String(length=36), nullable=True))

    op.create_table(
        'webhook_data_source_association',
        sa.Column('webhook_id', sa.String(length=36), nullable=True),
        sa.Column('data_source_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['webhook_id'], ['webhooks.id'], ),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ),
    )

    with op.batch_alter_table('reports') as batch_op:
        batch_op.add_column(sa.Column('webhook_id', sa.String(length=36), nullable=True))
        batch_op.create_index(op.f('ix_reports_webhook_id'), ['webhook_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('reports') as batch_op:
        batch_op.drop_index(op.f('ix_reports_webhook_id'))
        batch_op.drop_column('webhook_id')

    op.drop_table('webhook_data_source_association')

    with op.batch_alter_table('webhooks') as batch_op:
        batch_op.drop_column('model_id')
        batch_op.drop_column('mode')
        batch_op.drop_column('task_template')
        batch_op.alter_column('report_id', existing_type=sa.String(length=36), nullable=False)
