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

Finding a dashboard (findability P4): a `ListQuery` narrows and orders the
list in SQL, through the expressions of a per-request `ReaderScope` (why
the reader has each dashboard, starred, edited, unpublished changes). The
filters only narrow what a tab lists; they never let in more.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property

from fastapi import HTTPException
from sqlalchemy import and_, case, delete, exists, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_list_seen import ArtifactListSeen
from app.models.artifact_release import ArtifactConversation, ArtifactRelease, ArtifactShare
from app.models.artifact_star import ArtifactStar


logger = logging.getLogger(__name__)

BLANKS = " \t\r\n"  # what a blank title is made of (trim(x, chars): SQLite and Postgres alike)


def _like(text: str, *, prefix: bool = False) -> str:
    """The words typed, not a pattern: % and _ match themselves. Anywhere,
    or (`prefix`) from the start."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%" if prefix else "%" + escaped + "%"


@dataclass(frozen=True)
class ListQuery:
    """One request of the dashboards page. Datetimes are naive UTC; a `from`
    is inclusive, a `to` exclusive. `access` applies on Shared only and
    `status` on My only; each is accepted and ignored on the other tab."""
    filter: str = "my"
    search: str | None = None
    mode: str | None = None
    access: str = "all"
    owner_ids: tuple[str, ...] = ()
    created_from: datetime | None = None
    created_to: datetime | None = None
    edited_from: datetime | None = None
    edited_to: datetime | None = None
    status: str | None = None
    starred: bool = False
    sort: str = "edited"
    page: int = 1
    limit: int = 15


def _live_report(report_id):
    """`report_id` is a conversation still there (dashboard_collab._live_reports).

    These subqueries sit two levels below the listed dashboard (inside the
    `newest` version subquery): SQLAlchemy auto-correlates one level only,
    so each correlates everything but its own table explicitly -- else the
    outer `artifacts` is joined in again and the test runs over every row."""
    from app.models.report import Report

    report = aliased(Report)
    return exists(select(report.id).where(report.id == report_id, report.deleted_at.is_(None),
                                          report.status != "archived").correlate_except(report))


def _editing_conversation(report_id=None):
    """A live conversation edits the dashboard -- its own or a joined one
    (dashboard_collab.editing_report_ids); with `report_id`, that one."""
    joined = aliased(ArtifactConversation)
    own = and_(Artifact.report_id.isnot(None), _live_report(Artifact.report_id))
    other = [joined.artifact_id == Artifact.id, joined.ended_at.is_(None), joined.deleted_at.is_(None),
             _live_report(joined.report_id)]
    if report_id is not None:
        own = and_(own, Artifact.report_id == report_id)
        other.append(joined.report_id == report_id)
    return or_(own, exists(select(joined.id).where(*other).correlate_except(joined)))


class ReaderScope:
    """The SQL vocabulary of one reader's list (ADR-2): the list conditions,
    the order and (later) the card columns and facets use these same
    expressions, each built once per request."""

    def __init__(self, user_id: str, organization_id: str, groups, project_ids: tuple[str, ...]):
        self.user_id = user_id
        self.organization_id = organization_id
        self.groups = groups  # a subquery of my group ids in this organization
        self.project_ids = project_ids  # the projects I can view

    def _share(self, *where):
        return exists(select(ArtifactShare.id).where(ArtifactShare.artifact_id == Artifact.id, *where))

    @cached_property
    def direct(self):
        """A share to me by name (to view or to edit)."""
        return self._share(ArtifactShare.user_id == self.user_id)

    @cached_property
    def group(self):
        return self._share(ArtifactShare.group_id.in_(self.groups))

    @cached_property
    def project(self):
        if not self.project_ids:
            return false()
        return Artifact.project_id.in_(self.project_ids)

    @cached_property
    def org(self):
        return and_(Artifact.published_release_id.isnot(None), Artifact.visibility.in_(("internal", "public")))

    @cached_property
    def edit(self):
        """Whoever can_edit lets in besides the owner: an edit grant, or the project."""
        granted = self._share(ArtifactShare.role == "edit",
                              or_(ArtifactShare.user_id == self.user_id, ArtifactShare.group_id.in_(self.groups)))
        return or_(granted, self.project)

    @cached_property
    def shared_with(self):
        """What admit_reader can let in that is not mine: a published dashboard
        shared with the organization or the public, or with me or one of my
        groups; one I may edit (collab01), published or not; or one in a
        project I can view (its editors)."""
        published = Artifact.published_release_id.isnot(None)
        granted = self._share(or_(ArtifactShare.user_id == self.user_id, ArtifactShare.group_id.in_(self.groups)))
        edit_granted = self._share(ArtifactShare.role == "edit", or_(
            ArtifactShare.user_id == self.user_id, ArtifactShare.group_id.in_(self.groups)))
        terms = [and_(published, Artifact.visibility.in_(("internal", "public"))),
                 and_(published, Artifact.visibility == "shared", granted), edit_granted]
        if self.project_ids:
            terms.append(Artifact.project_id.in_(self.project_ids))
        return or_(*terms)

    @cached_property
    def rank(self):
        """Shared, every reason: shares to me first, then my groups' and my
        projects', then the organization's."""
        return case((self.direct, 0), (or_(self.group, self.project), 1), else_=2)

    @cached_property
    def starred(self):
        return exists(select(ArtifactStar.id).where(ArtifactStar.artifact_id == Artifact.id,
                                                    ArtifactStar.user_id == self.user_id))

    @cached_property
    def edited(self):
        return func.coalesce(Artifact.content_updated_at, Artifact.created_at)

    @cached_property
    def published_at(self):
        release = aliased(ArtifactRelease)
        return select(release.published_at).where(release.id == Artifact.published_release_id).scalar_subquery()

    @cached_property
    def title(self):
        """The card's title, case-folded: its own when not blank, else its
        legacy conversation's when not blank; else NULL (sorted last)."""
        from app.models.report import Report

        legacy = aliased(Report)
        legacy_title = select(legacy.title).where(legacy.id == Artifact.legacy_report_id).scalar_subquery()

        def blank_is_null(text):  # as _card_title's strip(): spaces, tabs and line breaks
            return func.nullif(func.trim(text, BLANKS), "")

        return func.lower(func.coalesce(blank_is_null(Artifact.title), blank_is_null(legacy_title)))

    @cached_property
    def unpublished_changes(self):
        """Published, and the draft's newest completed version is not the
        live release's (dashboard_publish.publish_status): the newest by
        version number, of the implicit draft, in a live editing conversation
        when the dashboard has any."""
        version, release = aliased(ArtifactVersion), aliased(ArtifactRelease)
        newest = select(version.id).where(
            version.artifact_id == Artifact.id, version.status == "completed", version.deleted_at.is_(None),
            version.draft_id.is_(None),
            or_(~_editing_conversation(), _editing_conversation(version.report_id)),
        ).order_by(version.version.desc()).limit(1).scalar_subquery()
        source = select(release.source_version_id).where(release.id == Artifact.published_release_id).scalar_subquery()
        return and_(Artifact.published_release_id.isnot(None), newest.isnot(None),
                    or_(source.is_(None), newest != source))


async def reader_scope(db, user, organization) -> ReaderScope:
    from app.services.project_service import project_service
    from app.services.report_service import ReportService

    groups = ReportService._user_group_ids_subquery(user.id, str(organization.id))
    projects = await project_service.get_visible_project_ids(db, user, organization)
    return ReaderScope(str(user.id), str(organization.id), groups, tuple(str(p) for p in projects))


def conditions(scope: ReaderScope, query: ListQuery, *, ignore: str | None = None) -> list:
    """The SQL candidates of one tab with the query's filters; `ignore`
    names one filter to leave out (a facet counts without its own)."""
    from app.models.report import Report
    from app.models.user import User

    where = [Artifact.organization_id == scope.organization_id, Artifact.deleted_at.is_(None),
             Artifact.owner_id.isnot(None), not_an_older_legacy_version(), not_only_failed()]
    if query.filter == "my":
        where.append(Artifact.owner_id == scope.user_id)
    else:
        where += [Artifact.owner_id != scope.user_id, scope.shared_with]
    named = {}
    if query.search and query.search.strip():
        typed = query.search.strip()
        like = _like(typed)
        terms = [  # by either name an old dashboard's card may show
            Artifact.title.ilike(like, escape="\\"),
            exists().where(Report.id == Artifact.legacy_report_id, Report.title.ilike(like, escape="\\"))]
        if query.filter == "shared":
            # And by its owner (on My that is always the reader): the name
            # anywhere, the email only from its start -- a domain alone
            # would match everyone.
            terms.append(exists().where(User.id == Artifact.owner_id, or_(
                User.name.ilike(like, escape="\\"), User.email.ilike(_like(typed, prefix=True), escape="\\"))))
        named["search"] = or_(*terms)
    if query.mode in ("page", "slides", "doc"):
        named["mode"] = Artifact.mode == query.mode
    if query.filter == "shared" and query.access != "all":
        named["access"] = {"direct": scope.direct, "group": scope.group, "project": scope.project,
                           "org": scope.org, "edit": scope.edit}[query.access]
    if query.owner_ids:
        named["owners"] = Artifact.owner_id.in_(query.owner_ids)
    if query.created_from is not None or query.created_to is not None:
        named["created"] = _within(Artifact.created_at, query.created_from, query.created_to)
    if query.edited_from is not None or query.edited_to is not None:
        named["edited"] = _within(scope.edited, query.edited_from, query.edited_to)
    if query.filter == "my" and query.status is not None:
        # My only: unpublished changes are for the dashboard's editors.
        published = Artifact.published_release_id.isnot(None)
        named["status"] = {"draft": Artifact.published_release_id.is_(None),
                           "changes": and_(published, scope.unpublished_changes),
                           "published": and_(published, ~scope.unpublished_changes)}[query.status]
    if query.starred:
        named["starred"] = scope.starred
    return where + [term for name, term in named.items() if name != ignore]


def _within(column, start: datetime | None, end: datetime | None):
    terms = []
    if start is not None:
        terms.append(column >= start)
    if end is not None:
        terms.append(column < end)
    return and_(*terms)


def order_by(scope: ReaderScope, query: ListQuery) -> list:
    """Starred first; on Shared with every reason, the rank; the sort; the
    id, so the order is total and pages never overlap. A missing key (no
    title, never published) sorts last on both databases."""
    keys = [case((scope.starred, 0), else_=1)]
    if query.filter == "shared" and query.access == "all":
        keys.append(scope.rank)
    if query.sort == "title":
        keys += [scope.title.is_(None), scope.title]
    elif query.sort == "published":
        keys += [scope.published_at.is_(None), scope.published_at.desc()]
    elif query.sort == "created":
        keys.append(Artifact.created_at.desc())
    elif query.sort == "-created":
        keys.append(Artifact.created_at.asc())
    else:
        keys.append(scope.edited.desc())
    return keys + [Artifact.id]


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


async def list_dashboards(db, user, organization, query: ListQuery) -> dict:
    """One tab of the dashboards page. `meta.total` is an upper bound: it
    counts the SQL narrowing, and admit_reader can still leave a card out of
    its page (a release not ready yet, a monitoring dashboard the reader
    lacks the scope for), so a page may hold fewer than `limit` cards."""
    from app.services import dashboard_access

    scope = await reader_scope(db, user, organization)
    where = conditions(scope, query)
    page, limit = query.page, query.limit
    # The SQL narrows to what admit_reader can let in, counts and pages; the
    # check then guards only the cards returned (a card it refuses -- a
    # monitoring dashboard the reader lacks the scope for, a release not
    # ready -- is left out of this page).
    total = (await db.execute(select(func.count()).select_from(Artifact).where(*where))).scalar() or 0
    total_pages = (total + limit - 1) // limit if total else 0
    candidates = (await db.execute(
        select(Artifact).where(*where).order_by(*order_by(scope, query))
        .offset((page - 1) * limit).limit(limit)
    )).scalars().all()
    shown = []
    for artifact in candidates:
        try:
            await dashboard_access.admit_reader(db, artifact, user)
        except (AppError, HTTPException) as refused:
            # By id only: a title would say what the reader may not see.
            logger.debug("dashboards list: candidate %s refused by admit_reader (%s)",
                         artifact.id, type(refused).__name__)
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
