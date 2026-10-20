"""add run_once_at to scheduled_prompts

Revision ID: onceat01
Revises: mrgheads09
Create Date: 2026-10-14 00:00:00.000000

A "One-time" scheduled task used to be stored as a cron pinned to one
minute/hour/day/month ("30 16 14 10 *"), which is a yearly schedule: it fired
again on the same date every year. One-time tasks now store the instant they
run at in run_once_at and fire once.

Legacy rows are recognised by shape: the recurring builder in the UI always
writes "*" in the month field, so a pinned minute/hour/day/month with a "*"
weekday is the One-time shape. Of those:
- the ones that already ran are deactivated, so they do not fire next year;
- the ones still waiting are converted: the scheduler's stored next fire time
  is the absolute instant the task was meant to run at (the cron itself
  cannot be converted, the browser timezone that produced it is unknown).
  Their old cron job is dropped; startup registers the one-time job.
Waiting rows with no scheduler job left are not touched.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'onceat01'
down_revision: Union[str, None] = 'mrgheads09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_legacy_one_time(cron: str) -> bool:
    parts = (cron or "").split()
    if len(parts) != 5:
        return False
    minute, hour, day, month, dow = parts
    return all(p.isdigit() for p in (minute, hour, day, month)) and dow == "*"


def _job_id(sp_id: str) -> str:
    return f"scheduled_prompt_{sp_id}"


def upgrade() -> None:
    op.add_column('scheduled_prompts', sa.Column('run_once_at', sa.DateTime(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, cron_schedule, last_run_at FROM scheduled_prompts "
        "WHERE is_active = :active AND deleted_at IS NULL"
    ), {"active": True}).fetchall()
    legacy = [r for r in rows if _is_legacy_one_time(r[1])]
    if not legacy:
        return
    ran = [str(r[0]) for r in legacy if r[2] is not None]
    waiting = [str(r[0]) for r in legacy if r[2] is None]

    prompts = sa.table(
        'scheduled_prompts', sa.column('id', sa.String), sa.column('is_active', sa.Boolean),
        sa.column('cron_schedule', sa.String), sa.column('run_once_at', sa.DateTime),
    )
    if ran:
        bind.execute(prompts.update().where(prompts.c.id.in_(ran)).values(is_active=False))

    # APScheduler creates its job table on first start, so it may not exist yet.
    if not sa.inspect(bind).has_table('apscheduler_jobs'):
        return
    jobs = sa.table('apscheduler_jobs', sa.column('id', sa.String), sa.column('next_run_time', sa.Float))

    # next_run_time is a UTC epoch; NULL means the job is paused.
    next_runs = dict(bind.execute(
        sa.select(jobs.c.id, jobs.c.next_run_time)
        .where(jobs.c.id.in_([_job_id(i) for i in waiting]), jobs.c.next_run_time.isnot(None))
    ).all()) if waiting else {}
    converted = []
    for sp_id in waiting:
        ts = next_runs.get(_job_id(sp_id))
        if ts is None:
            continue
        run_once_at = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        bind.execute(prompts.update().where(prompts.c.id == sp_id)
                     .values(cron_schedule="@once", run_once_at=run_once_at))
        converted.append(sp_id)

    dropped = ran + converted
    if dropped:
        bind.execute(jobs.delete().where(jobs.c.id.in_([_job_id(i) for i in dropped])))


def downgrade() -> None:
    op.drop_column('scheduled_prompts', 'run_once_at')
