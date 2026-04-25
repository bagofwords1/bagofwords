"""eval-as-tools: status + provenance on test_cases, is_eval_run on agent_executions

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-04-25 00:00:00.000000

Backs the eval-as-tools feature:

- ``test_cases.status`` (draft|active|archived): drafts are excluded from
  default suite runs and promoted to ``active`` by a manage-evals user.
- ``test_cases.auto_generated``: badge for cases drafted by the
  knowledge-harness ``create_eval`` path.
- ``test_cases.source_completion_id`` / ``source_agent_execution_id`` /
  ``source_feedback_id``: provenance back to the conversation that
  produced the case.
- ``agent_executions.is_eval_run``: True for executions spawned by
  ``TestRunService`` to evaluate a test case. Used by the ``run_eval``
  tool to refuse nested invocations.

Existing rows are backfilled with ``status='active'`` /
``auto_generated=False`` / ``is_eval_run=False`` so they keep running
unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'

    # === test_cases ===
    # Use server_default for boolean/string backfill — portable across
    # SQLite (stores 0/1) and Postgres (BOOLEAN).
    op.add_column(
        'test_cases',
        sa.Column('status', sa.String(), nullable=True, server_default=sa.text("'active'")),
    )
    op.add_column(
        'test_cases',
        sa.Column('auto_generated', sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column('test_cases', sa.Column('source_completion_id', sa.String(length=36), nullable=True))
    op.add_column('test_cases', sa.Column('source_agent_execution_id', sa.String(length=36), nullable=True))
    op.add_column('test_cases', sa.Column('source_feedback_id', sa.String(length=36), nullable=True))

    # === agent_executions ===
    op.add_column(
        'agent_executions',
        sa.Column('is_eval_run', sa.Boolean(), nullable=True, server_default=sa.false()),
    )

    if is_sqlite:
        # SQLite can't ALTER nullability or add FKs in-place.
        with op.batch_alter_table('test_cases') as batch_op:
            batch_op.alter_column('status', existing_type=sa.String(), nullable=False)
            batch_op.alter_column('auto_generated', existing_type=sa.Boolean(), nullable=False)
            batch_op.create_index('ix_test_cases_status', ['status'], unique=False)
            batch_op.create_index('ix_test_cases_source_completion_id', ['source_completion_id'], unique=False)
            batch_op.create_index('ix_test_cases_source_agent_execution_id', ['source_agent_execution_id'], unique=False)
            batch_op.create_index('ix_test_cases_source_feedback_id', ['source_feedback_id'], unique=False)
            batch_op.create_foreign_key(
                'fk_test_cases_source_completion_id_completions',
                'completions', ['source_completion_id'], ['id'],
            )
            batch_op.create_foreign_key(
                'fk_test_cases_source_agent_execution_id_agent_executions',
                'agent_executions', ['source_agent_execution_id'], ['id'],
            )
            batch_op.create_foreign_key(
                'fk_test_cases_source_feedback_id_completion_feedbacks',
                'completion_feedbacks', ['source_feedback_id'], ['id'],
            )

        with op.batch_alter_table('agent_executions') as batch_op:
            batch_op.alter_column('is_eval_run', existing_type=sa.Boolean(), nullable=False)
            batch_op.create_index('ix_agent_executions_is_eval_run', ['is_eval_run'], unique=False)
    else:
        op.alter_column('test_cases', 'status', existing_type=sa.String(), nullable=False)
        op.alter_column('test_cases', 'auto_generated', existing_type=sa.Boolean(), nullable=False)
        op.create_index('ix_test_cases_status', 'test_cases', ['status'], unique=False)
        op.create_index('ix_test_cases_source_completion_id', 'test_cases', ['source_completion_id'], unique=False)
        op.create_index('ix_test_cases_source_agent_execution_id', 'test_cases', ['source_agent_execution_id'], unique=False)
        op.create_index('ix_test_cases_source_feedback_id', 'test_cases', ['source_feedback_id'], unique=False)
        op.create_foreign_key(
            'fk_test_cases_source_completion_id_completions',
            'test_cases', 'completions', ['source_completion_id'], ['id'],
        )
        op.create_foreign_key(
            'fk_test_cases_source_agent_execution_id_agent_executions',
            'test_cases', 'agent_executions', ['source_agent_execution_id'], ['id'],
        )
        op.create_foreign_key(
            'fk_test_cases_source_feedback_id_completion_feedbacks',
            'test_cases', 'completion_feedbacks', ['source_feedback_id'], ['id'],
        )
        op.alter_column('agent_executions', 'is_eval_run', existing_type=sa.Boolean(), nullable=False)
        op.create_index('ix_agent_executions_is_eval_run', 'agent_executions', ['is_eval_run'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'

    fks_test_cases = (
        'fk_test_cases_source_completion_id_completions',
        'fk_test_cases_source_agent_execution_id_agent_executions',
        'fk_test_cases_source_feedback_id_completion_feedbacks',
    )
    indices_test_cases = (
        'ix_test_cases_status',
        'ix_test_cases_source_completion_id',
        'ix_test_cases_source_agent_execution_id',
        'ix_test_cases_source_feedback_id',
    )

    if is_sqlite:
        with op.batch_alter_table('test_cases') as batch_op:
            for fk in fks_test_cases:
                try:
                    batch_op.drop_constraint(fk, type_='foreignkey')
                except Exception:
                    pass
            for ix in indices_test_cases:
                try:
                    batch_op.drop_index(ix)
                except Exception:
                    pass
            batch_op.drop_column('source_feedback_id')
            batch_op.drop_column('source_agent_execution_id')
            batch_op.drop_column('source_completion_id')
            batch_op.drop_column('auto_generated')
            batch_op.drop_column('status')
        with op.batch_alter_table('agent_executions') as batch_op:
            try:
                batch_op.drop_index('ix_agent_executions_is_eval_run')
            except Exception:
                pass
            batch_op.drop_column('is_eval_run')
    else:
        for fk in fks_test_cases:
            try:
                op.drop_constraint(fk, 'test_cases', type_='foreignkey')
            except Exception:
                pass
        for ix in indices_test_cases:
            try:
                op.drop_index(ix, table_name='test_cases')
            except Exception:
                pass
        op.drop_column('test_cases', 'source_feedback_id')
        op.drop_column('test_cases', 'source_agent_execution_id')
        op.drop_column('test_cases', 'source_completion_id')
        op.drop_column('test_cases', 'auto_generated')
        op.drop_column('test_cases', 'status')
        try:
            op.drop_index('ix_agent_executions_is_eval_run', table_name='agent_executions')
        except Exception:
            pass
        op.drop_column('agent_executions', 'is_eval_run')
