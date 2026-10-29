"""onceat01 — legacy one-time tasks stop recurring.

Before run_once_at existed, the UI stored a one-time task as a cron pinned to
one minute/hour/day/month, which fires yearly. The migration deactivates the
ones that already ran, converts the ones still waiting to run_once_at from the
scheduler's stored next fire time (dropping their cron job either way), and
leaves every other schedule alone — including recurring monthly ones that
share all fields but the month.

Run:
    cd backend
    TESTING=true uv run pytest tests/unit/test_onceat01_migration.py
"""
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _load_migration():
    path = (Path(__file__).resolve().parents[2]
            / "alembic" / "versions" / "onceat01_scheduled_prompt_run_once_at.py")
    spec = importlib.util.spec_from_file_location("onceat01", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(metadata, with_jobs=True):
    prompts = sa.Table(
        "scheduled_prompts", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cron_schedule", sa.String, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("last_run_at", sa.DateTime, nullable=True),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
    )
    jobs = None
    if with_jobs:
        jobs = sa.Table(
            "apscheduler_jobs", metadata,
            sa.Column("id", sa.String(191), primary_key=True),
            sa.Column("next_run_time", sa.Float),
            sa.Column("job_state", sa.LargeBinary),
        )
    return prompts, jobs


def _upgrade(engine, migration):
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()


RAN = datetime(2026, 3, 1, 12, 0)
# The instant a waiting task's cron job will fire next, as APScheduler stores it.
NEXT = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)
PAUSED = "paused"  # job row with next_run_time NULL

CASES = [
    # (cron, is_active, last_run_at, job next_run_time, expected)
    # expected: "off" = deactivated, "once" = converted to run_once_at, "kept" = untouched
    ("30 16 14 10 *", True, RAN, NEXT, "off"),     # one-time that ran
    ("5 3 1 1 *", True, RAN, NEXT, "off"),
    ("0 23 31 12 *", True, RAN, None, "off"),
    ("30 16 20 10 *", True, None, NEXT, "once"),   # one-time still waiting
    ("0 8 2 11 *", True, None, NEXT + timedelta(days=19), "once"),
    ("30 16 20 10 *", True, None, None, "kept"),   # waiting, no job to read the instant from
    ("30 16 20 10 *", True, None, PAUSED, "kept"),
    ("30 16 14 * *", True, RAN, NEXT, "kept"),     # recurring monthly
    ("30 16 14 * *", True, None, NEXT, "kept"),
    ("0 8 * * 1", True, RAN, NEXT, "kept"),        # weekly
    ("0 8 * * 1-5", True, None, NEXT, "kept"),
    ("*/15 * * * *", True, RAN, NEXT, "kept"),
    ("0 */2 * * *", True, RAN, NEXT, "kept"),
    ("0 9 1 1 1", True, RAN, NEXT, "kept"),        # weekday pinned: not the one-time shape
    ("0 0 9 1 1 *", True, RAN, NEXT, "kept"),      # 6-field: never written by the UI
    ("30 16 14 10 *", False, RAN, None, "kept"),   # already paused stays paused
    ("30 16 20 10 *", False, None, None, "kept"),
]


def test_upgrade_handles_each_legacy_shape(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/onceat.db")
    metadata = sa.MetaData()
    prompts, jobs = _schema(metadata, with_jobs=True)
    metadata.create_all(engine)

    ids = [str(uuid.uuid4()) for _ in CASES]
    with engine.begin() as conn:
        for sp_id, (cron, active, last_run, job, _) in zip(ids, CASES):
            conn.execute(prompts.insert().values(
                id=sp_id, cron_schedule=cron, is_active=active, last_run_at=last_run))
            if job is not None:
                conn.execute(jobs.insert().values(
                    id=f"scheduled_prompt_{sp_id}", job_state=b"",
                    next_run_time=None if job == PAUSED else job.timestamp()))

    _upgrade(engine, _load_migration())

    run_once_at = sa.Column("run_once_at", sa.DateTime)
    after_tbl = sa.Table("scheduled_prompts", sa.MetaData(), sa.Column("id", sa.String, primary_key=True),
                         sa.Column("cron_schedule", sa.String), sa.Column("is_active", sa.Boolean), run_once_at)
    with engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(sa.select(after_tbl)).all()}
        remaining_jobs = set(conn.execute(sa.select(jobs.c.id)).scalars())

    for sp_id, case in zip(ids, CASES):
        cron, active, _, job, expected = case
        row = rows[sp_id]
        had_job = job is not None
        if expected == "off":
            assert row.is_active is False, case
            assert row.cron_schedule == cron and row.run_once_at is None, case
            assert f"scheduled_prompt_{sp_id}" not in remaining_jobs, case
        elif expected == "once":
            assert row.is_active is True, case
            assert row.cron_schedule == "@once", case
            assert row.run_once_at == job.replace(tzinfo=None), case
            assert f"scheduled_prompt_{sp_id}" not in remaining_jobs, case
        else:
            assert row.is_active is active, case
            assert row.cron_schedule == cron and row.run_once_at is None, case
            assert (f"scheduled_prompt_{sp_id}" in remaining_jobs) is had_job, case


def test_upgrade_without_a_job_table_only_deactivates(tmp_path):
    """Before APScheduler ever started there is no job table: tasks that ran
    are still switched off, waiting ones cannot be converted and are kept."""
    engine = sa.create_engine(f"sqlite:///{tmp_path}/onceat_nojobs.db")
    metadata = sa.MetaData()
    prompts, _ = _schema(metadata, with_jobs=False)
    metadata.create_all(engine)
    ran_id, waiting_id = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(prompts.insert().values(id=ran_id, cron_schedule="30 16 14 10 *", is_active=True, last_run_at=RAN))
        conn.execute(prompts.insert().values(id=waiting_id, cron_schedule="30 16 20 10 *", is_active=True))

    _upgrade(engine, _load_migration())

    with engine.connect() as conn:
        active = dict(conn.execute(sa.select(prompts.c.id, prompts.c.is_active)).all())
        crons = dict(conn.execute(sa.select(prompts.c.id, prompts.c.cron_schedule)).all())
    assert active == {ran_id: False, waiting_id: True}
    assert crons[waiting_id] == "30 16 20 10 *"


def test_upgrade_leaves_soft_deleted_rows_alone(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/onceat_deleted.db")
    metadata = sa.MetaData()
    prompts, _ = _schema(metadata, with_jobs=False)
    metadata.create_all(engine)
    sp_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(prompts.insert().values(id=sp_id, cron_schedule="30 16 14 10 *",
                                             is_active=True, last_run_at=RAN, deleted_at=RAN))

    _upgrade(engine, _load_migration())

    with engine.connect() as conn:
        assert conn.execute(sa.select(prompts.c.is_active)).scalar_one() is True
