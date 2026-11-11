"""A custom table's run history (cqruns01).

One row per refresh that started: what started it, the range of data it
covered, what it read from the source, what that did to the table, and a
short event log. The table row itself only remembers its last refresh.

Writes go through their own short session, never the refresh's: a refresh
that rolls back keeps its row, and a write that fails is logged and dropped —
recording never breaks a refresh. Call them only while the refresh's own
session holds no uncommitted write (SQLite allows one writer).

Rows go after RETENTION_DAYS (`purge_old_runs`, daily).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select

from app.models.custom_table_run import (
    MODE_FULL,
    STATUS_RUNNING,
    CustomTableRun,
)

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
# A row still running this long after its last sign of life: the server
# stopped mid-run. Longer than the longest a single refresh may take.
INTERRUPTED_AFTER = timedelta(minutes=45)
ERROR_LIMIT = 2000
QUERY_LIMIT = 20_000
MAX_EVENTS = 200
MAX_CHUNKS = 500


async def _write(work) -> object:
    """`work(db)` in a session of its own, committed."""
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        result = await work(db)
        await db.commit()
        return result


def _clip(text, limit: int):
    return (str(text)[:limit] or None) if text else None


def event(message: str, level: str = "info") -> dict:
    return {"at": datetime.utcnow().isoformat(timespec="seconds"), "level": level, "message": str(message)[:500]}


async def start(*, connection_table_id: str, organization_id: str, trigger: str, mode: str = MODE_FULL,
                triggered_by_user_id: str | None = None, inputs: dict | None = None,
                run_id: str | None = None) -> str | None:
    """The row of a refresh that is starting; its id, or None when it could
    not be written."""
    async def work(db):
        run = CustomTableRun(
            connection_table_id=str(connection_table_id), organization_id=str(organization_id),
            trigger=trigger, mode=mode, status=STATUS_RUNNING,
            triggered_by_user_id=str(triggered_by_user_id) if triggered_by_user_id else None,
            inputs=inputs, started_at=datetime.utcnow(), events=[], chunks=None,
        )
        if run_id:
            run.id = run_id
        db.add(run)
        await db.flush()
        return str(run.id)

    try:
        return await _write(work)
    except Exception:
        logger.warning("Custom table %s: run history row not started", connection_table_id, exc_info=True)
        return None


async def progress(run_id: str | None, *, events: list | None = None, chunks: list | None = None,
                   **fields) -> None:
    """Record what a run has done so far: new events, the chunk list, any
    plain column (mode, rendered_query, window_from, rows_read, …)."""
    if not run_id:
        return

    async def work(db):
        run = await db.get(CustomTableRun, run_id)
        if run is None:
            return
        if events:
            run.events = (list(run.events or []) + list(events))[-MAX_EVENTS:]
        if chunks is not None:
            run.chunks = list(chunks)[:MAX_CHUNKS]
        for name, value in fields.items():
            if name == "rendered_query":
                value = _clip(value, QUERY_LIMIT)
            setattr(run, name, value)
        # Proof of life for the interrupted check on a long chunked load.
        run.updated_at = datetime.utcnow()

    try:
        await _write(work)
    except Exception:
        logger.warning("Run history row %s not updated", run_id, exc_info=True)


async def finish(run_id: str | None, *, status: str, error: str | None = None,
                 events: list | None = None, chunks: list | None = None, **fields) -> None:
    """How the run `run_id` ended."""
    if not run_id:
        return

    async def work(db):
        run = await db.get(CustomTableRun, run_id)
        if run is None:
            return
        now = datetime.utcnow()
        run.status, run.error, run.finished_at = status, _clip(error, ERROR_LIMIT), now
        if run.started_at:
            run.elapsed_ms = int((now - run.started_at).total_seconds() * 1000)
        if events:
            run.events = (list(run.events or []) + list(events))[-MAX_EVENTS:]
        if chunks is not None:
            run.chunks = list(chunks)[:MAX_CHUNKS]
        for name, value in fields.items():
            if name == "rendered_query":
                value = _clip(value, QUERY_LIMIT)
            setattr(run, name, value)

    try:
        await _write(work)
    except Exception:
        logger.warning("Run history row %s not finished", run_id, exc_info=True)


# ── reading ──────────────────────────────────────────────────────────────

def _cursor(run) -> str:
    return f"{run.started_at.isoformat()}|{run.id}"


def _parse_cursor(before: str):
    at, sep, run_id = (before or "").partition("|")
    if not sep or not run_id:
        return None
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return None
    return (when, run_id) if when.tzinfo is None else None


def status_of(run, now: datetime | None = None) -> str:
    """The stored status, except that a run which stopped reporting reads as
    interrupted rather than running forever."""
    now = now or datetime.utcnow()
    last = run.updated_at or run.started_at
    if run.status == STATUS_RUNNING and last and now - last > INTERRUPTED_AFTER:
        return "interrupted"
    return run.status


def summary(run, people: dict | None = None, now: datetime | None = None) -> dict:
    return {
        "id": str(run.id),
        "trigger": run.trigger,
        "mode": run.mode,
        "status": status_of(run, now),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "elapsed_ms": run.elapsed_ms,
        "window_from": run.window_from,
        "window_to": run.window_to,
        "rows_read": run.rows_read,
        "rows_inserted": run.rows_inserted,
        "rows_updated": run.rows_updated,
        "rows_deleted": run.rows_deleted,
        "row_count": run.row_count,
        "artifact_bytes": run.artifact_bytes,
        "error": run.error,
        "triggered_by": (people or {}).get(str(run.triggered_by_user_id)) if run.triggered_by_user_id else None,
        "chunks_total": len(run.chunks) if run.chunks else None,
        "chunks_done": sum(1 for c in run.chunks if c.get("status") == "done") if run.chunks else None,
    }


async def _people(db, runs) -> dict:
    from app.models.user import User

    ids = {str(r.triggered_by_user_id) for r in runs if r.triggered_by_user_id}
    if not ids:
        return {}
    users = (await db.execute(select(User).where(User.id.in_(ids)))).scalars()
    return {str(u.id): {"id": str(u.id), "name": u.name or u.email} for u in users}


async def list_runs(db, connection_table_id: str, *, limit: int = 20, before: str | None = None) -> dict:
    """The table's runs, newest first, `limit` at a time; `before`: the
    previous page's `next_before`. A cursor that does not parse returns
    nothing (never the first page again)."""
    stmt = select(CustomTableRun).where(CustomTableRun.connection_table_id == str(connection_table_id))
    if before:
        cursor = _parse_cursor(before)
        if cursor is None:
            return {"runs": [], "has_more": False, "next_before": None}
        at, run_id = cursor
        stmt = stmt.where(or_(
            CustomTableRun.started_at < at,
            and_(CustomTableRun.started_at == at, CustomTableRun.id < run_id),
        ))
    rows = list((await db.execute(
        stmt.order_by(CustomTableRun.started_at.desc(), CustomTableRun.id.desc()).limit(limit + 1)
    )).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]
    people = await _people(db, rows)
    now = datetime.utcnow()
    return {
        "runs": [summary(r, people, now) for r in rows],
        "has_more": has_more,
        "next_before": _cursor(rows[-1]) if has_more and rows else None,
    }


async def get_run(db, connection_table_id: str, run_id: str) -> dict | None:
    """One run in full: the summary plus the query, the chunks and the log."""
    run = (await db.execute(
        select(CustomTableRun).where(
            CustomTableRun.id == str(run_id),
            CustomTableRun.connection_table_id == str(connection_table_id),
        )
    )).scalar_one_or_none()
    if run is None:
        return None
    out = summary(run, await _people(db, [run]))
    out.update({
        "rendered_query": run.rendered_query,
        "chunks": run.chunks or [],
        "events": run.events or [],
        "inputs": run.inputs,
        "schema_fingerprint": run.schema_fingerprint,
    })
    return out


# ── retention ────────────────────────────────────────────────────────────

async def purge_old_runs(days: int = RETENTION_DAYS) -> int:
    """Delete runs that started more than `days` ago (the daily job). The
    rows are a log, not a record: deleted, not soft-deleted."""
    import asyncio

    from app.core.scheduler import claim_scheduled_run

    if not await asyncio.to_thread(claim_scheduled_run, "purge_custom_table_runs_daily"):
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)

    async def work(db):
        result = await db.execute(delete(CustomTableRun).where(CustomTableRun.started_at < cutoff))
        return result.rowcount or 0

    removed = await _write(work)
    logger.info("Custom table run history: %d runs older than %d days deleted", removed, days)
    return removed
