"""Publish a dashboard (phase 5, part B).

The owner publishes a version of the draft: the version open in the editor,
by default the newest that completed. The request checks what it can check
at once and returns; the run happens in the background:

1. `start_publish` (the request): the version completed; no other publish of
   this dashboard is running (dashfc04 holds at most one release in
   'publishing'; one that stopped beating is failed first); a public
   dashboard may not take on monitoring data; the publisher's connections to
   the conversation's sources open (no query runs). Then a release in
   'publishing' gets a copy of the version (dashboard_copy) and the request
   commits.
2. `run_publish` (background): every copied step runs again with the
   publisher's credentials, what a step loads first, beating the release's
   updated_at as it goes. A slide deck is frozen: nothing runs.
3. All or nothing. Every step succeeded: the release goes live in one switch
   (artifacts.published_release_id, with last_run_at) and the dashboard's
   other releases -- the previous live one, failed attempts -- are deleted
   with their copies, readers' results and files; its thumbnail is drawn
   again from it in the background (part C). One step failed: the release is 'failed' with
   the reason, its copies and files are deleted, and readers keep the live
   release as if nothing happened.

Readers' own results on the previous release go with it: they run again on
first open.
"""
import contextlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import lazyload, selectinload

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import (
    ArtifactRelease,
    artifact_release_data_source_association,
    artifact_release_file_association,
)
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization

logger = logging.getLogger(__name__)

# A publish that has not beaten for this long stopped (its server died);
# the next publish or status read fails it. It beats every
# PUBLISH_HEARTBEAT_SECONDS, also while one step runs: a step may run longer
# than the window (several slow warehouse queries).
PUBLISH_STALE_SECONDS = 300
PUBLISH_HEARTBEAT_SECONDS = 60

_ERROR_LIMIT = 2000


# ── what the owner sees ──────────────────────────────────────────────────

def _release_view(release: ArtifactRelease | None, version: ArtifactVersion | None = None) -> dict | None:
    if release is None:
        return None
    return {
        "id": str(release.id), "status": release.status, "error": release.error,
        "source_version_id": str(release.source_version_id) if release.source_version_id else None,
        "source_version_number": version.version if version is not None else None,
        "source_version_name": version.name if version is not None else None,
        "published_by": str(release.published_by) if release.published_by else None,
        "started_at": release.created_at, "published_at": release.published_at,
    }


async def _newest_completed_version(db, artifact: Artifact) -> ArtifactVersion | None:
    from app.services.dashboard_data import draft_version

    return await draft_version(db, artifact, light=True)


async def publish_status(db, artifact: Artifact) -> dict:
    """The live release, the latest attempt (which may be it), and whether
    the draft's newest completed version is not the live one's."""
    await reap_stale(db, artifact)
    live = None
    if artifact.published_release_id:
        live = await db.get(ArtifactRelease, str(artifact.published_release_id))
    latest = (await db.execute(
        select(ArtifactRelease).options(lazyload("*")).where(
            ArtifactRelease.artifact_id == str(artifact.id), ArtifactRelease.deleted_at.is_(None),
        ).order_by(ArtifactRelease.created_at.desc()).limit(1)
    )).scalars().first()
    newest = await _newest_completed_version(db, artifact)
    published_version = str(live.source_version_id) if live is not None and live.source_version_id else None

    async def source(release):
        if release is None or not release.source_version_id:
            return None
        return (await db.execute(select(ArtifactVersion).options(lazyload("*")).where(
            ArtifactVersion.id == str(release.source_version_id)))).scalars().first()

    shown = live if live is not None and live.status == "ready" else None
    return {
        "published": _release_view(shown, await source(shown)),
        "latest": _release_view(latest, await source(latest)),
        "has_unpublished_changes": newest is not None and str(newest.id) != published_version,
        "newest_version_id": str(newest.id) if newest is not None else None,
        "newest_version_number": newest.version if newest is not None else None,
        "last_run": {"at": artifact.last_run_at, "status": artifact.last_run_status,
                     "error": artifact.last_run_error},
    }


# ── starting ─────────────────────────────────────────────────────────────

async def _version_to_publish(db, artifact: Artifact, version_id: str | None) -> ArtifactVersion:
    if version_id is None:
        version = await _newest_completed_version(db, artifact)
    else:
        version = (await db.execute(
            select(ArtifactVersion).options(lazyload("*")).where(
                ArtifactVersion.id == str(version_id),
                ArtifactVersion.artifact_id == str(artifact.id),
                ArtifactVersion.deleted_at.is_(None),
            )
        )).scalars().first()
        if version is None:
            raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    if version is None or version.status != "completed":
        raise AppError.conflict(ErrorCode.DASHBOARD_VERSION_NOT_PUBLISHABLE,
                                "Only a version that finished successfully can be published")
    return version


def needed_sources(sources: list, codes: list) -> list:
    """The sources the dashboard's code uses: generated code addresses clients
    by "<data source name>:<connection>" keys, so those whose name it
    contains. Code that names none of them may use any: then all of them
    (as viewer_data_policy._org_fallback_ds_ids counts them)."""
    text = "\n".join(c for c in codes if c)
    named = [ds for ds in sources if ds.name and str(ds.name) in text]
    return named or list(sources)


async def _version_codes(db, version: ArtifactVersion) -> list:
    """The code of every step the version needs (what a copy would run)."""
    from app.services.dashboard_copy import collect_dashboard_deps

    deps = await collect_dashboard_deps(db, version)
    if not deps.all_query_ids:
        return []
    return [c for (c,) in (await db.execute(
        select(Step.code).join(Query, Query.default_step_id == Step.id).where(Query.id.in_(deps.all_query_ids))
    )).all()]


async def _check_connections(db, report: Report, version: ArtifactVersion, publisher: User) -> None:
    """The publisher's clients for the conversation's sources the dashboard
    uses build (credentials resolve, tokens refresh); no query runs. A
    broken connection the dashboard does not use does not block it. An
    "Auto" conversation has none of its own: its run reports what it cannot
    reach."""
    from app.models.data_source import DataSource
    from app.models.report_data_source_association import report_data_source_association as assoc
    from app.services.data_source_service import DataSourceService

    ids = select(assoc.c.data_source_id).where(assoc.c.report_id == str(report.id))
    # Model-default loaders: construct_clients reads connections eagerly.
    sources = (await db.execute(
        select(DataSource).where(DataSource.id.in_(ids), DataSource.deleted_at.is_(None))
    )).scalars().unique().all()
    sources = needed_sources(list(sources), await _version_codes(db, version)) if sources else []
    service = DataSourceService()
    broken: list[str] = []
    for ds in sources:
        if not DataSourceService.is_execution_live(ds):
            continue
        errors: list = []
        try:
            await service.construct_clients(db, ds, current_user=publisher, connection_errors=errors)
        except Exception as e:
            logger.info("Publish: no client for %s as %s: %s", ds.id, publisher.id, e)
            broken.append(ds.name)
            continue
        broken.extend(e.get("connection_name") or e.get("data_source_name") or ds.name for e in errors)
    if broken:
        names = ", ".join(dict.fromkeys(broken))
        raise AppError.bad_request(ErrorCode.DASHBOARD_PUBLISH_CONNECTION,
                                   "Connections that need attention: " + names, names=names)


async def start_publish(db, artifact: Artifact, publisher: User, organization, *,
                        version_id: str | None = None, force: bool = False) -> ArtifactRelease:
    """Checks, a release in 'publishing' with the version copied into it,
    committed. The caller spawns run_publish(release.id) after this returns.

    Collaboration (collab01): a version is publishable from any live
    conversation editing the dashboard; and unless `force`, a version that
    does not build on the live release (someone published since it started)
    is refused with who and when, so nobody overwrites another's work
    unknowingly."""
    from app.ee.audit.service import audit_service
    from app.services.bow_source_access import assert_read
    from app.services.dashboard_copy import copy_dashboard

    # The connections first, unlocked: building the clients may wait on a
    # warehouse's handshake or an OAuth refresh, which must not hold a
    # conversation delete or another Publish on the dashboard's row.
    report, version = await _publish_target(db, artifact, version_id)
    if not force:
        await _refuse_if_published_since(db, artifact, version)
    if artifact.mode != "slides":
        await _check_connections(db, report, version, publisher)
    # Then locked, so a conversation delete (dashboard_lifecycle.settle_
    # dashboards locks it too) cannot settle the dashboard while this starts;
    # what was checked is read again under the lock.
    artifact = (await db.execute(
        select(Artifact).where(Artifact.id == str(artifact.id)).with_for_update()
        .execution_options(populate_existing=True))).scalar_one()
    checked = str(version.id)
    report, version = await _publish_target(db, artifact, version_id)
    if artifact.mode != "slides" and str(version.id) != checked:
        await _check_connections(db, report, version, publisher)  # the draft moved meanwhile
    if artifact.mode != "slides":
        await _refuse_failed_charts(db, version)
    if not force:
        # Again under the lock: someone may have published while the
        # connections above were being checked (a handshake can take long).
        await _refuse_if_published_since(db, artifact, version)
    # What is published is the conversation's: the publisher must still read
    # its monitoring data, and a public dashboard may not show it.
    await assert_read(db, report.bow_source_access, publisher)
    if report.bow_source_access and artifact.visibility == "public":
        raise AppError.conflict(ErrorCode.DASHBOARD_PUBLISH_PUBLIC_MONITORING,
                                "A public dashboard cannot show monitoring data; change its sharing first")
    await reap_stale(db, artifact, commit=False)  # with the rest of this start, or not at all
    if await _publishing(db, artifact) is not None:
        raise AppError.conflict(ErrorCode.DASHBOARD_PUBLISH_IN_PROGRESS, "This dashboard is already being published")

    release = ArtifactRelease(artifact_id=str(artifact.id), organization_id=str(artifact.organization_id),
                              status="publishing", published_by=str(publisher.id))
    db.add(release)
    try:
        await db.flush()
    except IntegrityError:
        # Another server started one between the check and this insert.
        await db.rollback()
        raise AppError.conflict(ErrorCode.DASHBOARD_PUBLISH_IN_PROGRESS, "This dashboard is already being published")
    try:
        await copy_dashboard(db, version, release)
        await db.commit()
    except Exception:
        pptx = release.pptx_path  # a new object keeps its values through the rollback
        await db.rollback()
        _remove_files(str(release.id), pptx)
        raise
    with contextlib.suppress(Exception):  # the audit trail never fails the publish
        await audit_service.log(db=db, organization_id=str(organization.id), action="dashboard.publish_started",
                                user_id=str(publisher.id), resource_type="artifact", resource_id=str(artifact.id),
                                details={"release_id": str(release.id), "version_id": str(version.id)})
    return release


async def _refuse_failed_charts(db, version: ArtifactVersion) -> None:
    """409 naming the charts `version` shows whose step failed -- or has not
    finished yet (a joiner's copy still running again, a step the agent is
    still building) -- in its conversation: the copy takes successful steps
    only, so the release would go out without them, unseen. Failed ones are
    named first: those need fixing, the others only waiting. Deleted ones
    were removed on purpose; a version that lists no charts shows whatever
    succeeded, as before."""
    from app.models.query import Query
    from app.models.step import Step
    from app.models.visualization import Visualization

    listed = [str(v) for v in ((version.content or {}).get("visualization_ids") or [])]
    if not listed:
        return
    rows = (await db.execute(
        select(Visualization.id, Visualization.title, Query.title, Step.status)
        .join(Query, Query.id == Visualization.query_id).join(Step, Step.id == Query.default_step_id)
        .where(Visualization.id.in_(listed), Visualization.deleted_at.is_(None),
               Query.report_id == str(version.report_id), Query.deleted_at.is_(None),
               Step.status.in_(("error", "pending", "draft")))
    )).all()
    for statuses, code, message in (
        (("error",), ErrorCode.DASHBOARD_PUBLISH_CHARTS_FAILED, "Charts that failed in this conversation: "),
        (("pending", "draft"), ErrorCode.DASHBOARD_PUBLISH_CHARTS_RUNNING, "Charts still running: "),
    ):
        title = {str(viz_id): viz_title or query_title or ""
                 for viz_id, viz_title, query_title, status in rows if status in statuses}
        if title:
            names = ", ".join(dict.fromkeys(title[v] for v in listed if v in title))
            raise AppError.conflict(code, message + names, names=names)


async def _refuse_if_published_since(db, artifact: Artifact, version) -> None:
    """409 when someone published since `version`'s draft started (collab01)."""
    from app.services.dashboard_collab import published_since

    since = await published_since(db, artifact, version)
    if since is not None:
        raise AppError.conflict(
            ErrorCode.DASHBOARD_PUBLISHED_SINCE, "A version this one does not include is live",
            by=(since["by"] or {}).get("name"), by_id=(since["by"] or {}).get("id"),
            at=since["at"].isoformat() if since["at"] else None,
            live_version_id=since["live_version_id"], live_version=since["live_version"],
            version=getattr(version, "version", None))


async def _publish_target(db, artifact: Artifact, version_id: str | None):
    """The conversation and the version a Publish would copy -- refused when
    there is no draft to publish."""
    # A dashboard whose conversations were all deleted has no draft:
    # publishing an old version from one would roll the dashboard back. Edit
    # it first. Several conversations may edit it (collab01).
    from app.services.dashboard_collab import editing_report_ids

    editing = await editing_report_ids(db, artifact)
    if not editing:
        raise AppError.conflict(ErrorCode.DASHBOARD_EDIT_FIRST, "This dashboard has no draft to publish; edit it first")
    version = await _version_to_publish(db, artifact, version_id)
    if str(version.report_id) not in editing:
        # A version from a conversation the dashboard has left (Edit moved it).
        raise AppError.conflict(ErrorCode.DASHBOARD_VERSION_NOT_PUBLISHABLE,
                                "This version belongs to a deleted conversation")
    report = (await db.execute(
        select(Report).options(lazyload("*")).where(Report.id == str(version.report_id))
    )).scalar_one()
    return report, version


async def _publishing(db, artifact: Artifact) -> ArtifactRelease | None:
    return (await db.execute(
        select(ArtifactRelease).options(lazyload("*")).where(
            ArtifactRelease.artifact_id == str(artifact.id), ArtifactRelease.status == "publishing",
        )
    )).scalars().first()


async def reap_stale(db, artifact: Artifact, *, commit: bool = True) -> None:
    """A publish that stopped beating fails, and its copies go. `commit`
    False: part of the caller's transaction (starting a publish, deleting a
    conversation) -- flushed only, so a refusal later rolls it back and the
    caller's row locks hold; its files go once the caller commits."""
    cutoff = datetime.utcnow() - timedelta(seconds=PUBLISH_STALE_SECONDS)
    stale = (await db.execute(
        select(ArtifactRelease.id, ArtifactRelease.pptx_path).where(
            ArtifactRelease.artifact_id == str(artifact.id), ArtifactRelease.status == "publishing",
            ArtifactRelease.updated_at < cutoff,
        )
    )).all()
    for rid, pptx in stale:
        failed = await db.execute(
            update(ArtifactRelease).where(ArtifactRelease.id == rid, ArtifactRelease.status == "publishing",
                                          ArtifactRelease.updated_at < cutoff)
            .values(status="failed", error="Publishing stopped before it finished. Publish again.")
        )
        if failed.rowcount == 1:
            await delete_release_rows(db, str(rid), keep_release=True)
            if commit:
                await db.commit()
                _remove_files(str(rid), pptx)
            else:
                await db.flush()
                _after_commit(db, lambda rid=str(rid), pptx=pptx: _remove_files(rid, pptx))


def _after_commit(db, action) -> None:
    """Run `action` once the session's transaction commits -- never when it
    rolls back first (the files of rows that were not deleted stay)."""
    from sqlalchemy import event

    session = db.sync_session
    state = {"settled": False}

    def committed(_session):
        if not state["settled"]:
            state["settled"] = True
            action()

    def rolled_back(_session, _previous=None):
        state["settled"] = True

    event.listen(session, "after_commit", committed, once=True)
    event.listen(session, "after_soft_rollback", rolled_back, once=True)


# ── running ──────────────────────────────────────────────────────────────

_ABANDONED = object()


class RunFailure(str):
    """Why a run of the copies stopped: "<step or connection>: <reason>", as
    Publish and the dashboard have always shown it, with the two parts kept
    for the refresh history (refhist01)."""

    def __new__(cls, source: str, reason: str):
        failure = super().__new__(cls, f"{source}: {reason}"[:_ERROR_LIMIT])
        failure.source, failure.reason = source, reason
        return failure

    def __reduce__(self):
        # copy / pickle rebuild it from its two parts, not from the joined text.
        return (RunFailure, (self.source, self.reason))


def _reason(e: Exception) -> str:
    return str(getattr(e, "detail", None) or e)[:_ERROR_LIMIT] or e.__class__.__name__


async def run_publish(release_id: str) -> None:
    """The background half: run the copies, then go live or fail."""
    import asyncio

    from app.dependencies import async_session_maker

    beating = asyncio.create_task(_keep_beating(str(release_id)))
    try:
        async with async_session_maker() as db:
            outcome = await _run_copies(db, str(release_id))
    except Exception as e:
        logger.warning("Publish %s failed while running", release_id, exc_info=True)
        outcome = f"Publishing failed: {_reason(e)}"
    finally:
        beating.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beating
    async with async_session_maker() as db:
        if outcome is None:
            if await _go_live(db, str(release_id)):
                return
            outcome = _ABANDONED
        await _fail(db, str(release_id), None if outcome is _ABANDONED else outcome)


async def _keep_beating(release_id: str) -> None:
    """Beat on a session of its own every PUBLISH_HEARTBEAT_SECONDS until
    cancelled: the run's own session is busy inside a step."""
    import asyncio

    from app.dependencies import async_session_maker

    while True:
        await asyncio.sleep(PUBLISH_HEARTBEAT_SECONDS)
        try:
            async with async_session_maker() as db:
                if not await _beat(db, release_id):
                    return
        except Exception:
            logger.debug("Publish %s: heartbeat failed", release_id, exc_info=True)


async def _beat(db, release_id: str) -> bool:
    """Still publishing (and now marked alive)? False once it was failed."""
    alive = await db.execute(
        update(ArtifactRelease).where(ArtifactRelease.id == release_id, ArtifactRelease.status == "publishing")
        .values(updated_at=datetime.utcnow())
    )
    await db.commit()
    return alive.rowcount == 1


async def _run_copies(db, release_id: str):
    """None when every copy ran, else the reason (or _ABANDONED). Each step
    commits as it goes: nothing reads a release before it goes live, and a
    failure deletes every copy anyway."""
    from app.models.organization import Organization

    release = await db.get(ArtifactRelease, release_id)
    if release is None or release.status != "publishing":
        return _ABANDONED
    artifact = await db.get(Artifact, str(release.artifact_id))
    if artifact is None:
        return _ABANDONED
    if artifact.mode == "slides":
        return None  # a published deck is frozen
    publisher = await db.get(User, str(release.published_by)) if release.published_by else None
    organization = await db.get(Organization, str(release.organization_id))
    if publisher is None or organization is None:
        return "The publisher is no longer available"

    async def beat() -> bool:
        return await _beat(db, release_id)

    async def write(step, data, params) -> None:
        step.data = data
        step.applied_params = dict(params) if params else None
        step.status = "success"
        await db.commit()

    return await run_release_copies(db, release, publisher, organization, before_step=beat, after_step=write)


async def run_release_copies(db, release: ArtifactRelease, run_user: User, organization, *,
                             after_step, before_step=None):
    """Run every copied default step of `release` with `run_user`'s
    credentials, what a step loads first: Publish's run, and a refresh's
    (phase 6). `after_step(step, data, params)` is handed each result --
    Publish writes it at once, a refresh keeps it until every step ran.
    `before_step()` returning False stops the run (_ABANDONED). None when
    every step ran, else the reason; a step that fails rolls the session
    back."""
    from app.ai.tools.implementations.agent_focus_common import accessible_agents
    from app.services.dashboard_runs import in_dependency_order, release_data_sources, release_files
    from app.services.data_source_service import DataSourceService
    from app.services.exec_context import ExecContext
    from app.services.step_service import StepService

    release_id = str(release.id)
    org_settings = await organization.get_settings(db)

    steps = {str(s.id): s for s in (await db.execute(
        select(Step).options(lazyload("*"), selectinload(Step.query).options(lazyload("*")))
        .join(Query, Query.default_step_id == Step.id)
        .where(Query.artifact_release_id == release_id)
        .order_by(Query.created_at, Query.id)
    )).scalars().all()}
    sources = await release_data_sources(db, release)
    own_sources = bool(sources)
    if not own_sources:
        sources = await accessible_agents(db, organization, run_user)
    sources = [ds for ds in sources if DataSourceService.is_execution_live(ds)]
    # A broken source the code does not use fails nothing (as the check
    # before the copy); it gets no client either.
    needed = {str(ds.id) for ds in needed_sources(sources, [s.code for s in steps.values()])}
    ds_service = DataSourceService()
    clients: dict = {}
    for ds in sources:
        errors: list = []
        try:
            clients.update(await ds_service.construct_clients(db, ds, current_user=run_user,
                                                              connection_errors=errors))
        except Exception as e:
            if own_sources and str(ds.id) in needed:
                return RunFailure(ds.name, _reason(e))
            continue
        if errors and own_sources and str(ds.id) in needed:
            first = errors[0]
            return RunFailure(first.get('connection_name') or ds.name, str(first.get('error')))

    ctx = ExecContext.from_release(release, data_sources=sources, files=await release_files(db, release),
                                   credential_user=run_user, reader=run_user, organization=organization,
                                   organization_settings=org_settings)
    ctx.fresh_rows = {}  # the new rows of each step another one loads
    loaded = await _loaded_step_ids(db, release, [s.code for s in steps.values()])
    order = await in_dependency_order(db, release, [(sid, s.code) for sid, s in steps.items()])
    service = StepService()
    for step_id, code in order:
        if before_step is not None and not await before_step():
            return _ABANDONED
        if not (code or "").strip():
            continue  # nothing to run: the copied rows stay
        step = steps[step_id]
        label = step.title or step_id  # read before a rollback expires it
        try:
            specs = service._step_param_specs(step)
            params = await service._resolve_step_params(
                db, step, stored=step.applied_params, run_user=run_user,
                organization_id=str(release.organization_id), specs=specs)
            data = await service._execute_step_code(db, step, ctx, db_clients=clients, params=params,
                                                    param_specs=specs)
        except Exception as e:
            await db.rollback()
            return RunFailure(label, _reason(e))
        if step_id in loaded:
            ctx.fresh_rows[step_id] = data
        await after_step(step, data, params)
    return None


async def _loaded_step_ids(db, release: ArtifactRelease, codes: list) -> set:
    """The release's steps some copy loads (load_step, resolved as the
    release resolves it)."""
    from app.ai.code_execution.loadables import extract_loadable_refs
    from app.services.identity_taint import release_step_lookup

    lookup = await release_step_lookup(db, str(release.id))
    loaded = set()
    for code in codes:
        for ref in extract_loadable_refs(code or "")[0]:
            step = lookup(ref)
            if step is not None:
                loaded.add(str(step.id))
    return loaded


async def _go_live(db, release_id: str) -> bool:
    """The switch: this release live, every other one of the dashboard gone,
    in one transaction under the dashboard's row lock -- so a publish that
    goes live right after never deletes this one, nor this one it. False
    when it is no longer publishing (it was failed meanwhile) or may not go
    live (failed here)."""
    from app.ee.audit.service import audit_service

    release = await db.get(ArtifactRelease, release_id)
    if release is None:
        return False
    artifact_id, organization_id = str(release.artifact_id), str(release.organization_id)
    published_by = str(release.published_by) if release.published_by else None
    version_id = str(release.source_version_id or "")
    artifact = (await db.execute(
        select(Artifact).options(lazyload("*")).where(Artifact.id == artifact_id).with_for_update()
    )).scalar_one_or_none()
    if artifact is None or artifact.deleted_at is not None:
        await db.rollback()
        await _fail(db, release_id, "The dashboard was deleted while it was being published.")
        return True
    if release.bow_source_access and artifact.visibility == "public":
        # Made public while this publish ran: monitoring data may not go live there.
        await db.rollback()
        await _fail(db, release_id, "A public dashboard cannot show monitoring data; change its sharing and "
                                    "publish again.")
        return True

    async def switch() -> bool:
        switched = await db.execute(
            update(ArtifactRelease).where(ArtifactRelease.id == release_id, ArtifactRelease.status == "publishing")
            .values(status="ready", error=None, published_at=datetime.utcnow(),
                    run_by=ArtifactRelease.published_by)  # the publisher's run (collab01)
        )
        if switched.rowcount != 1:
            return False
        await db.execute(update(Artifact).where(Artifact.id == artifact_id)
                         .values(published_release_id=release_id, last_run_at=datetime.utcnow(),
                                 last_run_status="success", last_run_error=None))
        return True

    if not await switch():
        await db.rollback()
        return False
    # The last KEEP_RELEASES ready releases stay, for Roll back (collab01);
    # older ones and failed attempts go.
    from app.services.dashboard_collab import KEEP_RELEASES

    kept = [rid for (rid,) in (await db.execute(
        select(ArtifactRelease.id).where(
            ArtifactRelease.artifact_id == artifact_id, ArtifactRelease.id != release_id,
            ArtifactRelease.status == "ready", ArtifactRelease.deleted_at.is_(None),
        ).order_by(ArtifactRelease.published_at.desc(), ArtifactRelease.created_at.desc())
        .limit(KEEP_RELEASES - 1)
    )).all()]
    others = (await db.execute(
        select(ArtifactRelease.id, ArtifactRelease.pptx_path).where(
            ArtifactRelease.artifact_id == artifact_id, ArtifactRelease.id != release_id,
            ArtifactRelease.status != "publishing", ArtifactRelease.id.notin_(kept or [""]),
        )
    )).all()
    try:
        for rid, _ in others:
            await delete_release_rows(db, str(rid))
        await db.commit()
    except Exception:
        # The release still goes live; the old ones wait for the next publish.
        logger.exception("Publish %s: the dashboard's other releases were not deleted", release_id)
        await db.rollback()
        others = []
        artifact = (await db.execute(
            select(Artifact).options(lazyload("*")).where(Artifact.id == artifact_id).with_for_update()
        )).scalar_one_or_none()
        if artifact is None or not await switch():
            await db.rollback()
            return False
        await db.commit()
    for rid, pptx in others:
        _remove_files(str(rid), pptx)
    # A named draft that went live is promoted (collab01); the implicit
    # draft simply goes on.
    with contextlib.suppress(Exception):
        await _mark_promoted(db, release_id, version_id)

    from app.core import fire_and_forget
    from app.services.thumbnail_service import ThumbnailService
    fire_and_forget.spawn(ThumbnailService().regenerate_for_release(release_id))
    with contextlib.suppress(Exception):
        await audit_service.log(db=db, organization_id=organization_id, action="dashboard.published",
                                user_id=published_by, resource_type="artifact", resource_id=artifact_id,
                                details={"release_id": release_id, "version_id": version_id})
    return True


async def _mark_promoted(db, release_id: str, version_id: str) -> None:
    from app.models.artifact_release import ArtifactDraft

    if not version_id:
        return
    draft_id = (await db.execute(
        select(ArtifactVersion.draft_id).where(ArtifactVersion.id == version_id))).scalar()
    if draft_id:
        await db.execute(update(ArtifactDraft).where(ArtifactDraft.id == str(draft_id))
                         .values(status="promoted", promoted_release_id=release_id))
        await db.commit()


async def _fail(db, release_id: str, error: str | None) -> None:
    """Failed with its reason (unless already failed); its copies and files go."""
    from app.ee.audit.service import audit_service

    release = await db.get(ArtifactRelease, release_id)
    if release is None:
        return
    pptx = release.pptx_path
    if error is not None:
        await db.execute(
            update(ArtifactRelease).where(ArtifactRelease.id == release_id, ArtifactRelease.status == "publishing")
            .values(status="failed", error=error[:_ERROR_LIMIT])
        )
    await db.refresh(release)
    if release.status == "ready":
        return  # live after all (a second run of it): never delete a live release
    await delete_release_rows(db, release_id, keep_release=True)
    await db.commit()
    _remove_files(release_id, pptx)
    if error is not None:
        with contextlib.suppress(Exception):
            await audit_service.log(db=db, organization_id=str(release.organization_id),
                                    action="dashboard.publish_failed",
                                    user_id=str(release.published_by) if release.published_by else None,
                                    resource_type="artifact", resource_id=str(release.artifact_id),
                                    details={"release_id": release_id, "error": error[:500]})


# ── deleting a release ───────────────────────────────────────────────────

async def delete_release_rows(db, release_id: str, *, keep_release: bool = False) -> None:
    """A release's copies, readers' results on them and links, deleted in
    the order the foreign keys need (no ON DELETE on the copies' columns;
    SQLite does not enforce them, Postgres does). `keep_release` keeps the
    release row itself -- a failed attempt's record. Not committed; files
    are the caller's (after the commit)."""
    from app.models.completion import Completion
    from app.models.entity import Entity
    from app.models.step_user_result import StepUserResult
    from app.models.table_feedback_event import TableFeedbackEvent
    from app.models.table_usage_event import TableUsageEvent
    from app.models.tool_execution import ToolExecution

    query_ids = select(Query.id).where(Query.artifact_release_id == release_id).scalar_subquery()
    step_ids = [str(s) for (s,) in (await db.execute(
        select(Step.id).where(Step.query_id.in_(query_ids))
    )).all()]
    await db.execute(update(Query).where(Query.artifact_release_id == release_id).values(default_step_id=None))
    if step_ids:
        # Nothing should point at a copy; if something does, it lets go.
        await db.execute(update(Entity).where(Entity.source_step_id.in_(step_ids)).values(source_step_id=None))
        await db.execute(update(Completion).where(Completion.step_id.in_(step_ids)).values(step_id=None))
        await db.execute(update(ToolExecution).where(ToolExecution.created_step_id.in_(step_ids))
                         .values(created_step_id=None))
        await db.execute(delete(TableUsageEvent).where(TableUsageEvent.step_id.in_(step_ids)))
        await db.execute(delete(TableFeedbackEvent).where(TableFeedbackEvent.step_id.in_(step_ids)))
        await db.execute(delete(StepUserResult).where(StepUserResult.step_id.in_(step_ids)))
        await db.execute(delete(Step).where(Step.id.in_(step_ids)))
    await db.execute(delete(Visualization).where(
        (Visualization.artifact_release_id == release_id) | Visualization.query_id.in_(query_ids)))
    await db.execute(delete(Query).where(Query.artifact_release_id == release_id))
    await db.execute(delete(artifact_release_data_source_association).where(
        artifact_release_data_source_association.c.artifact_release_id == release_id))
    await db.execute(delete(artifact_release_file_association).where(
        artifact_release_file_association.c.artifact_release_id == release_id))
    if not keep_release:
        await db.execute(update(Artifact).where(Artifact.published_release_id == release_id)
                         .values(published_release_id=None))
        await db.execute(delete(ArtifactRelease).where(ArtifactRelease.id == release_id))


def _remove_files(release_id: str, pptx_path: str | None) -> None:
    from app.services.dashboard_copy import remove_release_files
    try:
        remove_release_files(release_id, pptx_path)
    except Exception:
        logger.warning("Publish: files of release %s not removed", release_id, exc_info=True)
