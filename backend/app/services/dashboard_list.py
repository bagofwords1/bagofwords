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
from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, select

from app.errors import AppError
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactShare

MAX_CANDIDATES = 2000  # after the SQL narrowing; the check runs per row


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
    terms = [and_(published, Artifact.visibility.in_(("internal", "public"))),
             and_(published, Artifact.visibility == "shared", granted)]
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


async def _card(db, artifact: Artifact, user) -> dict:
    from app.models.report import Report
    from app.models.user import User
    from app.services.dashboard_access import can_edit, is_owner, published_release

    release = await published_release(db, artifact)
    editor = await can_edit(db, artifact, user)
    report_id = None
    if editor and artifact.report_id:
        editing = await db.get(Report, str(artifact.report_id))
        if editing is not None and editing.deleted_at is None and editing.status != "archived":
            report_id = str(editing.id)
    thumbnail = release.thumbnail_path if release is not None else (
        await _draft_thumbnail(db, artifact) if editor else None)
    owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
    return {
        "id": str(artifact.id), "slug": artifact.slug, "title": artifact.title, "mode": artifact.mode,
        "published": release is not None, "is_owner": is_owner(artifact, user), "report_id": report_id,
        "owner": {"id": str(owner.id), "name": owner.name or owner.email} if owner else None,
        "thumbnail_url": _thumbnail_url(thumbnail), "updated_at": artifact.updated_at,
    }


async def list_dashboards(db, user, organization, *, filter: str = "my", search: str | None = None,
                          mode: str | None = None, page: int = 1, limit: int = 15) -> dict:
    from app.services.dashboard_access import admit_reader

    conditions = [Artifact.organization_id == str(organization.id), Artifact.deleted_at.is_(None),
                  Artifact.owner_id.isnot(None)]
    if filter == "my":
        conditions.append(Artifact.owner_id == str(user.id))
    else:
        conditions += [Artifact.owner_id != str(user.id), await _shared_with(db, user, organization)]
    if search and search.strip():
        conditions.append(Artifact.title.ilike(_like(search.strip()), escape="\\"))
    if mode in ("page", "slides", "doc"):
        conditions.append(Artifact.mode == mode)
    candidates = (await db.execute(
        select(Artifact).where(*conditions).order_by(Artifact.updated_at.desc(), Artifact.id)
        .limit(MAX_CANDIDATES)
    )).scalars().all()

    admitted = []
    for artifact in candidates:
        try:
            await admit_reader(db, artifact, user)
        except (AppError, HTTPException):
            continue
        admitted.append(artifact)

    total = len(admitted)
    total_pages = (total + limit - 1) // limit if total else 0
    shown = admitted[(page - 1) * limit:page * limit]
    return {
        "dashboards": [await _card(db, a, user) for a in shown],
        "meta": {"total": total, "page": page, "limit": limit, "total_pages": total_pages,
                 "has_next": page < total_pages, "has_prev": page > 1},
    }
