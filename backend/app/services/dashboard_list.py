"""The dashboards page's list (phase 8): one card per dashboard.

Two tabs, as the page had over conversations: "my" -- the dashboards I own,
published or not -- and "shared" -- the others I can open. A dashboard is
listed only if its /d page would let the reader in (dashboard_access.
admit_reader: its editors, or once published whoever its sharing lets in),
so a card never leads to "not found". SQL narrows the candidates to what
that check can let in (mine; or published and shared with me, or in a
project I can view), and each is then admitted by the check itself, before
paging.

A card carries the conversation id only for whoever may edit the dashboard
(the pencil); readers get the /d link. The thumbnail is the published
version's for everyone, else -- for its editors -- the draft's.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_list_seen import ArtifactListSeen
from app.models.artifact_release import ArtifactShare
from app.models.artifact_star import ArtifactStar

def _like(text: str) -> str:
    """The words typed, not a pattern: % and _ match themselves."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _shared_with(db, user, organization):
    """What admit_reader can let in that is not mine: a published dashboard
    shared with the organization or the public, or with me or one of my
    groups; or one in a project I can view (its editors)."""
    from app.services.project_service import project_service
    from app.services.report_service import ReportService

    groups = ReportService._user_group_ids_subquery(user.id, str(organization.id))
    granted = exists(select(ArtifactShare.id).where(
        ArtifactShare.artifact_id == Artifact.id,
        or_(ArtifactShare.user_id == str(user.id), ArtifactShare.group_id.in_(groups))))
    published = Artifact.published_release_id.isnot(None)
    # An editor (collab01) sees the dashboard published or not.
    edit_granted = exists(select(ArtifactShare.id).where(
        ArtifactShare.artifact_id == Artifact.id, ArtifactShare.role == "edit",
        or_(ArtifactShare.user_id == str(user.id), ArtifactShare.group_id.in_(groups))))
    terms = [and_(published, Artifact.visibility.in_(("internal", "public"))),
             and_(published, Artifact.visibility == "shared", granted), edit_granted]
    projects = await project_service.get_visible_project_ids(db, user, organization)
    if projects:
        terms.append(Artifact.project_id.in_([str(p) for p in projects]))
    return or_(*terms)


def _thumbnail_url(path: str | None) -> str | None:
    if not path:
        return None
    return f"/thumbnails/{path.split('/')[-1]}"


async def _draft_thumbnail(db, artifact: Artifact) -> str | None:
    return (await db.execute(
        select(ArtifactVersion.thumbnail_path).where(
            ArtifactVersion.artifact_id == str(artifact.id), ArtifactVersion.deleted_at.is_(None),
            ArtifactVersion.status == "completed",
        ).order_by(ArtifactVersion.version.desc()).limit(1)
    )).scalar_one_or_none()


async def _card(db, artifact: Artifact, user, starred: set[str]) -> dict:
    from app.models.report import Report
    from app.models.user import User
    from app.services.dashboard_access import can_edit, is_owner, published_release

    release = await published_release(db, artifact)
    editor = await can_edit(db, artifact, user)
    report_id = None
    if editor:
        # The card's pencil opens the reader's own editing conversation
        # (collab01): several people edit a dashboard, each in their own.
        from app.services.dashboard_collab import my_conversation

        report_id = await my_conversation(db, artifact, user)
    thumbnail = release.thumbnail_path if release is not None else (
        await _draft_thumbnail(db, artifact) if editor else None)
    owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
    return {
        "id": str(artifact.id), "slug": artifact.slug, "title": await _card_title(db, artifact),
        "mode": artifact.mode,
        "published": release is not None, "is_owner": is_owner(artifact, user), "report_id": report_id,
        "role": "owner" if is_owner(artifact, user) else ("edit" if editor else "view"),
        "owner": {"id": str(owner.id), "name": owner.name or owner.email} if owner else None,
        "thumbnail_url": _thumbnail_url(thumbnail), "updated_at": artifact.updated_at,
        "created_at": artifact.created_at, "edited_at": artifact.content_updated_at or artifact.created_at,
        "is_starred": str(artifact.id) in starred,
    }


async def _card_title(db, artifact: Artifact) -> str | None:
    """A dashboard is named by its own `title`; renaming it no longer touches
    the conversation it was born from. An old dashboard that never got its own
    name still falls back to the conversation's (phase 9), so an untouched card
    reads as it did before."""
    from app.models.report import Report

    if artifact.title and artifact.title.strip():
        return artifact.title
    if artifact.legacy_report_id:
        name = (await db.execute(
            select(Report.title).where(Report.id == str(artifact.legacy_report_id))
        )).scalar_one_or_none()
        if name and name.strip():
            return name
    return artifact.title


def not_an_older_legacy_version():
    """artver01 gave every old version a dashboard of its own (its id is the
    version's): of those in one conversation and of one kind, only the newest
    is listed -- one card, as the editor and the link showed one dashboard
    (decision 4). The rows stay as they are; the editor lists them as
    versions."""
    newer = aliased(Artifact)
    own_version, newer_version = aliased(ArtifactVersion), aliased(ArtifactVersion)
    is_legacy = exists().where(own_version.id == Artifact.id)
    newer_legacy = exists().where(
        newer.report_id == Artifact.report_id, newer.mode == Artifact.mode, newer.id != Artifact.id,
        newer.deleted_at.is_(None), newer.created_at > Artifact.created_at,
        exists().where(newer_version.id == newer.id))
    # Unless its owner put it to use from the editor: published, shared or
    # scheduled, it is a dashboard people use (2026-10-21).
    in_use = or_(Artifact.published_release_id.isnot(None), Artifact.visibility != "none",
                 and_(Artifact.cron_schedule.isnot(None), Artifact.cron_schedule != ""))
    return ~and_(is_legacy, newer_legacy, Artifact.legacy_report_id.is_(None), ~in_use)


def not_only_failed():
    """A create_artifact whose render validation failed keeps its row (and a
    `failed` version) for debugging; it is not a dashboard anyone built, so
    no card -- unless something did go live from it."""
    any_version, working = aliased(ArtifactVersion), aliased(ArtifactVersion)
    has_versions = exists().where(any_version.artifact_id == Artifact.id, any_version.deleted_at.is_(None))
    has_working = exists().where(working.artifact_id == Artifact.id, working.deleted_at.is_(None),
                                 working.status != "failed")
    return or_(~has_versions, has_working, Artifact.published_release_id.isnot(None))


async def list_dashboards(db, user, organization, *, filter: str = "my", search: str | None = None,
                          mode: str | None = None, page: int = 1, limit: int = 15) -> dict:
    """One tab of the dashboards page. `meta.total` is an upper bound: it
    counts the SQL narrowing, and admit_reader can still leave a card out of
    its page (a release not ready yet, a monitoring dashboard the reader
    lacks the scope for), so a page may hold fewer than `limit` cards."""
    from app.services import dashboard_access

    conditions = [Artifact.organization_id == str(organization.id), Artifact.deleted_at.is_(None),
                  Artifact.owner_id.isnot(None), not_an_older_legacy_version(), not_only_failed()]
    if filter == "my":
        conditions.append(Artifact.owner_id == str(user.id))
    else:
        conditions += [Artifact.owner_id != str(user.id), await _shared_with(db, user, organization)]
    if search and search.strip():
        from app.models.report import Report

        like = _like(search.strip())
        conditions.append(or_(  # by either name an old dashboard's card may show
            Artifact.title.ilike(like, escape="\\"),
            exists().where(Report.id == Artifact.legacy_report_id, Report.title.ilike(like, escape="\\"))))
    if mode in ("page", "slides", "doc"):
        conditions.append(Artifact.mode == mode)
    # The SQL narrows to what admit_reader can let in, counts and pages; the
    # check then guards only the cards returned (a card it refuses -- a
    # monitoring dashboard the reader lacks the scope for, a release not
    # ready -- is left out of this page).
    total = (await db.execute(select(func.count()).select_from(Artifact).where(*conditions))).scalar() or 0
    total_pages = (total + limit - 1) // limit if total else 0
    candidates = (await db.execute(
        select(Artifact).where(*conditions).order_by(Artifact.updated_at.desc(), Artifact.id)
        .offset((page - 1) * limit).limit(limit)
    )).scalars().all()
    shown = []
    for artifact in candidates:
        try:
            await dashboard_access.admit_reader(db, artifact, user)
        except (AppError, HTTPException):
            continue
        shown.append(artifact)
    starred = await _starred_ids(db, user, [str(a.id) for a in shown])
    return {
        "dashboards": [await _card(db, a, user, starred) for a in shown],
        "meta": {"total": total, "page": page, "limit": limit, "total_pages": total_pages,
                 "has_next": page < total_pages, "has_prev": page > 1},
    }


# ── stars and the Shared tab's watermark (dashlist01) ───────────────────

async def _starred_ids(db, user, dashboard_ids: list[str]) -> set[str]:
    """Which of these dashboards the reader starred: one query per page."""
    if not dashboard_ids:
        return set()
    return set((await db.execute(
        select(ArtifactStar.artifact_id).where(ArtifactStar.user_id == str(user.id),
                                               ArtifactStar.artifact_id.in_(dashboard_ids))
    )).scalars().all())


def _is_unique_pair(exc: IntegrityError, constraint: str, columns: str) -> bool:
    """True when `exc` is that unique constraint firing. Postgres names the
    constraint; SQLite only lists its columns (as
    artifact_service._is_version_number_collision)."""
    message = str(exc.orig)
    return constraint in message or columns in message


async def set_star(db, user, organization, dashboard_id: str, starred: bool) -> dict:
    """Star or unstar a dashboard the reader can open (dashboard_access.
    admit_reader). A missing dashboard and one the reader cannot open answer
    the same 404, so a star never tells whether a dashboard exists. Starring
    twice, or two POSTs at once, leaves one star; unstarring deletes it."""
    from app.services import dashboard_access

    artifact = (await db.execute(
        select(Artifact).where(Artifact.id == str(dashboard_id), Artifact.organization_id == str(organization.id),
                               Artifact.deleted_at.is_(None))
    )).scalar_one_or_none()
    if artifact is None:
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    try:
        await dashboard_access.admit_reader(db, artifact, user)
    except (AppError, HTTPException):
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND) from None
    aid, uid = str(artifact.id), str(user.id)
    if starred:
        # No existence check first: a repeat meets the unique pair exactly as
        # a concurrent POST does. The savepoint undoes only this insert.
        try:
            async with db.begin_nested():
                db.add(ArtifactStar(artifact_id=aid, user_id=uid))
                await db.flush()
        except IntegrityError as exc:
            if not _is_unique_pair(exc, "uq_artifact_stars_artifact_user",
                                   "artifact_stars.artifact_id, artifact_stars.user_id"):
                raise
    else:
        await db.execute(delete(ArtifactStar).where(ArtifactStar.artifact_id == aid, ArtifactStar.user_id == uid))
    await db.commit()
    return {"id": aid, "is_starred": starred}


async def mark_shared_seen(db, user, organization) -> dict:
    """The reader looked at the Shared tab of this organization now. Returns
    when they did before (None on the first visit): the page keeps showing
    what is New against that for the rest of the visit."""
    now = datetime.utcnow()
    uid, oid = str(user.id), str(organization.id)
    mine = (ArtifactListSeen.user_id == uid, ArtifactListSeen.organization_id == oid)
    row = (await db.execute(select(ArtifactListSeen).where(*mine))).scalar_one_or_none()
    previous = None
    if row is None:
        try:
            async with db.begin_nested():
                db.add(ArtifactListSeen(user_id=uid, organization_id=oid, shared_seen_at=now))
                await db.flush()
        except IntegrityError as exc:
            # Two first visits at once: the other one wrote the row.
            if not _is_unique_pair(exc, "uq_artifact_list_seen_user_org",
                                   "artifact_list_seen.user_id, artifact_list_seen.organization_id"):
                raise
            row = (await db.execute(select(ArtifactListSeen).where(*mine))).scalar_one()
    seen = now
    if row is not None:
        previous = row.shared_seen_at
        # Never back: a concurrent visit may have committed a later time.
        seen = max(previous, now)
        row.shared_seen_at = seen
    await db.commit()
    return {"previous_seen_at": previous, "seen_at": seen}
