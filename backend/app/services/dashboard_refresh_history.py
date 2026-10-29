"""A dashboard's refresh history (refhist01, decided 2026-10-25).

One row per refresh that ran -- the published release's
(dashboard_refresh.refresh) or the draft's (ReportService.rerun_report_steps)
-- and per scheduled run that could not run. A view that did not refresh
(fresh, off, already running) is not a refresh and leaves nothing.

Writes go through their own short session, never the refresh's: a refresh
that rolls back keeps its row, and a write that fails is logged and
dropped -- recording never breaks a refresh. Call them only while the
refresh's own session holds no uncommitted write (SQLite allows one writer).

The owner and the dashboard's editors read it (the route). Rows go after
RETENTION_DAYS (`purge_old_runs`, daily).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select

from app.models.dashboard_refresh_run import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    DashboardRefreshRun,
)

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
# A row still running this long after it started: the server stopped mid-run.
INTERRUPTED_AFTER = timedelta(hours=1)
ERROR_LIMIT = 1000


async def _write(work) -> object:
    """`work(db)` in a session of its own, committed."""
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        result = await work(db)
        await db.commit()
        return result


def _clip(error: str | None) -> str | None:
    return (str(error)[:ERROR_LIMIT] or None) if error else None


async def start(*, artifact_id: str, organization_id: str, trigger: str, run_as_user_id: str | None = None,
                triggered_by_user_id: str | None = None, release_id: str | None = None,
                report_id: str | None = None) -> str | None:
    """The row of a refresh that is starting; its id, or None when it could
    not be written."""
    async def work(db):
        run = DashboardRefreshRun(
            artifact_id=str(artifact_id), organization_id=str(organization_id), trigger=trigger,
            status=STATUS_RUNNING, run_as_user_id=run_as_user_id, triggered_by_user_id=triggered_by_user_id,
            release_id=release_id, report_id=report_id, started_at=datetime.utcnow(),
        )
        db.add(run)
        await db.flush()
        return str(run.id)

    try:
        return await _write(work)
    except Exception:
        logger.warning("Dashboard %s: refresh history row not started", artifact_id, exc_info=True)
        return None


async def finish(run_id: str | None, *, status: str, steps_total: int = 0, steps_succeeded: int = 0,
                 steps_failed: int = 0, error: str | None = None, error_source: str | None = None,
                 skip_reason: str | None = None) -> None:
    """How the refresh `run_id` ended. `error_source`: the query or
    connection the first failure was in, when the run knows it; `error`:
    the reason, without it."""
    if not run_id:
        return

    async def work(db):
        run = await db.get(DashboardRefreshRun, run_id)
        if run is None:
            return
        run.status, run.skip_reason, run.error = status, skip_reason, _clip(error)
        run.error_source = (str(error_source)[:255] or None) if error_source else None
        run.steps_total, run.steps_succeeded, run.steps_failed = steps_total, steps_succeeded, steps_failed
        run.finished_at = datetime.utcnow()

    try:
        await _write(work)
    except Exception:
        logger.warning("Refresh history row %s not finished", run_id, exc_info=True)


async def skipped(*, artifact_id: str, organization_id: str, trigger: str, reason: str,
                  run_as_user_id: str | None = None, release_id: str | None = None,
                  report_id: str | None = None) -> None:
    """A run that did not happen (a scheduled one) or did not count: one
    row, written once, already final."""
    async def work(db):
        now = datetime.utcnow()
        db.add(DashboardRefreshRun(
            artifact_id=str(artifact_id), organization_id=str(organization_id), trigger=trigger,
            status=STATUS_SKIPPED, skip_reason=reason, run_as_user_id=run_as_user_id,
            release_id=release_id, report_id=report_id, started_at=now, finished_at=now,
        ))

    try:
        await _write(work)
    except Exception:
        logger.warning("Dashboard %s: skipped refresh not recorded", artifact_id, exc_info=True)


def draft_outcome(steps_total: int, steps_succeeded: int, steps_failed: int) -> tuple[str, str | None]:
    """(status, skip_reason) of a draft's run from what it counted: a draft
    is not all or nothing, so some queries can refresh while others fail."""
    if steps_total == 0:
        return STATUS_SKIPPED, "nothing_to_run"
    if steps_failed == 0:
        return STATUS_SUCCESS, None
    if steps_succeeded == 0:
        return STATUS_FAILED, None
    return STATUS_PARTIAL, None


async def dashboard_of_rerun(db, report_id: str, version_id: str | None) -> tuple[str, str] | None:
    """(artifact_id, organization_id) of the dashboard a conversation's
    rerun refreshes: the version's, or -- no version named -- the
    conversation's newest dashboard, exactly as the rerun picks it
    (ReportService._artifact_query_ids: live versions, the parent's own
    deleted_at not consulted). None: no dashboard."""
    from app.models.artifact import Artifact, ArtifactVersion

    stmt = (
        select(Artifact.id, Artifact.organization_id)
        .join(ArtifactVersion, ArtifactVersion.artifact_id == Artifact.id)
        .where(ArtifactVersion.report_id == str(report_id), ArtifactVersion.deleted_at.is_(None))
        .order_by(ArtifactVersion.created_at.desc())
        .limit(1)
    )
    if version_id:
        stmt = stmt.where(ArtifactVersion.id == str(version_id))
    else:
        stmt = stmt.where(Artifact.mode.in_(("page", "slides")))
    row = (await db.execute(stmt)).first()
    return (str(row[0]), str(row[1])) if row else None


# ── reading ──────────────────────────────────────────────────────────────

def _cursor(run) -> str:
    """Where the next page starts: the last row's own values, so a row the
    daily purge deleted meanwhile still marks the place."""
    return f"{run.started_at.isoformat()}|{run.id}"


def _parse_cursor(before: str) -> tuple[datetime, str] | None:
    at, sep, run_id = (before or "").partition("|")
    if not sep or not run_id:
        return None
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return None
    # The column is naive UTC and every cursor this module writes is too: an
    # aware one was not ours (and Postgres would refuse the comparison).
    return (when, run_id) if when.tzinfo is None else None


def _kind(run) -> str | None:
    """Which version the run refreshed: the published one (a release) or a
    draft (a conversation's); None when it never got that far."""
    if run.release_id:
        return "published"
    if run.report_id:
        return "draft"
    return None


async def list_runs(db, artifact_id: str, *, limit: int = 20, before: str | None = None) -> dict:
    """The dashboard's runs, newest first, `limit` at a time; `before`: the
    previous page's `next_before`. A cursor that does not parse returns
    nothing (never the first page again)."""
    from app.models.user import User

    stmt = select(DashboardRefreshRun).where(DashboardRefreshRun.artifact_id == str(artifact_id))
    if before:
        cursor = _parse_cursor(before)
        if cursor is None:
            return {"runs": [], "has_more": False, "next_before": None}
        at, run_id = cursor
        stmt = stmt.where(or_(
            DashboardRefreshRun.started_at < at,
            and_(DashboardRefreshRun.started_at == at, DashboardRefreshRun.id < run_id),
        ))
    rows = list((await db.execute(
        stmt.order_by(DashboardRefreshRun.started_at.desc(), DashboardRefreshRun.id.desc()).limit(limit + 1)
    )).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    user_ids = {str(u) for r in rows for u in (r.run_as_user_id, r.triggered_by_user_id) if u}
    people = {}
    if user_ids:
        for user in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars():
            people[str(user.id)] = {"id": str(user.id), "name": user.name or user.email}

    now = datetime.utcnow()

    def status_of(run) -> str:
        if run.status == STATUS_RUNNING and run.started_at and now - run.started_at > INTERRUPTED_AFTER:
            return "interrupted"
        return run.status

    def counted(run) -> bool:
        # Counts mean something once a run counted its queries: a published
        # run that failed stopped before counting, a skipped one never ran.
        return bool(run.steps_total)

    return {
        "runs": [{
            "id": str(r.id), "trigger": r.trigger, "status": status_of(r), "skip_reason": r.skip_reason,
            "kind": _kind(r),
            "steps_total": r.steps_total if counted(r) else None,
            "steps_succeeded": r.steps_succeeded if counted(r) else None,
            "steps_failed": r.steps_failed if counted(r) else None,
            "error": r.error, "error_source": r.error_source,
            "run_as": people.get(str(r.run_as_user_id)) if r.run_as_user_id else None,
            "triggered_by": people.get(str(r.triggered_by_user_id)) if r.triggered_by_user_id else None,
            "started_at": r.started_at, "finished_at": r.finished_at,
        } for r in rows],
        "has_more": has_more,
        "next_before": _cursor(rows[-1]) if has_more and rows else None,
    }


# ── retention ────────────────────────────────────────────────────────────

async def purge_old_runs(days: int = RETENTION_DAYS) -> int:
    """Delete runs that started more than `days` ago (the daily job). The
    rows are a log, not a record: deleted, not soft-deleted."""
    import asyncio

    from app.core.scheduler import claim_scheduled_run

    # Fires in every worker (shared job store); claim so only one purges,
    # as purge_step_payloads does.
    if not await asyncio.to_thread(claim_scheduled_run, "purge_dashboard_refresh_runs_daily"):
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)

    async def work(db):
        result = await db.execute(delete(DashboardRefreshRun).where(DashboardRefreshRun.started_at < cutoff))
        return result.rowcount or 0

    removed = await _write(work)
    logger.info("Dashboard refresh history: %d runs older than %d days deleted", removed, days)
    return removed
