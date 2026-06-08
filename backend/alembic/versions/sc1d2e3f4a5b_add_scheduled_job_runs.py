"""add scheduled_job_runs table for scheduled-run claim dedup

Revision ID: sc1d2e3f4a5b
Revises: wh1a2b3c4d5e
Create Date: 2026-06-08 13:30:00.000000

Makes scheduled job execution idempotent across uvicorn workers and replicas.
Every worker runs its own AsyncIOScheduler against the shared APScheduler job
store, so a single scheduled fire would otherwise execute (and email) once per
worker. Each fire now atomically claims a row keyed on (job_id, run_bucket);
the unique constraint lets exactly one worker win. See
`app.core.scheduler.claim_scheduled_run`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'sc1d2e3f4a5b'
down_revision: Union[str, None] = 'wh1a2b3c4d5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scheduled_job_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.String(length=191), nullable=False),
        sa.Column('run_bucket', sa.BigInteger(), nullable=False),
        sa.Column('claimed_by', sa.String(length=255), nullable=True),
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'run_bucket', name='uq_scheduled_job_runs_job_bucket'),
    )
    op.create_index(
        'ix_scheduled_job_runs_job_bucket',
        'scheduled_job_runs',
        ['job_id', 'run_bucket'],
    )


def downgrade() -> None:
    op.drop_index('ix_scheduled_job_runs_job_bucket', table_name='scheduled_job_runs')
    op.drop_table('scheduled_job_runs')
