"""scheduled prompts: report-per-run routing + report provenance

Revision ID: trig0002
Revises: trig0001
Create Date: 2026-07-07 12:00:00.000000

Adds routing to scheduled prompts (docs/design/agent-triggers.md §6.3):
- scheduled_prompts.spawn_new_report — False = run in the host report
  (default, keeps cross-run memory), True = spawn a fresh report per run
- reports.scheduled_prompt_id — provenance stamp for spawned runs (plain
  string like reports.webhook_id; powers the 🕐 origin indicator)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'trig0002'
down_revision: Union[str, None] = 'trig0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('scheduled_prompts') as batch_op:
        batch_op.add_column(sa.Column('spawn_new_report', sa.Boolean(), nullable=False, server_default=sa.false()))

    with op.batch_alter_table('reports') as batch_op:
        batch_op.add_column(sa.Column('scheduled_prompt_id', sa.String(length=36), nullable=True))
        batch_op.create_index(op.f('ix_reports_scheduled_prompt_id'), ['scheduled_prompt_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('reports') as batch_op:
        batch_op.drop_index(op.f('ix_reports_scheduled_prompt_id'))
        batch_op.drop_column('scheduled_prompt_id')

    with op.batch_alter_table('scheduled_prompts') as batch_op:
        batch_op.drop_column('spawn_new_report')
