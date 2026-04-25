import os
import fcntl
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from app.settings.database import create_database_engine

# Use synchronous engine directly
jobstore = SQLAlchemyJobStore(
    engine=create_database_engine(),
    tablename='apscheduler_jobs'
)

scheduler = AsyncIOScheduler(
    jobstores={
        'default': jobstore
    }
)


_LEADER_LOCK_FD = None


def try_acquire_scheduler_leader() -> bool:
    """Return True if this process wins the scheduler leader lock.

    Multi-worker uvicorn deployments otherwise run every scheduled job N
    times (once per worker), which is what turns warmup jobs into resource
    storms at customer sites. The lock is held for the lifetime of the
    process — a crashed leader releases the flock and the next worker to
    start wins on its next startup.

    Override via BOW_SCHEDULER_LEADER=1 to force-enable (useful when running
    a dedicated scheduler sidecar) or BOW_SCHEDULER_DISABLED=1 to opt out.
    """
    if os.environ.get("BOW_SCHEDULER_DISABLED") == "1":
        return False
    if os.environ.get("BOW_SCHEDULER_LEADER") == "1":
        return True

    global _LEADER_LOCK_FD
    lock_path = os.environ.get("BOW_SCHEDULER_LOCK_PATH", "/tmp/bow-scheduler.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LEADER_LOCK_FD = fd  # keep fd alive for process lifetime
        return True
    except (OSError, BlockingIOError):
        return False
