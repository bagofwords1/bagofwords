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


_CRON_DOW_NUM_TO_NAME = {
    '0': 'sun', '1': 'mon', '2': 'tue', '3': 'wed',
    '4': 'thu', '5': 'fri', '6': 'sat', '7': 'sun',
}


def cron_dow_to_apscheduler(dow: str) -> str:
    """Translate a standard-cron day-of-week field into APScheduler's naming.

    Standard cron numbers weekdays 0=Sun..6=Sat (7=Sun too). APScheduler's
    numeric ``day_of_week`` instead uses 0=Mon..6=Sun, so feeding a standard
    cron number straight in shifts every weekday by one (e.g. Sunday '0' would
    fire Monday). We map numbers to APScheduler's unambiguous weekday NAMES
    (sun..sat) so the schedule means the same day in both conventions.

    Handles '*', comma lists ('0,6'), ranges ('1-5'); leaves names and step
    expressions ('*/2') untouched. The stored cron string is unchanged — this
    only adjusts the value handed to APScheduler at registration time.
    """
    if not dow or dow == '*' or '/' in dow:
        return dow

    def _map(token: str) -> str:
        t = token.strip().lower()
        return _CRON_DOW_NUM_TO_NAME.get(t, t)

    def _part(token: str) -> str:
        if '-' in token:
            a, _, b = token.partition('-')
            return f"{_map(a)}-{_map(b)}"
        return _map(token)

    return ','.join(_part(t) for t in dow.split(','))


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
