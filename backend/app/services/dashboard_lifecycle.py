"""A published dashboard outlives its conversation (phase 7).

Deleting a conversation archives it, as it always has; nothing is removed
from the database. What changes is what happens to its dashboards, in the
same transaction (`settle_dashboards`):

- published (a live release): it leaves the conversation
  (artifacts.report_id is cleared) and stays exactly as readers know it --
  link, sharing, schedule, chat. Unpublished changes stay behind in the
  archived conversation; "Edit" starts a new conversation from the release;
- never published: it is deleted with the conversation (soft, deleted_at).

`delete_outcomes` tells beforehand which of the three each dashboard is
(survives / loses_changes / deleted). A conversation one of whose dashboards
is publishing is not deleted. A deleted project's dashboards leave it
(`leave_project`).
"""
import logging
from datetime import datetime

from sqlalchemy import select, update

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactRelease

logger = logging.getLogger(__name__)


async def _dashboards(db, report_ids: list[str], *, lock: bool = False) -> list[Artifact]:
    """The dashboards these conversations edit, as their own conversation or
    joined (collab01). `lock` serializes with start_publish, which locks the
    dashboard too."""
    from app.services.dashboard_collab import dashboards_of_conversations

    return await dashboards_of_conversations(db, report_ids, lock=lock)


async def _live_release(db, artifact: Artifact) -> ArtifactRelease | None:
    if not artifact.published_release_id:
        return None
    release = await db.get(ArtifactRelease, str(artifact.published_release_id))
    return release if release is not None and release.status == "ready" and release.deleted_at is None else None


async def _newest_completed_version_id(db, artifact: Artifact) -> str | None:
    from app.services.dashboard_data import draft_version

    found = await draft_version(db, artifact, light=True)
    return str(found.id) if found else None


async def delete_outcomes(db, report_ids: list[str]) -> list[dict]:
    """For each dashboard of these conversations: what deleting them does to
    it -- survives, loses_changes (published, with unpublished edits) or
    deleted (never published)."""
    from app.services.dashboard_list import not_an_older_legacy_version

    dashboards = await _dashboards(db, report_ids)
    # An old version artver01 made a dashboard of its own is not one about to
    # go: it is counted as the dashboards list counts (decision 4).
    counted = {str(i) for (i,) in (await db.execute(
        select(Artifact.id).where(Artifact.id.in_([str(a.id) for a in dashboards]), not_an_older_legacy_version())
    )).all()} if dashboards else set()
    from app.services.dashboard_collab import editing_report_ids

    out = []
    gone = {str(r) for r in report_ids}
    for artifact in dashboards:
        if str(artifact.id) not in counted:
            continue
        if await editing_report_ids(db, artifact, exclude=gone):
            # Other conversations keep editing it (collab01): nothing happens to it.
            out.append({"id": str(artifact.id), "title": artifact.title, "mode": artifact.mode,
                        "outcome": "others_editing"})
            continue
        release = await _live_release(db, artifact)
        if release is None:
            # The owner's (or an ownerless) dashboard goes with the owner's
            # last conversation; an editor's never takes it.
            outcome = "deleted" if await _owner_among(db, artifact, gone) else "kept_for_owner"
        elif await _newest_completed_version_id(db, artifact) != (
                str(release.source_version_id) if release.source_version_id else None):
            outcome = "loses_changes"
        else:
            outcome = "survives"
        out.append({"id": str(artifact.id), "title": artifact.title, "mode": artifact.mode, "outcome": outcome})
    return out


async def _owner_among(db, artifact: Artifact, report_ids) -> bool:
    """Whether the owner's own conversation is among these -- or the
    dashboard has no owner on record (as before collab01)."""
    from app.models.report import Report

    if not artifact.owner_id:
        return True
    return (await db.execute(select(Report.id).where(
        Report.id.in_([str(r) for r in report_ids]), Report.user_id == str(artifact.owner_id)).limit(1)
    )).scalar_one_or_none() is not None


async def settle_dashboards(db, report_ids: list[str]) -> None:
    """The archived conversations' dashboards: published ones leave them,
    the others are deleted with the owner's last conversation (an editor's
    leaves them to the owner). 409 -- and the caller's archive rolls back --
    when one of them is publishing (after failing a publish that stopped
    beating): what would become of it if that publish then failed is not
    something to guess. Each dashboard is locked, as start_publish locks it,
    so the two never interleave. Not committed: part of the caller's
    archive."""
    from app.services.dashboard_collab import end_conversations
    from app.services.dashboard_publish import _publishing, reap_stale

    now = datetime.utcnow()
    for artifact in await _dashboards(db, report_ids, lock=True):
        # These conversations stop editing it; when others still do, it stays
        # as it is -- in their hands (collab01).
        if await end_conversations(db, artifact, [str(r) for r in report_ids]):
            continue
        await reap_stale(db, artifact, commit=False)  # with the caller's archive, or not at all
        if await _publishing(db, artifact) is not None:
            raise AppError.conflict(ErrorCode.DASHBOARD_PUBLISH_IN_PROGRESS,
                                    f'"{artifact.title or "A dashboard"}" is being published; try again when it '
                                    "finishes")
        if await _live_release(db, artifact) is not None:
            await db.execute(update(Artifact).where(Artifact.id == str(artifact.id)).values(report_id=None))
        elif await _owner_among(db, artifact, report_ids):
            await db.execute(update(Artifact).where(Artifact.id == str(artifact.id)).values(deleted_at=now))
        else:
            # Never published, and an editor deleted the last conversation
            # editing it: deleting a dashboard is its owner's call. It stays
            # theirs, with no draft; Edit goes on from its newest version.
            await db.execute(update(Artifact).where(Artifact.id == str(artifact.id)).values(report_id=None))


# ── from a published release to a new conversation (part B) ──────────────

async def _release_queries(db, release_id: str) -> list:
    from sqlalchemy.orm import lazyload, selectinload

    from app.models.query import Query

    return list((await db.execute(
        select(Query).options(
            lazyload("*"), selectinload(Query.default_step).options(lazyload("*")),
            selectinload(Query.visualizations).options(lazyload("*")),
        ).where(Query.artifact_release_id == release_id).order_by(Query.created_at, Query.id)
    )).scalars().unique().all())


async def release_to_conversation(db, artifact: Artifact, release: ArtifactRelease, *, user, title: str,
                                  project_id: str | None, slug_prefix: str, step_overrides=None,
                                  copy_source_refs: bool = True, link_files: bool = True, into=None):
    """A new conversation of `user` holding a copy of the release: its
    queries, their default steps and charts (references re-pointed by the
    copy engine), its sources, theme and monitoring access, and -- with
    `link_files` -- its files (the owner's Edit only: a file linked to a
    conversation is readable by whoever can view it, and a Fork, like the
    conversation's Fork, links none). Returns (report, copy maps). Not
    committed."""
    from app.models.artifact_release import (
        artifact_release_data_source_association as release_sources,
    )
    from app.models.artifact_release import (
        artifact_release_file_association as release_files,
    )

    ds_ids = [str(i) for (i,) in (await db.execute(select(release_sources.c.data_source_id).where(
        release_sources.c.artifact_release_id == str(release.id)))).all()]
    file_ids = [str(i) for (i,) in (await db.execute(select(release_files.c.file_id).where(
        release_files.c.artifact_release_id == str(release.id)))).all()] if link_files else []
    return await rows_to_conversation(
        db, artifact, queries=await _release_queries(db, str(release.id)), data_source_ids=ds_ids,
        file_ids=file_ids, theme_name=release.theme_name, theme_overrides=release.theme_overrides,
        bow_source_access=release.bow_source_access, user=user, title=title, project_id=project_id,
        slug_prefix=slug_prefix, step_overrides=step_overrides, copy_source_refs=copy_source_refs, into=into)


async def rows_to_conversation(db, artifact: Artifact, *, queries: list, data_source_ids: list[str],
                               file_ids: list[str], theme_name, theme_overrides, bow_source_access, user,
                               title: str, project_id: str | None, slug_prefix: str, step_overrides=None,
                               copy_source_refs: bool = True, option_target=None, into=None,
                               any_status: bool = False):
    """A new conversation of `user` holding copies of `queries` (default
    steps and charts, references re-pointed), these sources and files, the
    theme and monitoring access. With `into`, that existing conversation of
    `user` takes them instead (sources and files added to its own; its theme
    and monitoring access kept when it has them). Returns (report, copy
    maps). Not committed."""
    import copy
    import uuid

    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association
    from app.models.report_file_association import report_file_association
    from app.models.widget import Widget
    from app.services.dashboard_copy import Placement, copy_query_rows

    from sqlalchemy import select as _select

    if into is None:
        report = Report(
            title=title, slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}", status="draft", report_type="regular",
            theme_name=theme_name, theme_overrides=copy.deepcopy(theme_overrides),
            user_id=str(user.id), organization_id=str(artifact.organization_id), project_id=project_id,
            bow_source_access=copy.deepcopy(bow_source_access) if copy_source_refs else None,
        )
        db.add(report)
        await db.flush()
        have_ds, have_files = set(), set()
    else:
        report = into
        if not report.theme_name and theme_name:
            report.theme_name = theme_name
            report.theme_overrides = copy.deepcopy(theme_overrides)
        if bow_source_access and copy_source_refs:
            # Union of restrictions (org-wide dominates): the destination
            # never ends up weaker than what it now holds (#1455 review, 4).
            from app.services.bow_source_access import merge_access
            report.bow_source_access = merge_access(report.bow_source_access, copy.deepcopy(bow_source_access))
        have_ds = {str(r) for (r,) in (await db.execute(_select(report_data_source_association.c.data_source_id).where(
            report_data_source_association.c.report_id == str(report.id)))).all()}
        have_files = {str(r) for (r,) in (await db.execute(_select(report_file_association.c.file_id).where(
            report_file_association.c.report_id == str(report.id)))).all()}
    for ds_id in dict.fromkeys(data_source_ids):
        if str(ds_id) in have_ds:
            continue
        await db.execute(report_data_source_association.insert().values(report_id=str(report.id),
                                                                         data_source_id=str(ds_id)))
    for file_id in dict.fromkeys(file_ids):
        if str(file_id) in have_files:
            continue
        await db.execute(report_file_association.insert().values(report_id=str(report.id), file_id=str(file_id)))

    async def widget_for(query) -> str:
        # A conversation's query has a widget; a release's copy has none.
        widget = Widget(title=query.title or "", slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}", status="draft",
                        x=0, y=0, width=5, height=9, report_id=str(report.id))
        db.add(widget)
        await db.flush()
        return str(widget.id)

    maps = await copy_query_rows(
        db, queries,
        placement=Placement(report_id=str(report.id), widget_for=widget_for, slug_prefix=slug_prefix),
        organization_id=str(artifact.organization_id), user_id=str(user.id),
        step_overrides=step_overrides, copy_source_refs=copy_source_refs, option_target=option_target,
        any_status=any_status,
    )
    return report, maps


def _content_for(release: ArtifactRelease, maps, stem: str, *, baked: bool = True):
    """The release's content with the copies' ids, and -- with `baked` -- its
    slide previews and deck as files of their own (the release's go with its
    next publish). Without it they are left out: those images carry the
    release's rows. Returns (content, deck path, old preview path -> new)."""
    from app.services.dashboard_copy import _copy_deck, _copy_previews, remap_ids

    content = remap_ids(release.content or {}, maps.ids())
    copied: dict = {}
    if not baked:
        content.pop("preview_images", None)
        return content, None, copied
    if content.get("preview_images"):
        content["preview_images"], copied = _copy_previews(content["preview_images"], str(release.id), stem=stem)
    return content, _copy_deck(release.pptx_path, stem), copied


def _copy_thumbnail(release: ArtifactRelease, version_id: str, copied_previews: dict) -> str | None:
    """The release's thumbnail as the new version's: a deck's is its first
    slide preview (already copied), a page's a thumbnail file."""
    if not release.thumbnail_path:
        return None
    if release.thumbnail_path in copied_previews:
        return copied_previews[release.thumbnail_path]
    try:
        from app.services.thumbnail_service import ThumbnailService
        return ThumbnailService().copy_thumbnail(f"release-{release.id}", version_id)
    except Exception:
        return None


def _remove_edit_files(stem: str, deck: str | None, thumbnail: str | None) -> None:
    """The files an Edit copied from the release, when its rows did not
    stay: its slide previews' folder, its deck and its thumbnail (only
    names this Edit made, inside uploads/)."""
    import shutil
    from pathlib import Path

    from app.services.dashboard_copy import _UPLOADS

    shutil.rmtree(_UPLOADS / "pptx_previews" / stem, ignore_errors=True)
    for path in (deck, thumbnail):
        if not path or "pptx_previews/" in str(path):
            continue  # a deck's thumbnail is one of its previews, gone with the folder
        try:
            target = Path(path) if Path(path).is_absolute() else _UPLOADS / str(path)
            target.resolve().relative_to(_UPLOADS.resolve())  # never outside uploads/
            target.unlink(missing_ok=True)
        except (ValueError, OSError):
            logger.warning("Edit: copied file %s not removed", path)


async def _editing_conversation(db, artifact: Artifact):
    from app.models.report import Report

    report = await db.get(Report, str(artifact.report_id)) if artifact.report_id else None
    if report is None or report.status == "archived" or report.deleted_at is not None:
        return None
    return report


async def edit_dashboard(db, artifact: Artifact, owner, organization) -> dict:
    """The owner's "Edit": the conversation the draft is edited in; when it
    was deleted, a new one from the published release, with a new draft
    version of the same dashboard there (link, sharing, schedule and chat
    stay). The rows are copied as they are when they are the owner's (the
    owner published the release); published by another editor (collab01),
    every step arrives empty and runs again as the owner -- returned as
    `pending_code` for the caller to spawn. The live release now matches
    that version (no unpublished changes yet). Two Edits at once: one wins,
    the other gets the winner's conversation."""
    from app.ee.audit.service import audit_service
    from app.services.artifact_service import new_version

    from app.services.dashboard_collab import _rerun_overrides, check_join_access

    editing = await _editing_conversation(db, artifact)
    if editing is not None:
        return {"report_id": str(editing.id), "created": False, "pending_code": {}}
    release = await _live_release(db, artifact)
    if release is None:
        raise AppError.conflict(ErrorCode.DASHBOARD_NOT_PUBLISHED, "Nothing published to edit from")
    artifact_id, was = str(artifact.id), (str(artifact.report_id) if artifact.report_id else None)
    # Whose run the stored rows are (collab01: a refresh may have run as
    # another editor than the publisher).
    own_rows = str(getattr(release, "run_by", None) or release.published_by or "") == str(owner.id)
    if not own_rows:
        from app.models.artifact_release import artifact_release_data_source_association as rassoc

        await check_join_access(db, artifact, owner, bow_source_access=release.bow_source_access, ds_ids=[
            str(i) for (i,) in (await db.execute(select(rassoc.c.data_source_id).where(
                rassoc.c.artifact_release_id == str(release.id)))).all()])

    report, maps = await release_to_conversation(
        db, artifact, release, user=owner, title=artifact.title or "Untitled", project_id=artifact.project_id,
        slug_prefix="edit", step_overrides=None if own_rows else _rerun_overrides, link_files=own_rows)
    source = (await db.execute(
        select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc()).limit(1)
    )).scalars().first()
    stem = f"edit-{report.id}"
    deck = thumbnail = None
    try:
        content, deck, previews = _content_for(release, maps, stem, baked=own_rows)
        version = await new_version(db, source, content=content, user_id=str(owner.id), report_id=str(report.id),
                                    status="completed", base_version_id=(
                                        str(release.source_version_id) if release.source_version_id else None),
                                    draft_id=None, **({"pptx_path": deck} if deck else {}))
        thumbnail = _copy_thumbnail(release, str(version.id), previews) if own_rows else None
        if thumbnail:
            version.thumbnail_path = thumbnail
        moved = await db.execute(
            update(Artifact).where(Artifact.id == artifact_id,
                                   Artifact.report_id.is_(None) if was is None else Artifact.report_id == was)
            .values(report_id=str(report.id)))
    except BaseException:
        _remove_edit_files(stem, deck, thumbnail)
        raise
    if moved.rowcount != 1:
        await db.rollback()  # another Edit moved it first: theirs stands
        _remove_edit_files(stem, deck, thumbnail)  # what this one copied holds the release's data
        winner = await db.get(Artifact, artifact_id)
        await db.refresh(winner)
        editing = await _editing_conversation(db, winner)
        if editing is None:
            raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "The dashboard changed meanwhile; try again")
        return {"report_id": str(editing.id), "created": False, "pending_code": {}}
    await db.execute(update(ArtifactRelease).where(ArtifactRelease.id == str(release.id))
                     .values(source_version_id=str(version.id)))
    pending = {}
    if not own_rows:
        from app.models.step import Step

        for new_step_id in maps.step_map.values():
            code = (await db.execute(select(Step.code).where(Step.id == new_step_id))).scalar()
            if (code or "").strip():
                pending[new_step_id] = code
    await db.commit()
    try:
        await audit_service.log(db=db, organization_id=str(organization.id), action="dashboard.edit_started",
                                user_id=str(owner.id), resource_type="artifact", resource_id=artifact_id,
                                details={"report_id": str(report.id), "release_id": str(release.id)})
    except Exception:
        pass
    return {"report_id": str(report.id), "created": True, "pending_code": pending}


_FORK_REFUSALS = {
    "not_logged_in": "Cannot fork: not_logged_in",
    "different_org": "Cannot fork: different_org",
    "no_permission": "Cannot fork: no permission to create reports",
    "forks_disabled": "Cannot fork: forks_disabled",
    "no_data_source_access": "Cannot fork: no_data_source_access",
}


async def fork_eligibility(db, artifact: Artifact, release: ArtifactRelease, user) -> dict:
    """Fork's rules, against the release: the forker is a member who may
    create conversations, the organization allows forks, and the forker may
    use every source the release runs on. {can_fork, reason}, as /r's page
    reads it; the reason is the first rule that fails."""
    from app.core.permission_resolver import FULL_ADMIN, resolve_permissions, user_can_access_data_source
    from app.models.organization import Organization
    from app.services.dashboard_access import _is_member
    from app.services.dashboard_runs import release_data_sources

    def no(reason):
        return {"can_fork": False, "reason": reason}

    if user is None:
        return no("not_logged_in")
    if not await _is_member(db, user, artifact.organization_id):
        return no("different_org")
    resolved = await resolve_permissions(db, str(user.id), str(artifact.organization_id))
    if FULL_ADMIN not in resolved.org_permissions and not resolved.has_org_permission("create_reports"):
        return no("no_permission")
    organization = await db.get(Organization, str(artifact.organization_id))
    settings = await organization.get_settings(db) if organization else None
    allow = settings.get_config("allow_forks") if settings is not None else None
    if allow is not None and not (allow.value if hasattr(allow, "value") else allow):
        return no("forks_disabled")
    for ds in await release_data_sources(db, release):
        if not await user_can_access_data_source(db, str(user.id), str(artifact.organization_id), ds):
            return no("no_data_source_access")
    return {"can_fork": True, "reason": None}


async def _fork_eligibility(db, artifact: Artifact, release: ArtifactRelease, user) -> None:
    from fastapi import HTTPException

    reason = (await fork_eligibility(db, artifact, release, user))["reason"]
    if reason is not None:
        raise HTTPException(status_code=403, detail=_FORK_REFUSALS[reason])


async def fork_release(db, artifact: Artifact, release: ArtifactRelease, user, *, title: str | None = None) -> dict:
    """Fork of a published dashboard: a new dashboard of the forker's, in a
    new conversation of theirs, with Fork's data policy -- over a personal
    connection or row-level security the owner's rows are not copied, and
    over a personal connection the code runs again as the forker
    (hydrate_fork, spawned by the route with the returned pending code).
    What the dashboard's own page withholds from this reader -- slide
    images, the deck, the thumbnail -- is not copied either; no file is
    linked; the fork does not name the owner's conversation."""
    from app.ee.audit.service import audit_service
    from app.models.artifact_resource import ArtifactResource
    from app.services.artifact_service import new_artifact
    from app.services.dashboard_data import any_copy_tainted, release_run_data_source_ids, release_snapshot_withheld
    from app.services.fork_service import identity_steps, make_step_overrides
    from app.services.viewer_data_policy import _any_rls_relation, _any_user_scoped_connection

    await _fork_eligibility(db, artifact, release, user)
    source_ids = await release_run_data_source_ids(db, release)
    user_scoped = await _any_user_scoped_connection(db, source_ids)
    strict_source = user_scoped or await _any_rls_relation(db, source_ids)
    baked = not (strict_source or await release_snapshot_withheld(db, artifact, release)
                 or await any_copy_tainted(db, release))
    report, maps = await release_to_conversation(
        db, artifact, release, user=user, title=title or f"Fork of {artifact.title or 'Untitled'}",
        project_id=None, slug_prefix="fork", step_overrides=make_step_overrides(
            user_scoped, strict_source,
            await identity_steps(db, [q.default_step for q in await _release_queries(db, str(release.id))
                                      if q.default_step is not None])),
        copy_source_refs=False, link_files=False)
    # The fork names the dashboard, never the owner's conversation (phase 8):
    # the editor's "Forked from" banner and hydration wait read it.
    report.forked_from_artifact_id = str(artifact.id)
    pending_code = {}
    if user_scoped:
        for query in await _release_queries(db, str(release.id)):
            step = query.default_step
            if step is not None and (step.code or "").strip():
                pending_code[maps.step_map[str(step.id)]] = maps.rewrite_code(step.code)
    content, deck, previews = _content_for(release, maps, f"fork-{report.id}", baked=baked)
    version = await new_artifact(db, report_id=str(report.id), organization_id=str(artifact.organization_id),
                                 user_id=str(user.id), mode=artifact.mode, title=artifact.title, content=content,
                                 **({"pptx_path": deck} if deck else {}))
    if baked:
        thumbnail = _copy_thumbnail(release, str(version.id), previews)
        if thumbnail:
            version.thumbnail_path = thumbnail
    # The app's resource definitions (schema, permissions), never its records,
    # as the conversation's Fork carries them.
    import copy
    for resource in (await db.execute(select(ArtifactResource).where(
            ArtifactResource.artifact_id == str(artifact.id), ArtifactResource.deleted_at.is_(None)))).scalars():
        db.add(ArtifactResource(artifact_id=str(version.artifact_id), organization_id=str(artifact.organization_id),
                                name=resource.name, kind=resource.kind,
                                definition=copy.deepcopy(resource.definition)))
    await db.commit()
    try:
        await audit_service.log(db=db, organization_id=str(artifact.organization_id), action="dashboard.forked",
                                user_id=str(user.id), resource_type="artifact", resource_id=str(artifact.id),
                                details={"report_id": str(report.id), "release_id": str(release.id)})
    except Exception:
        pass
    return {"report_id": str(report.id), "artifact_id": str(version.artifact_id), "pending_code": pending_code}


async def leave_project(db, project_id: str) -> None:
    """A deleted project's dashboards that no conversation of it settles --
    already detached, or of a conversation archived before: published ones
    leave it, the others are deleted. Not committed."""
    live = select(ArtifactRelease.id).where(ArtifactRelease.status == "ready",
                                             ArtifactRelease.deleted_at.is_(None)).scalar_subquery()
    await db.execute(update(Artifact).where(
        Artifact.project_id == str(project_id), Artifact.deleted_at.is_(None),
        Artifact.published_release_id.in_(live)).values(project_id=None))
    from app.models.report import Report

    gone = select(Report.id).where(Report.status == "archived").scalar_subquery()
    await db.execute(update(Artifact).where(
        Artifact.project_id == str(project_id), Artifact.deleted_at.is_(None),
        (Artifact.published_release_id.is_(None)) | (Artifact.published_release_id.notin_(live)),
        (Artifact.report_id.is_(None)) | (Artifact.report_id.in_(gone)))
        .values(deleted_at=datetime.utcnow()))
