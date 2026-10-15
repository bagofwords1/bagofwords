"""A published dashboard refreshes (phase 6).

Three triggers, one run (`refresh`):

- the dashboard's schedule (`scheduled_refresh`, an APScheduler job per
  dashboard holding only its id): run as the dashboard's owner as they are
  at run time, while still in the organization; subscribers are told on
  success (inbox and email, a link to /d), the owner on failure;
- a reader opening it (`refresh_on_view`): the conversation's rule -- the
  owner's credentials, at most once per REFRESH_ON_VIEW_MIN_INTERVAL_SECONDS,
  one at a time; no thumbnail, no email;
- the owner's Refresh (`refresh` from the route).

The run is Publish's (dashboard_publish.run_release_copies) over the live
release, without the copy: every copied step, what it loads first, with
the owner's credentials. All or nothing: the rows are kept until every step
ran, then written at once while the release is still the live one; readers'
own results on it go, as a conversation refresh drops them. A failure
writes no row and is recorded on the dashboard (last_run_status /
last_run_error); last_run_at is the last success. A refresh does not start
while a publish runs, and a published deck is frozen.

The schedule fires in the organization's timezone when it has one, else on
the server's clock -- the rule scheduled prompts follow. The leader
re-registers every dashboard's job at startup (`register_all_jobs`); the
conversations' own schedules are left as they are until the data migration.
"""
import asyncio
import contextlib
import logging
from datetime import datetime

from apscheduler.jobstores.base import JobLookupError
from sqlalchemy import delete, select, update
from sqlalchemy.orm import lazyload

from app.core.scheduler import claim_scheduled_run, scheduler
from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactRelease
from app.models.user import User

logger = logging.getLogger(__name__)

JOB_PREFIX = "dashboard_"
_ERROR_LIMIT = 2000


def _interval() -> int:
    from app.services.report_service import REFRESH_ON_VIEW_MIN_INTERVAL_SECONDS
    return REFRESH_ON_VIEW_MIN_INTERVAL_SECONDS


# ── the schedule's job ───────────────────────────────────────────────────

def job_id(artifact_id: str) -> str:
    return f"{JOB_PREFIX}{artifact_id}"


def parse_cron(expression: str | None) -> dict | None:
    """The trigger fields of a 5- or 6-field cron, as a conversation's
    schedule reads it; None to unschedule. ValueError when malformed."""
    from apscheduler.triggers.cron import CronTrigger

    from app.services.report_service import ReportService

    fields = ReportService()._parse_cron_expression(expression)
    if fields is not None:
        CronTrigger(**fields)  # every field valid, not only the count
    return fields


async def org_timezone(db, organization_id: str) -> str | None:
    """The organization's timezone (IANA), as scheduled prompts read it;
    None when it has none (the server's clock)."""
    from app.models.organization_settings import OrganizationSettings

    try:
        config = (await db.execute(
            select(OrganizationSettings.config).where(OrganizationSettings.organization_id == str(organization_id))
        )).scalar()
        if isinstance(config, str):
            import json
            config = json.loads(config)
        return (config or {}).get("timezone") or None
    except Exception:
        return None


def remove_job(artifact_id: str) -> None:
    with contextlib.suppress(JobLookupError):
        scheduler.remove_job(job_id(artifact_id))


def register_job(artifact_id: str, cron: str | None, timezone: str | None) -> None:
    """The dashboard's job for `cron` in `timezone` (None: the server's
    clock), or none."""
    remove_job(artifact_id)
    fields = parse_cron(cron)
    if fields is None:
        return
    if timezone:
        fields = {**fields, "timezone": timezone}
    scheduler.add_job(func=scheduled_refresh, trigger="cron", id=job_id(artifact_id), args=[str(artifact_id)],
                      replace_existing=True, **fields)


async def register_all_jobs() -> None:
    """At startup, on the leader: one job per published, scheduled dashboard,
    and no other dashboard job. The same on every call."""
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Artifact.id, Artifact.organization_id, Artifact.cron_schedule).where(
                Artifact.cron_schedule.isnot(None), Artifact.cron_schedule != "",
                Artifact.published_release_id.isnot(None), Artifact.deleted_at.is_(None),
                Artifact.mode != "slides",  # a published deck is frozen
            )
        )).all()
        zones = {org: await org_timezone(db, org) for org in {str(r[1]) for r in rows}}
    wanted = set()
    for artifact_id, organization_id, cron in rows:
        try:
            register_job(str(artifact_id), cron, zones.get(str(organization_id)))
            wanted.add(job_id(str(artifact_id)))
        except Exception:
            logger.warning("Dashboard %s: schedule %r not registered", artifact_id, cron, exc_info=True)
    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIX) and job.id not in wanted:
            remove_job(job.id[len(JOB_PREFIX):])
    logger.info("Registered %d dashboard schedules", len(wanted))


# ── the run ──────────────────────────────────────────────────────────────

async def _publishing(db, artifact: Artifact) -> bool:
    """A publish is running -- after failing one that stopped beating, so a
    crashed publish does not hold refreshes back."""
    from app.services.dashboard_publish import reap_stale

    await reap_stale(db, artifact)
    return (await db.execute(
        select(ArtifactRelease.id).where(ArtifactRelease.artifact_id == str(artifact.id),
                                         ArtifactRelease.status == "publishing")
    )).first() is not None


async def _record_failure(db, artifact_id: str, release_id: str | None, error: str) -> bool:
    """The failure on the dashboard, only while `release_id` is still the
    live release (None: whatever is live): a publish that replaced it
    meanwhile has the last word. False when it was replaced."""
    await db.rollback()
    where = [Artifact.id == artifact_id]
    if release_id is not None:
        where.append(Artifact.published_release_id == release_id)
    recorded = await db.execute(update(Artifact).where(*where)
                                .values(last_run_status="failed", last_run_error=error[:_ERROR_LIMIT]))
    await db.commit()
    return recorded.rowcount == 1


def _result(status: str, *, error=None, reason=None, last_run_at=None, steps=0) -> dict:
    return {"status": status, "error": error, "reason": reason, "last_run_at": last_run_at, "steps_total": steps}


async def refresh(db, artifact: Artifact, run_user: User, organization, *, trigger: str,
                  thumbnail: bool) -> dict:
    """Run the live release's copies as `run_user`; write every row at once,
    or none. status: success | failed | skipped (with a reason)."""
    from app.ee.audit.service import audit_service
    from app.services.dashboard_access import published_release

    # Read now: a failed run rolls the session back, expiring every object.
    artifact_id, user_id, organization_id = str(artifact.id), str(run_user.id), str(organization.id)
    release = await published_release(db, artifact)
    if release is None:
        return _result("skipped", reason="not_published")
    if artifact.mode == "slides":
        return _result("skipped", reason="frozen")  # a published deck does not run
    if await _publishing(db, artifact):
        return _result("skipped", reason="publishing")
    release_id = str(release.id)
    try:
        result = await _run_and_write(db, artifact_id, release, run_user, organization)
    except Exception as e:
        logger.warning("Dashboard %s: refresh failed", artifact_id, exc_info=True)
        result = _result("failed", error=f"Refresh failed: {str(getattr(e, 'detail', None) or e)}"[:_ERROR_LIMIT])
    if result["status"] == "failed":
        if not await _record_failure(db, artifact_id, release_id, result["error"]):
            return _result("skipped", reason="replaced")  # published meanwhile: its outcome stands
    if result["status"] == "success" and thumbnail:
        from app.core import fire_and_forget
        from app.services.thumbnail_service import ThumbnailService
        fire_and_forget.spawn(ThumbnailService().regenerate_for_release(release_id))
    if result["status"] in ("success", "failed"):
        await _audit(db, audit_service, organization_id, user_id, artifact_id, trigger, result["status"],
                     result["error"])
    return result


async def _run_and_write(db, artifact_id: str, release: ArtifactRelease, run_user: User, organization) -> dict:
    from app.models.step_user_result import StepUserResult
    from app.services.dashboard_publish import _ABANDONED, run_release_copies

    release_id = str(release.id)
    results: list = []

    async def keep(step, data, params) -> None:
        results.append((step, data, params))

    # The shared rows are about to change: monitoring data the run reads
    # extends the release's access lineage, as a conversation's refresh
    # extends the conversation's (bow_source_access.protect_release); in this
    # transaction, so a failure leaves it as it was.
    release.refreshing = True
    outcome = await run_release_copies(db, release, run_user, organization, after_step=keep)
    if outcome is _ABANDONED:
        return _result("skipped", reason="stopped")
    if outcome is not None:
        return _result("failed", error=outcome[:_ERROR_LIMIT])

    live = (await db.execute(
        select(Artifact).options(lazyload("*")).where(Artifact.id == artifact_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if live is None or str(live.published_release_id or "") != release_id:
        await db.rollback()
        return _result("skipped", reason="replaced")  # published meanwhile: never write an old release
    for step, data, params in results:
        step.data = data
        step.applied_params = dict(params) if params else None
        step.status = "success"
    step_ids = [str(step.id) for step, _, _ in results]
    if step_ids:
        # The shared rows changed: readers' own results on them are stale,
        # as a conversation refresh drops them.
        await db.execute(delete(StepUserResult).where(StepUserResult.step_id.in_(step_ids)))
    now = datetime.utcnow()
    live.last_run_at, live.last_run_status, live.last_run_error = now, "success", None
    await db.commit()
    return _result("success", last_run_at=now, steps=len(results))


async def _audit(db, audit_service, organization_id, user_id, artifact_id, trigger, status, error) -> None:
    """The audit row, after the refresh committed; it never fails the refresh."""
    with contextlib.suppress(Exception):
        await audit_service.log(db=db, organization_id=organization_id, action="dashboard.refreshed",
                                user_id=user_id,
                                resource_type="artifact", resource_id=artifact_id,
                                details={"trigger": trigger, "status": status,
                                         **({"error": error[:500]} if error else {})})


async def owner_refresh(db, artifact: Artifact, owner: User, organization) -> dict:
    """The owner's Refresh: now, with the thumbnail; nothing published or a
    publish running is refused."""
    from app.services.dashboard_access import published_release

    if await published_release(db, artifact) is None:
        raise AppError.conflict(ErrorCode.DASHBOARD_NOT_PUBLISHED, "Publish the dashboard first")
    if await _publishing(db, artifact):
        raise AppError.conflict(ErrorCode.DASHBOARD_PUBLISH_IN_PROGRESS, "This dashboard is being published")
    return await refresh(db, artifact, owner, organization, trigger="owner", thumbnail=True)


async def refresh_on_view(db, artifact: Artifact) -> dict:
    """A reader opened the dashboard (access already checked): refresh as
    the owner unless it is off, fresh, being published or already running."""
    from app.models.organization import Organization
    from app.services.dashboard_access import counts_as_owner

    def skipped(reason: str) -> dict:
        return {"skipped": True, "reason": reason, "last_run_at": artifact.last_run_at}

    if not artifact.refresh_on_view:
        return skipped("disabled")
    if await _publishing(db, artifact):
        return skipped("publishing")
    interval = _interval()
    if artifact.last_run_at is not None and (datetime.utcnow() - artifact.last_run_at).total_seconds() < interval:
        return skipped("fresh")
    if not await asyncio.to_thread(claim_scheduled_run, f"dashboard_view_{artifact.id}", interval):
        return skipped("running")
    owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
    if owner is None or not await counts_as_owner(db, artifact, owner):
        return skipped("owner")
    organization = await db.get(Organization, str(artifact.organization_id))
    result = await refresh(db, artifact, owner, organization, trigger="view", thumbnail=False)
    return {"skipped": result["status"] == "skipped", "reason": result["reason"], "status": result["status"],
            "last_run_at": result["last_run_at"]}


# ── scheduled ────────────────────────────────────────────────────────────

async def scheduled_refresh(artifact_id: str) -> None:
    """The dashboard's job. One worker runs a fire; a dashboard that is gone,
    unpublished or unscheduled drops its job."""
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.services.dashboard_access import counts_as_owner

    if not await asyncio.to_thread(claim_scheduled_run, job_id(artifact_id)):
        return
    async with async_session_maker() as db:
        artifact = await db.get(Artifact, str(artifact_id))
        if (artifact is None or artifact.deleted_at is not None or not artifact.cron_schedule
                or not artifact.published_release_id):
            remove_job(artifact_id)
            return
        organization = await db.get(Organization, str(artifact.organization_id))
        owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
        if owner is None or organization is None or not await counts_as_owner(db, artifact, owner):
            await _record_failure(db, str(artifact_id), None,
                                  "The dashboard's owner is no longer a member of the organization")
            return
        title, subscribers = artifact.title or "Untitled", artifact.notification_subscribers
        owner_id, organization_id = str(owner.id), str(organization.id)
        result = await refresh(db, artifact, owner, organization, trigger="schedule", thumbnail=True)
        # A failed run rolled the session back: load them again.
        owner, organization = await db.get(User, owner_id), await db.get(Organization, organization_id)
        if result["status"] == "success" and subscribers:
            await _tell_subscribers(db, str(artifact_id), title, subscribers, organization)
        elif result["status"] == "failed":
            await _tell_owner(db, str(artifact_id), title, owner, organization, result["error"])


async def _tell_subscribers(db, artifact_id: str, title: str, subscribers: list, organization) -> None:
    """As a conversation's schedule does: an inbox row for user subscribers
    and the email, with a link to the dashboard."""
    from app.dependencies import _locale_from_org
    from app.services.inbox_service import inbox_service
    from app.services.notification_service import notification_service
    from app.settings.config import settings

    link = f"/d/{artifact_id}"
    try:
        user_ids = [str(s.get("id")) for s in subscribers if s.get("type") == "user" and s.get("id")]
        if user_ids:
            await inbox_service.notify_users(
                db, organization_id=str(organization.id), user_ids=user_ids, source="schedule",
                type="scheduled_run", title=f'"{title}" ran', body="Your scheduled report ran.", link=link,
                subject={"kind": "dashboard", "artifact_id": artifact_id,
                         "i18n": {"report": title, "variant": "short"}},
                group_key=f"schedule:dashboard:{artifact_id}",
            )
    except Exception:
        logger.warning("Dashboard %s: scheduled-run inbox notification failed", artifact_id, exc_info=True)
    try:
        await notification_service.send_scheduled_report_results(
            report_id=artifact_id, report_title=title, subscribers=subscribers,
            report_url=f"{settings.bow_config.base_url}{link}", locale=_locale_from_org(organization),
            organization_id=str(organization.id),  # the organization's own mail relay, not the default
        )
    except Exception:
        logger.warning("Dashboard %s: scheduled-run email failed", artifact_id, exc_info=True)


async def _tell_owner(db, artifact_id: str, title: str, owner: User, organization, error: str) -> None:
    from app.services import automation_alerts

    try:
        await automation_alerts.notify_owner_of_failure(
            db, kind="dashboard", automation_id=artifact_id, automation_name=title, owner=owner,
            organization=organization, outcome=automation_alerts.RunOutcome(ok=False, error_message=error),
            link=f"/d/{artifact_id}",
        )
    except Exception:
        logger.warning("Dashboard %s: failure alert not sent", artifact_id, exc_info=True)
