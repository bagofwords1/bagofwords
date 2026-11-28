"""Scheduled jobs for agent sandboxes.

Three jobs, on the scheduler leader, registered only while the deployment
flag is on:

``lifecycle``       mark idle, stop at the idle timeout, delete after the
                    stopped retention or the maximum lifetime, delete the
                    sandbox of a deleted report, and recover from a worker
                    that died in the middle of a create or a run
``compute health``  test each compute and each running sandbox; an
                    unreachable compute gets no new sandboxes, a sandbox that
                    is gone is marked failed (recreated on its next run)
``reconcile``       make each compute match the database: remove orphaned
                    sandboxes and routes, repair what a known one is missing

Each ``*_tick`` function does one pass and returns counts. They take ``now``
so the timeouts can be tested without waiting, and are safe to run from any
worker; a failure on one sandbox or compute never stops the rest of the pass.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, select, update

from app.models.sandbox import Sandbox, SandboxCompute, SandboxRun
from app.services.sandbox import computes as compute_service
from app.services.sandbox import errors
from app.services.sandbox.drivers.base import (
    CAP_STOP_RESUME, STATUS_MISSING, STATUS_STOPPED, SandboxDriverError, SandboxHandle,
)
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.feature import sandbox_feature_enabled
from app.services.sandbox.service import SandboxService
from app.services.sandbox.settings import SandboxSettings, load_settings

logger = logging.getLogger(__name__)

# Every sandbox job id starts with this, so tests and operators can tell them apart.
JOB_ID_PREFIX = "agent_sandbox_"

LIFECYCLE_INTERVAL_SECONDS = 60
HEALTH_INTERVAL_SECONDS = 120
RECONCILE_INTERVAL_SECONDS = 600

# A create that never finished, and a run that outlived any possible timeout:
# the worker doing it died. Both are generous so a slow image pull or a long
# run is never mistaken for one.
STUCK_PROVISIONING_SECONDS = 30 * 60
STUCK_RUN_SLACK_SECONDS = 15 * 60
# A failed sandbox is recreated by its next run; with no next run it is cleared out after this.
FAILED_RETENTION_SECONDS = 24 * 3600


def _session_factory(session_factory=None):
    if session_factory is not None:
        return session_factory
    from app.dependencies import async_session_maker

    return async_session_maker


async def _organizations_with_sandboxes(db) -> List[str]:
    rows = await db.execute(select(Sandbox.organization_id).where(Sandbox.deleted_at.is_(None)).distinct())
    return [row[0] for row in rows.all()]


async def _report_is_gone(db, report_id: str) -> bool:
    from app.models.report import Report

    # "Deleting" a report in the app archives it; a row can also be soft-deleted or gone.
    row = (await db.execute(
        select(Report.id, Report.deleted_at, Report.status).where(Report.id == report_id)
    )).first()
    return row is None or row[1] is not None or row[2] == "archived"


# ── lifecycle ────────────────────────────────────────────────────────────


async def lifecycle_tick(session_factory=None, now: Optional[datetime] = None) -> Dict[str, int]:
    """One pass over every live sandbox. Returns how many were marked idle, stopped, deleted, recovered."""
    now = now or datetime.utcnow()
    counts = {"idle": 0, "stopped": 0, "deleted": 0, "recovered": 0, "errors": 0}
    service = SandboxService(session_factory=session_factory)
    async with _session_factory(session_factory)() as db:
        settings_by_org: Dict[str, SandboxSettings] = {}
        rows = await db.execute(select(Sandbox.id).where(Sandbox.deleted_at.is_(None)))
        for sandbox_id in [row[0] for row in rows.all()]:
            try:
                sandbox = await db.get(Sandbox, sandbox_id, populate_existing=True)
                if sandbox is None or sandbox.deleted_at is not None:
                    continue
                if sandbox.organization_id not in settings_by_org:
                    settings_by_org[sandbox.organization_id] = await load_settings(db, sandbox.organization_id)
                outcome = await _lifecycle_one(db, service, sandbox, settings_by_org[sandbox.organization_id], now)
                if outcome:
                    counts[outcome] += 1
            except Exception:  # noqa: BLE001 - one sandbox must not stop the pass
                counts["errors"] += 1
                await db.rollback()
                logger.exception("sandbox lifecycle failed for sandbox %s", sandbox_id)
    return counts


async def _lifecycle_one(db, service: SandboxService, sandbox: Sandbox, settings: SandboxSettings,
                         now: datetime) -> Optional[str]:
    age = lambda moment: (now - moment).total_seconds() if moment else None  # noqa: E731
    used = sandbox.last_used_at or sandbox.created_at

    # A worker died mid-run or mid-create: put things back so the sandbox is usable.
    if sandbox.state == "busy":
        limit = int((sandbox.limits or {}).get("run_timeout_seconds") or 3600) + STUCK_RUN_SLACK_SECONDS
        if (age(used) or 0) > limit:
            # Only if nothing touched it since this pass read it: a run that
            # finished and another that started would look the same by state alone.
            released = await db.execute(
                update(Sandbox).where(
                    Sandbox.id == sandbox.id, Sandbox.state == "busy", Sandbox.last_used_at == sandbox.last_used_at,
                ).values(state="ready")
            )
            if released.rowcount != 1:
                await db.rollback()
                return None
            await _fail_running_runs(db, sandbox.id, now)
            await service._event(db, sandbox.id, "ready", None,
                                 {"from": "busy", "to": "ready", "reason": "the run was abandoned"})
            await db.commit()
            await db.refresh(sandbox)
            return "recovered"
        return None
    if sandbox.state == "provisioning":
        if (age(sandbox.created_at) or 0) > STUCK_PROVISIONING_SECONDS:
            sandbox.error = "Creation did not finish"
            await service._set_state(db, sandbox, "failed", None, details={"reason": "creation did not finish"})
            await db.commit()
            return "recovered"
        return None

    # Deletion: the report is gone, the sandbox reached its maximum lifetime, or it stayed stopped too long.
    reason = None
    if await _report_is_gone(db, sandbox.report_id):
        reason = "report deleted"
    elif sandbox.expires_at and sandbox.expires_at <= now:
        reason = "expired"
    elif sandbox.state == "stopped" and (age(sandbox.stopped_at) or 0) >= settings.stopped_retention_seconds:
        reason = "stopped too long"
    elif sandbox.state == "failed" and (age(sandbox.updated_at) or 0) >= FAILED_RETENTION_SECONDS:
        reason = "failed"
    if reason:
        if reason == "expired":
            await service._event(db, sandbox.id, "expired", None, {"expires_at": sandbox.expires_at.isoformat()})
        await service.delete(db, sandbox, None, reason=reason)
        return "deleted"

    if sandbox.state in ("ready", "idle"):
        unused = age(used) or 0
        if unused >= settings.idle_timeout_seconds:
            # Stop, do not delete: the next run resumes it with its files and packages.
            compute = await db.get(SandboxCompute, sandbox.compute_id) if sandbox.compute_id else None
            if compute is not None and CAP_STOP_RESUME not in (compute.capabilities or []):
                return None  # this compute cannot stop; the maximum lifetime still applies
            try:
                await service.stop(db, sandbox, None, reason="idle")
            except SandboxError as exc:
                if exc.code == errors.BUSY:
                    return None  # a run claimed it since this pass read it
                raise
            return "stopped"
        if sandbox.state == "ready" and unused >= settings.mark_idle_after_seconds:
            return "idle" if await service.mark_idle(db, sandbox) else None
    return None


async def _fail_running_runs(db, sandbox_id: str, now: datetime) -> None:
    rows = await db.execute(select(SandboxRun).where(SandboxRun.sandbox_id == sandbox_id, SandboxRun.status == "running"))
    for run in rows.scalars().all():
        run.status, run.finished_at = "failed", now


# ── compute health ───────────────────────────────────────────────────────


async def compute_health_tick(session_factory=None, now: Optional[datetime] = None) -> Dict[str, int]:
    """Test every compute, then every running sandbox on the reachable ones."""
    counts = {"healthy": 0, "unreachable": 0, "sandboxes_failed": 0, "sandboxes_stopped": 0, "errors": 0}
    service = SandboxService(session_factory=session_factory)
    async with _session_factory(session_factory)() as db:
        rows = await db.execute(select(SandboxCompute.id).where(SandboxCompute.deleted_at.is_(None)))
        for compute_id in [row[0] for row in rows.all()]:
            try:
                compute = await db.get(SandboxCompute, compute_id, populate_existing=True)
                if compute is None or compute.bootstrap_status != "ready":
                    continue
                health = await compute_service.check_compute(db, compute.organization_id, compute)
                if not health.reachable:
                    counts["unreachable"] += 1
                    continue  # its sandboxes cannot be judged while the compute is unreachable
                counts["healthy"] += 1 if health.healthy else 0
                await _check_sandboxes(db, service, compute, counts)
            except Exception:  # noqa: BLE001
                counts["errors"] += 1
                await db.rollback()
                logger.exception("sandbox compute health check failed for compute %s", compute_id)
    return counts


async def _check_sandboxes(db, service: SandboxService, compute: SandboxCompute, counts: Dict[str, int]) -> None:
    rows = await db.execute(
        select(Sandbox).where(Sandbox.compute_id == compute.id, Sandbox.deleted_at.is_(None),
                              Sandbox.state.in_(("ready", "idle")))
    )
    sandboxes = list(rows.scalars().all())
    if not sandboxes:
        return
    driver = await compute_service.build_driver(db, compute)
    try:
        for sandbox in sandboxes:
            try:
                async with service._runtime(driver, sandbox) as runtime:
                    if await runtime.health():
                        sandbox.last_seen_at = datetime.utcnow()
                        continue
                status = await driver.status(service._handle(sandbox))
                # Re-read: a run may have claimed or resumed it while we were checking.
                await db.refresh(sandbox)
                if sandbox.state not in ("ready", "idle"):
                    continue
                if status.state == STATUS_STOPPED:
                    await service._set_state(db, sandbox, "stopped", None, details={"reason": "found stopped"})
                    counts["sandboxes_stopped"] += 1
                else:
                    reason = "it is no longer on its compute" if status.state == STATUS_MISSING else "it does not answer"
                    sandbox.error = f"lost: {reason}"
                    await service._set_state(db, sandbox, "failed", None, details={"reason": f"lost: {reason}"})
                    counts["sandboxes_failed"] += 1
            except (SandboxDriverError, SandboxError):
                counts["errors"] += 1
        await db.commit()
    finally:
        await driver.close()


# ── reconcile ────────────────────────────────────────────────────────────


async def reconcile_tick(session_factory=None, now: Optional[datetime] = None) -> Dict[str, int]:
    """Make every bootstrapped, reachable compute match the database."""
    counts = {"computes": 0, "removed": 0, "repaired": 0, "lost": 0, "errors": 0}
    service = SandboxService(session_factory=session_factory)
    async with _session_factory(session_factory)() as db:
        rows = await db.execute(select(SandboxCompute.id).where(SandboxCompute.deleted_at.is_(None)))
        for compute_id in [row[0] for row in rows.all()]:
            try:
                compute = await db.get(SandboxCompute, compute_id, populate_existing=True)
                if compute is None or compute.bootstrap_status != "ready" or compute.status == "unreachable":
                    continue
                # Every sandbox the database knows on this compute, including one
                # being created right now: it must never be taken for an orphan.
                known_rows = await db.execute(
                    select(Sandbox).where(Sandbox.compute_id == compute.id, Sandbox.deleted_at.is_(None))
                )
                known = list(known_rows.scalars().all())
                driver = await compute_service.build_driver(db, compute)
                try:
                    result = await driver.reconcile([service._handle(s) for s in known])
                finally:
                    await driver.close()
                counts["computes"] += 1
                counts["removed"] += len(result.removed)
                counts["repaired"] += len(result.repaired)
                missing = set(result.missing)
                for sandbox in known:
                    if sandbox.id in missing and sandbox.state in ("ready", "idle", "stopped"):
                        sandbox.error = "lost: it is no longer on its compute"
                        await service._set_state(db, sandbox, "failed", None,
                                                 details={"reason": "lost: it is no longer on its compute"})
                        counts["lost"] += 1
                if result.removed or result.repaired:
                    logger.info("sandbox reconcile on %s: removed %s, repaired %s", compute.name, result.removed, result.repaired)
                await db.commit()
            except Exception:  # noqa: BLE001
                counts["errors"] += 1
                await db.rollback()
                logger.exception("sandbox reconcile failed for compute %s", compute_id)
    return counts


# ── registration and startup ─────────────────────────────────────────────


async def run_lifecycle_job() -> None:
    await lifecycle_tick()


async def run_compute_health_job() -> None:
    await compute_health_tick()


async def run_reconcile_job() -> None:
    await reconcile_tick()


_JOBS = (
    ("lifecycle", run_lifecycle_job, LIFECYCLE_INTERVAL_SECONDS),
    ("compute_health", run_compute_health_job, HEALTH_INTERVAL_SECONDS),
    ("reconcile", run_reconcile_job, RECONCILE_INTERVAL_SECONDS),
)


def register_sandbox_jobs(scheduler) -> List[str]:
    """Register the sandbox jobs on ``scheduler``; returns the job ids added.

    A no-op returning ``[]`` when the feature is off for this deployment.
    """
    if not sandbox_feature_enabled():
        return []
    job_ids: List[str] = []
    for name, function, seconds in _JOBS:
        job_id = f"{JOB_ID_PREFIX}{name}"
        scheduler.add_job(
            function, trigger="interval", seconds=seconds, id=job_id, replace_existing=True,
            coalesce=True, max_instances=1, misfire_grace_time=seconds,
        )
        job_ids.append(job_id)
    return job_ids


async def warn_if_sandboxes_exist_while_disabled(session_factory=None) -> int:
    """With the flag off nothing cleans up, so sandboxes left behind keep
    running on their servers. Logs a warning naming how many; returns the count."""
    if sandbox_feature_enabled():
        return 0
    try:
        async with _session_factory(session_factory)() as db:
            count = (await db.execute(
                select(func.count()).select_from(Sandbox).where(
                    Sandbox.deleted_at.is_(None), Sandbox.state.in_(compute_service.LIVE_STATES),
                )
            )).scalar() or 0
    except Exception:  # noqa: BLE001 - e.g. the table does not exist yet during the first migration
        return 0
    if count:
        logger.warning(
            "Agent sandboxes are turned off (BOW_AGENT_SANDBOXES_ENABLED) but %d sandbox(es) still exist. "
            "They are not being stopped or deleted while the feature is off: turn it back on and delete them "
            "from Settings > Sandbox, or remove them from their servers.", count,
        )
    return count
