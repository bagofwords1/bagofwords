"""A published dashboard refreshes (phase 6).

Three triggers, one run (`refresh`):

- the dashboard's schedule (`scheduled_refresh`, an APScheduler job per
  dashboard holding only its id): run as the dashboard's owner as they are
  at run time, while still in the organization; subscribers are told on
  success (inbox and email, a link to /d), the owner on failure. The
  schedule refreshes what is seen (phase 8): a dashboard not published yet
  refreshes its draft with the conversation's own rerun, as a conversation's
  schedule does today (its queries only; the link is the draft's);
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


async def _scheduled_dashboards() -> list:
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Artifact.id, Artifact.organization_id, Artifact.cron_schedule).where(
                Artifact.cron_schedule.isnot(None), Artifact.cron_schedule != "",
                Artifact.deleted_at.is_(None),
                Artifact.mode != "slides",  # a deck does not refresh
            )
        )).all()
        zones = {org: await org_timezone(db, org) for org in {str(r[1]) for r in rows}}
    return [(str(a), cron, zones.get(str(o))) for a, o, cron in rows]


async def restore_missing_jobs() -> None:
    """On the leader, every few minutes: puts back a scheduled dashboard's
    job that is missing -- a server still on an older version, during a
    rolling upgrade, deletes jobs it cannot load (APScheduler drops what it
    cannot restore). Jobs that are there are left as they are, so a fire
    that is due is not moved."""
    for artifact_id, cron, zone in await _scheduled_dashboards():
        if scheduler.get_job(job_id(artifact_id)) is None:
            try:
                register_job(artifact_id, cron, zone)
                logger.info("Dashboard %s: missing schedule job restored", artifact_id)
            except Exception:
                logger.warning("Dashboard %s: schedule %r not restored", artifact_id, cron, exc_info=True)


async def keep_jobs(interval_seconds: int = 300) -> None:
    """The leader's loop around restore_missing_jobs (an asyncio task, not a
    stored job: a server that cannot load it would drop that too)."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await restore_missing_jobs()
        except Exception:
            logger.warning("Dashboard schedules were not checked", exc_info=True)


async def register_all_jobs() -> None:
    """At startup, on the leader: one job per scheduled dashboard, published
    or not, and no other dashboard job. The same on every call."""
    rows = await _scheduled_dashboards()
    wanted = set()
    for artifact_id, cron, zone in rows:
        try:
            register_job(artifact_id, cron, zone)
            wanted.add(job_id(artifact_id))
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
        # as main's rerun_step drops them on a conversation refresh.
        await db.execute(delete(StepUserResult).where(StepUserResult.step_id.in_(step_ids)))
    now = datetime.utcnow()
    live.last_run_at, live.last_run_status, live.last_run_error = now, "success", None
    # The stored rows are this runner's now (collab01): only they read them
    # unconditionally; anyone else gets them only where the policy allows.
    release.run_by = str(run_user.id)
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


async def refresh_identity(db, artifact: Artifact) -> User | None:
    """Whose credentials refresh the published dashboard (collab01): the
    last publisher -- Publish checked their access to everything it runs on,
    and another editor may have added a source the owner cannot use -- while
    they can still edit it; else the owner while still a member; else no one."""
    from app.services.dashboard_access import can_edit, counts_as_owner, published_release

    if (artifact.shared_run_identity or "viewer") == "creator":
        # Readers run with the owner's credentials: so do refreshes, or the
        # rows they are shown would be the last publisher's.
        owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
        return owner if owner is not None and await counts_as_owner(db, artifact, owner) else None
    release = await published_release(db, artifact)
    if release is not None and release.published_by:
        publisher = await db.get(User, str(release.published_by))
        if publisher is not None and await can_edit(db, artifact, publisher):
            return publisher
    owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
    if owner is not None and await counts_as_owner(db, artifact, owner):
        return owner
    return None


async def refresh_as_identity(artifact_id: str) -> None:
    """A refresh in its own session, as whoever refreshes the dashboard now
    (refresh_identity): after "run on my behalf" is turned on over rows that
    are not the owner's, so readers get the owner's rows without waiting for
    the next refresh."""
    from app.dependencies import async_session_maker
    from app.models.organization import Organization

    try:
        async with async_session_maker() as db:
            artifact = await db.get(Artifact, str(artifact_id))
            if artifact is None or artifact.deleted_at is not None:
                return
            runner = await refresh_identity(db, artifact)
            organization = await db.get(Organization, str(artifact.organization_id))
            if runner is None or organization is None:
                return
            await refresh(db, artifact, runner, organization, trigger="run_identity", thumbnail=True)
    except Exception:
        logger.warning("Dashboard %s: refresh after the run identity changed failed", artifact_id, exc_info=True)


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
    owner = await refresh_identity(db, artifact)
    if owner is None:
        return skipped("owner")
    organization = await db.get(Organization, str(artifact.organization_id))
    result = await refresh(db, artifact, owner, organization, trigger="view", thumbnail=False)
    return {"skipped": result["status"] == "skipped", "reason": result["reason"], "status": result["status"],
            "last_run_at": result["last_run_at"]}


# ── scheduled ────────────────────────────────────────────────────────────

async def scheduled_refresh(artifact_id: str) -> None:
    """The dashboard's job. One worker runs a fire; a dashboard that is gone
    or unscheduled drops its job. Not published yet: its draft refreshes."""
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.services.dashboard_access import counts_as_owner

    if not await asyncio.to_thread(claim_scheduled_run, job_id(artifact_id)):
        return
    async with async_session_maker() as db:
        artifact = await db.get(Artifact, str(artifact_id))
        if artifact is None or artifact.deleted_at is not None or not artifact.cron_schedule:
            remove_job(artifact_id)
            return
        organization = await db.get(Organization, str(artifact.organization_id))
        # The last publisher while they can still edit it, else the owner
        # (collab01); the owner always hears of a failure too.
        owner = await refresh_identity(db, artifact) if artifact.published_release_id else (
            await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None)
        if owner is None or organization is None or (
                not artifact.published_release_id and not await counts_as_owner(db, artifact, owner)):
            await _record_failure(db, str(artifact_id), None,
                                  "The dashboard's owner is no longer a member of the organization")
            return
        title, subscribers = await _schedule_title(db, artifact), artifact.notification_subscribers
        owner_id, organization_id = str(owner.id), str(organization.id)
        if not artifact.published_release_id:
            await _refresh_draft(db, artifact, owner, organization, title, subscribers)
            return
        result = await refresh(db, artifact, owner, organization, trigger="schedule", thumbnail=True)
        # A failed run rolled the session back: load them again.
        owner, organization = await db.get(User, owner_id), await db.get(Organization, organization_id)
        if result["status"] == "success" and subscribers:
            await _tell_subscribers(db, str(artifact_id), title, subscribers, organization)
        elif result["status"] == "failed":
            await _tell_failure(db, str(artifact_id), title, owner, organization, subscribers, result["error"])


async def _refresh_draft(db, artifact: Artifact, owner: User, organization, title: str, subscribers) -> None:
    """A dashboard not published yet: the conversation's rerun of this
    dashboard's draft, as a conversation's schedule runs today. Told as the
    published dashboard's schedule tells: subscribers when something ran,
    the owner when nothing did -- with the draft's link."""
    from app.models.report import Report
    from app.services.dashboard_data import draft_version
    from app.services.report_service import ReportService

    artifact_id, link = str(artifact.id), f"/d/{artifact.id}/draft"
    report = await db.get(Report, str(artifact.report_id)) if artifact.report_id else None
    if report is not None and await _moved_from_its_conversation(db, artifact, report):
        # The schedule the data migration moved here (its link shows a deck):
        # the conversation's old link, as main's emails had -- it leads to
        # what it showed; a draft's link opens for its editors only.
        link = f"/r/{report.id}"
    version = await draft_version(db, artifact)
    if version is not None:
        # The conversation the newest draft was made in refreshes, with ITS
        # user's credentials (collab01) -- never one person's rows in
        # another's conversation. Asked of the conversation's user every
        # time: which conversation `artifact.report_id` points at says
        # nothing about whose it is (a delete moves it to another editor's).
        from app.services.dashboard_access import can_edit

        report = await db.get(Report, str(version.report_id))
        runner = await db.get(User, str(report.user_id)) if report is not None else None
        if runner is None or not await can_edit(db, artifact, runner):
            return
        owner = runner
    if report is None or report.deleted_at is not None or report.status == "archived" or version is None:
        return  # nothing to refresh: no live conversation, or no finished version
    try:
        run = await ReportService().rerun_report_steps(db, str(report.id), owner, organization,
                                                       artifact_id=str(version.id), notify_subscribers=False)
    except Exception as e:
        logger.warning("Dashboard %s: draft refresh failed", artifact_id, exc_info=True)
        owner_id, organization_id = str(owner.id), str(organization.id)
        await db.rollback()
        # The rollback expired them: load them again (as the published path does).
        from app.models.organization import Organization

        owner, organization = await db.get(User, owner_id), await db.get(Organization, organization_id)
        run = {"steps_total": 1, "steps_succeeded": 0, "error": str(e)}
    if run.get("steps_succeeded"):
        if subscribers:
            await _tell_subscribers(db, artifact_id, title, subscribers, organization, link=link)
    elif run.get("steps_total"):
        await _tell_failure(db, artifact_id, title, owner, organization, subscribers,
                            run.get("error") or run.get("message") or "The draft's queries failed", link=link)


async def _schedule_title(db, artifact: Artifact) -> str:
    """The name the schedule's emails carry: a migrated dashboard's -- the one
    its old link leads to, or the one its conversation's schedule moved to --
    is its conversation's, as main named them and as the dashboards list and
    the project's automations still do (2026-10-21); any other its own."""
    from app.models.report import Report

    report = await db.get(Report, str(artifact.report_id)) if artifact.report_id else None
    if report is not None and report.title and (
            str(artifact.legacy_report_id or "") == str(report.id)
            or await _moved_from_its_conversation(db, artifact, report)):
        return report.title
    return artifact.title or "Untitled"


async def _moved_from_its_conversation(db, artifact: Artifact, report) -> bool:
    """This dashboard's schedule is the one the data migration moved from its
    conversation: a dashboard took the conversation's link over, and the
    schedule and subscribers are still those the conversation kept (its
    columns are the migration's backup, never written since)."""
    from app.services.dashboard_legacy import is_link_taken_over

    return ((report.cron_schedule or "").strip() == (artifact.cron_schedule or "").strip() != ""
            and (report.notification_subscribers or None) == (artifact.notification_subscribers or None)
            and await is_link_taken_over(db, report.id))


async def _tell_subscribers(db, artifact_id: str, title: str, subscribers: list, organization,
                            *, link: str | None = None) -> None:
    """As a conversation's schedule does: an inbox row for user subscribers
    and the email, with a link to the dashboard (its draft, before it is
    published)."""
    from app.dependencies import _locale_from_org
    from app.services.inbox_service import inbox_service
    from app.services.notification_service import notification_service
    from app.settings.config import settings

    link = link or f"/d/{artifact_id}"
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


async def _tell_failure(db, artifact_id: str, title: str, owner: User, organization, subscribers,
                        error: str, *, link: str | None = None) -> None:
    """A scheduled run that failed (decided 2026-10-21): emailed to the
    schedule's subscribers only -- the same list a run that worked emails,
    so nobody who did not ask gets mail; none for the owner, as main's
    conversation schedule sent none. They are told it failed, not why (they
    may be outside the organization); the owner's Notifications row holds
    the error."""
    from app.dependencies import _locale_from_org
    from app.models.user import User as UserModel
    from app.services import automation_alerts
    from app.services.notification_service import notification_service
    from app.settings.config import settings

    link = link or f"/d/{artifact_id}"
    # The run's user (the last publisher) and the dashboard's owner both get
    # the Notifications row (collab01).
    alerted = [owner]
    try:
        dashboard = await db.get(Artifact, str(artifact_id))
        if dashboard is not None and dashboard.owner_id and str(dashboard.owner_id) != str(owner.id):
            the_owner = await db.get(UserModel, str(dashboard.owner_id))
            if the_owner is not None:
                alerted.append(the_owner)
    except Exception:
        pass
    for person in alerted:
        try:
            await automation_alerts.notify_owner_of_failure(
                db, kind="dashboard", automation_id=artifact_id, automation_name=title, owner=person,
                organization=organization, outcome=automation_alerts.RunOutcome(ok=False, error_message=error),
                link=link, email=False,
            )
        except Exception:
            logger.warning("Dashboard %s: failure alert not sent", artifact_id, exc_info=True)
    addresses = []
    for sub in subscribers or []:
        if sub.get("type") == "email" and sub.get("address"):
            addresses.append(sub["address"])
        elif sub.get("type") == "user" and sub.get("id"):
            user = await db.get(UserModel, str(sub["id"]))
            if user is not None and user.email:
                addresses.append(user.email)
    for address in dict.fromkeys(addresses):
        try:
            await notification_service.send_automation_failure(
                recipient_email=address, automation_name=title, error_message="",
                link=f"{settings.bow_config.base_url.rstrip('/')}{link}",
                organization_id=str(organization.id), locale=_locale_from_org(organization),
            )
        except Exception:
            logger.warning("Dashboard %s: failure email not sent", artifact_id, exc_info=True)
