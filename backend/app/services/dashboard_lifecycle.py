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
from datetime import datetime

from sqlalchemy import select, update

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactRelease


async def _dashboards(db, report_ids: list[str], *, lock: bool = False) -> list[Artifact]:
    if not report_ids:
        return []
    stmt = select(Artifact).where(Artifact.report_id.in_([str(r) for r in report_ids]),
                                  Artifact.deleted_at.is_(None))
    if lock:
        # Serializes with start_publish, which locks the dashboard too.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


async def _live_release(db, artifact: Artifact) -> ArtifactRelease | None:
    if not artifact.published_release_id:
        return None
    release = await db.get(ArtifactRelease, str(artifact.published_release_id))
    return release if release is not None and release.status == "ready" and release.deleted_at is None else None


async def _newest_completed_version_id(db, artifact: Artifact) -> str | None:
    found = (await db.execute(
        select(ArtifactVersion.id).where(
            ArtifactVersion.artifact_id == str(artifact.id), ArtifactVersion.deleted_at.is_(None),
            ArtifactVersion.status == "completed",
        ).order_by(ArtifactVersion.version.desc()).limit(1)
    )).scalar()
    return str(found) if found else None


async def delete_outcomes(db, report_ids: list[str]) -> list[dict]:
    """For each dashboard of these conversations: what deleting them does to
    it -- survives, loses_changes (published, with unpublished edits) or
    deleted (never published)."""
    out = []
    for artifact in await _dashboards(db, report_ids):
        release = await _live_release(db, artifact)
        if release is None:
            outcome = "deleted"
        elif await _newest_completed_version_id(db, artifact) != (
                str(release.source_version_id) if release.source_version_id else None):
            outcome = "loses_changes"
        else:
            outcome = "survives"
        out.append({"id": str(artifact.id), "title": artifact.title, "mode": artifact.mode, "outcome": outcome})
    return out


async def settle_dashboards(db, report_ids: list[str]) -> None:
    """The archived conversations' dashboards: published ones leave them,
    the others are deleted. 409 -- and the caller's archive rolls back --
    when one of them is publishing (after failing a publish that stopped
    beating): what would become of it if that publish then failed is not
    something to guess. Each dashboard is locked, as start_publish locks it,
    so the two never interleave. Not committed: part of the caller's
    archive."""
    from app.services.dashboard_publish import _publishing, reap_stale

    now = datetime.utcnow()
    for artifact in await _dashboards(db, report_ids, lock=True):
        await reap_stale(db, artifact)
        if await _publishing(db, artifact) is not None:
            raise AppError.conflict(ErrorCode.DASHBOARD_PUBLISH_IN_PROGRESS,
                                    f'"{artifact.title or "A dashboard"}" is being published; try again when it '
                                    "finishes")
        if await _live_release(db, artifact) is not None:
            await db.execute(update(Artifact).where(Artifact.id == str(artifact.id)).values(report_id=None))
        else:
            await db.execute(update(Artifact).where(Artifact.id == str(artifact.id)).values(deleted_at=now))


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
                                  copy_source_refs: bool = True, link_files: bool = True):
    """A new conversation of `user` holding a copy of the release: its
    queries, their default steps and charts (references re-pointed by the
    copy engine), its sources, theme and monitoring access, and -- with
    `link_files` -- its files (the owner's Edit only: a file linked to a
    conversation is readable by whoever can view it, and a Fork, like the
    conversation's Fork, links none). Returns (report, copy maps). Not
    committed."""
    import copy
    import uuid

    from app.models.artifact_release import (
        artifact_release_data_source_association as release_sources,
    )
    from app.models.artifact_release import (
        artifact_release_file_association as release_files,
    )
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association
    from app.models.report_file_association import report_file_association
    from app.models.widget import Widget
    from app.services.dashboard_copy import Placement, copy_query_rows

    report = Report(
        title=title, slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}", status="draft", report_type="regular",
        theme_name=release.theme_name, theme_overrides=copy.deepcopy(release.theme_overrides),
        user_id=str(user.id), organization_id=str(artifact.organization_id), project_id=project_id,
        bow_source_access=copy.deepcopy(release.bow_source_access) if copy_source_refs else None,
    )
    db.add(report)
    await db.flush()
    for (ds_id,) in (await db.execute(select(release_sources.c.data_source_id).where(
            release_sources.c.artifact_release_id == str(release.id)))).all():
        await db.execute(report_data_source_association.insert().values(report_id=str(report.id),
                                                                         data_source_id=str(ds_id)))
    if link_files:
        for (file_id,) in (await db.execute(select(release_files.c.file_id).where(
                release_files.c.artifact_release_id == str(release.id)))).all():
            await db.execute(report_file_association.insert().values(report_id=str(report.id),
                                                                     file_id=str(file_id)))

    async def widget_for(query) -> str:
        # A conversation's query has a widget; a release's copy has none.
        widget = Widget(title=query.title or "", slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}", status="draft",
                        x=0, y=0, width=5, height=9, report_id=str(report.id))
        db.add(widget)
        await db.flush()
        return str(widget.id)

    maps = await copy_query_rows(
        db, await _release_queries(db, str(release.id)),
        placement=Placement(report_id=str(report.id), widget_for=widget_for, slug_prefix=slug_prefix),
        organization_id=str(artifact.organization_id), user_id=str(user.id),
        step_overrides=step_overrides, copy_source_refs=copy_source_refs,
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
    stay). The rows are copied as they are: they are the owner's. The live
    release now matches that version (no unpublished changes yet). Two Edits
    at once: one wins, the other gets the winner's conversation."""
    from app.ee.audit.service import audit_service
    from app.services.artifact_service import new_version

    editing = await _editing_conversation(db, artifact)
    if editing is not None:
        return {"report_id": str(editing.id), "created": False}
    release = await _live_release(db, artifact)
    if release is None:
        raise AppError.conflict(ErrorCode.DASHBOARD_NOT_PUBLISHED, "Nothing published to edit from")
    artifact_id, was = str(artifact.id), (str(artifact.report_id) if artifact.report_id else None)

    report, maps = await release_to_conversation(
        db, artifact, release, user=owner, title=artifact.title or "Untitled", project_id=artifact.project_id,
        slug_prefix="edit")
    source = (await db.execute(
        select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id)
        .order_by(ArtifactVersion.version.desc()).limit(1)
    )).scalars().first()
    content, deck, previews = _content_for(release, maps, f"edit-{report.id}")
    version = await new_version(db, source, content=content, user_id=str(owner.id), report_id=str(report.id),
                                status="completed", **({"pptx_path": deck} if deck else {}))
    thumbnail = _copy_thumbnail(release, str(version.id), previews)
    if thumbnail:
        version.thumbnail_path = thumbnail
    moved = await db.execute(
        update(Artifact).where(Artifact.id == artifact_id,
                               Artifact.report_id.is_(None) if was is None else Artifact.report_id == was)
        .values(report_id=str(report.id)))
    if moved.rowcount != 1:
        await db.rollback()  # another Edit moved it first: theirs stands
        winner = await db.get(Artifact, artifact_id)
        await db.refresh(winner)
        editing = await _editing_conversation(db, winner)
        if editing is None:
            raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "The dashboard changed meanwhile; try again")
        return {"report_id": str(editing.id), "created": False}
    await db.execute(update(ArtifactRelease).where(ArtifactRelease.id == str(release.id))
                     .values(source_version_id=str(version.id)))
    await db.commit()
    try:
        await audit_service.log(db=db, organization_id=str(organization.id), action="dashboard.edit_started",
                                user_id=str(owner.id), resource_type="artifact", resource_id=artifact_id,
                                details={"report_id": str(report.id), "release_id": str(release.id)})
    except Exception:
        pass
    return {"report_id": str(report.id), "created": True}


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
    from app.models.membership import Membership
    from app.models.organization import Organization
    from app.services.dashboard_runs import release_data_sources

    def no(reason):
        return {"can_fork": False, "reason": reason}

    if user is None:
        return no("not_logged_in")
    member = (await db.execute(select(Membership.id).where(
        Membership.user_id == str(user.id), Membership.organization_id == str(artifact.organization_id),
        Membership.deleted_at.is_(None)))).first()
    if member is None:
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
    from app.services.fork_service import make_step_overrides
    from app.services.viewer_data_policy import _any_rls_relation, _any_user_scoped_connection

    await _fork_eligibility(db, artifact, release, user)
    source_ids = await release_run_data_source_ids(db, release)
    user_scoped = await _any_user_scoped_connection(db, source_ids)
    strict_source = user_scoped or await _any_rls_relation(db, source_ids)
    baked = not (strict_source or await release_snapshot_withheld(db, artifact, release)
                 or await any_copy_tainted(db, release))
    report, maps = await release_to_conversation(
        db, artifact, release, user=user, title=title or f"Fork of {artifact.title or 'Untitled'}",
        project_id=None, slug_prefix="fork", step_overrides=make_step_overrides(user_scoped, strict_source),
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
