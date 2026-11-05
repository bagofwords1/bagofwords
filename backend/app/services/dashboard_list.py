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

The card (findability P5) says why the reader has the dashboard, whether
it is new to them, and -- for its editors only -- its schedule, last run
outcome and unpublished changes. Its fields are loaded for the whole page
in a fixed number of queries (`_page_extras`); only the editing check and
the pencil's conversation stay per card, as before.

A card lists its agents (findability P6): a live dashboard's release's, else
its conversation's -- only those the reader can view, and only those count
for the agent filter (recommended default 1).

The page's counts (findability P7, `list_facets`) use the same conditions,
each leaving its own filter out; "needs attention" (failing or stale) is the
org admin's alone (recommended default 3).
"""
import logging
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta
from functools import cached_property

from fastapi import HTTPException
from sqlalchemy import and_, case, delete, exists, false, func, or_, select, true, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_list_seen import ArtifactListSeen
from app.models.artifact_release import ArtifactConversation, ArtifactRelease, ArtifactShare
from app.models.artifact_star import ArtifactStar


logger = logging.getLogger(__name__)

BLANKS = " \t\r\n"  # what a blank title is made of (trim(x, chars): SQLite and Postgres alike)
STALE_AFTER = timedelta(days=90)  # not edited for this long: needs attention


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
    # Any of these agents (data sources); only those the reader can view count.
    agent_ids: tuple[str, ...] = ()
    created_from: datetime | None = None
    created_to: datetime | None = None
    edited_from: datetime | None = None
    edited_to: datetime | None = None
    status: str | None = None
    starred: bool = False
    # Needs attention (org admins only): the last refresh failed, or not
    # edited in STALE_AFTER.
    attention: bool = False
    sort: str = "edited"
    page: int = 1
    limit: int = 15
    # The Shared tab's watermark the page measures New from (the previous
    # visit); else the stored one. It only flags cards, it filters nothing.
    new_since: datetime | None = None


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


def _draft_head(column_of):
    """A column of the dashboard's draft head (dashboard_data.draft_version,
    which a publish copies): its newest completed version by number, of the
    implicit draft, in a live editing conversation when it has any. It
    correlates everything but its own table, so it can sit inside another
    subquery (has_agent)."""
    version = aliased(ArtifactVersion)
    return select(column_of(version)).where(
        version.artifact_id == Artifact.id, version.status == "completed", version.deleted_at.is_(None),
        version.draft_id.is_(None),
        or_(~_editing_conversation(), _editing_conversation(version.report_id)),
    ).order_by(version.version.desc()).limit(1).correlate_except(version).scalar_subquery()


class ReaderScope:
    """The SQL vocabulary of one reader's list (ADR-2): the list conditions,
    the order and (later) the card columns and facets use these same
    expressions, each built once per request."""

    def __init__(self, user_id: str, organization_id: str, groups, project_ids: tuple[str, ...],
                 seen_at: datetime | None = None, visible_agents: frozenset[str] = frozenset(),
                 *, is_admin: bool = False):
        self.user_id = user_id
        self.is_admin = is_admin  # the organization's FULL_ADMIN: "needs attention" is theirs
        self.now = datetime.utcnow()  # naive UTC, once per request: what "stale" is measured from
        self.organization_id = organization_id
        self.groups = groups  # a subquery of my group ids in this organization
        self.project_ids = project_ids  # the projects I can view
        self.seen_at = seen_at  # the Shared tab's watermark (naive UTC), if any
        # The agents I can view (derived_access.visible_data_source_ids) that
        # the agents page lists for me (_listed_agents): the only ones a card
        # names or the agent filter matches (default 1).
        self.visible_agents = visible_agents

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
    def failing(self):
        return Artifact.last_run_status == "failed"

    @cached_property
    def stale(self):
        return self.edited < self.now - STALE_AFTER

    @cached_property
    def attention(self):
        """Needs attention (the org admin's line): the last refresh failed, or
        not edited in STALE_AFTER."""
        return or_(self.failing, self.stale)

    @cached_property
    def published_at(self):
        release = aliased(ArtifactRelease)
        return select(release.published_at).where(release.id == Artifact.published_release_id).scalar_subquery()

    @cached_property
    def share_reach(self):
        """When the dashboard reached me by a share: my earliest share's
        creation, to me or to one of my groups (NULL without one). Share
        rows keep their time across sharing saves and transfers (P3b)."""
        share = aliased(ArtifactShare)
        return select(func.min(share.created_at)).where(
            share.artifact_id == Artifact.id,
            or_(share.user_id == self.user_id, share.group_id.in_(self.groups)),
        ).correlate_except(share).scalar_subquery()

    @cached_property
    def is_new(self):
        """It reached me after the watermark: by a share made after it, or
        through the organization by a release published after it (Nir, "Use
        publish time"; Known limitations 7-8) -- every way it reached me, so
        an older share keeps a republished dashboard from being new. A
        project dashboard is never new; without a watermark nothing is."""
        if self.seen_at is None:
            return false()
        seen = self.seen_at
        not_project = (or_(Artifact.project_id.is_(None), Artifact.project_id.notin_(self.project_ids))
                       if self.project_ids else true())
        return and_(
            not_project,
            or_(self.share_reach.isnot(None), self.org),
            or_(self.share_reach.is_(None), self.share_reach > seen),
            or_(~self.org, self.published_at > seen),
        )

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
        release = aliased(ArtifactRelease)
        newest = _draft_head(lambda version: version.id)
        source = select(release.source_version_id).where(release.id == Artifact.published_release_id).scalar_subquery()
        return and_(Artifact.published_release_id.isnot(None), newest.isnot(None),
                    or_(source.is_(None), newest != source))

    @cached_property
    def statuses(self) -> dict:
        """The `status` values (My only), mutually exclusive and covering
        every card: never published; published with unpublished changes;
        published as it is."""
        published = Artifact.published_release_id.isnot(None)
        return {"draft": Artifact.published_release_id.is_(None),
                "changes": and_(published, self.unpublished_changes),
                "published": and_(published, ~self.unpublished_changes)}

    @cached_property
    def draft_report(self):
        """The conversation whose agents an unpublished card shows: the one
        that wrote the draft's newest version -- what a publish copies
        (dashboard_copy.copy_dashboard takes the version's conversation's
        data sources) -- else, with no completed version yet, its own."""
        return func.coalesce(_draft_head(lambda version: version.report_id), Artifact.report_id)

    @cached_property
    def live(self):
        """Its release is live (dashboard_access.is_live): the card says
        published, and its agents are the release's."""
        release = aliased(ArtifactRelease)
        return exists(select(release.id).where(
            release.id == Artifact.published_release_id, release.deleted_at.is_(None), release.status == "ready",
        ).correlate_except(release))

    def has_agent(self, ids):
        """It has one of these agents, by the card's rule: a live dashboard
        its release's, another the agents of the conversation that wrote its
        draft (`draft_report`). Agents not mine to see (`visible_agents`) do
        not count, so filtering by them alone lists nothing. Each subquery
        correlates everything but its own table: a facet may nest this a
        level deeper (see _live_report)."""
        from app.models.artifact_release import artifact_release_data_source_association
        from app.models.report_data_source_association import report_data_source_association

        wanted = sorted(set(ids) & set(self.visible_agents))
        if not wanted:
            return false()
        on_release = artifact_release_data_source_association.alias()
        on_conversation = report_data_source_association.alias()
        from_release = exists(select(on_release.c.data_source_id).where(
            on_release.c.artifact_release_id == Artifact.published_release_id,
            on_release.c.data_source_id.in_(wanted)).correlate_except(on_release))
        from_conversation = exists(select(on_conversation.c.data_source_id).where(
            on_conversation.c.report_id == self.draft_report,
            on_conversation.c.data_source_id.in_(wanted)).correlate_except(on_conversation))
        return or_(and_(self.live, from_release), and_(~self.live, from_conversation))


async def reader_scope(db, user, organization, *, new_since: datetime | None = None) -> ReaderScope:
    """`new_since` is the watermark New is measured from; else the reader's
    stored visit to Shared in this organization; else there is none."""
    from app.core.derived_access import visible_data_source_ids
    from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
    from app.services.project_service import project_service
    from app.services.report_service import ReportService

    groups = ReportService._user_group_ids_subquery(user.id, str(organization.id))
    projects = await project_service.get_visible_project_ids(db, user, organization)
    seen_at = new_since
    if seen_at is None:
        seen_at = (await db.execute(
            select(ArtifactListSeen.shared_seen_at).where(ArtifactListSeen.user_id == str(user.id),
                                                          ArtifactListSeen.organization_id == str(organization.id))
        )).scalar_one_or_none()
    agents = await _listed_agents(db, user, organization, await visible_data_source_ids(db, user, organization))
    is_admin = FULL_ADMIN in (await resolve_permissions(db, str(user.id), str(organization.id))).org_permissions
    return ReaderScope(str(user.id), str(organization.id), groups, tuple(str(p) for p in projects), seen_at,
                       agents, is_admin=is_admin)


def _admit_attention(scope: ReaderScope, query: ListQuery) -> None:
    """`attention` is the org admin's (recommended default 3): anyone else
    asking for it is refused, on the list and on the facets alike."""
    if query.attention and not scope.is_admin:
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED, "Needs attention is for the organization's admins")


async def _listed_agents(db, user, organization, visible: set[str]) -> frozenset[str]:
    """The agents a card may name to this reader: those they can view that
    the agents page also lists for them (data_source_service.
    get_data_sources, ~:1526-1559, and get_active_data_sources, ~:1786-1803).
    A disabled agent is off for everyone; a draft is its managers' (org
    governance or `manage` on it); a `development` one its agent admins'
    (_development_hidden); a connected application's session sees only its
    agents (connected_application_service.agent_ceiling; no ceiling in a
    plain web session). Closed by default: what the page hides, the card
    never names. visible_data_source_ids itself is left as it is."""
    from app.models.data_source import DataSource
    from app.services.connected_application_service import agent_ceiling
    from app.services.data_source_service import DataSourceService

    if not visible:
        return frozenset()
    is_gov, manage_ids, resolved = await DataSourceService()._publish_visibility(db, user, organization)
    ceiling = await agent_ceiling(db, organization.id)
    rows = (await db.execute(
        select(DataSource.id, DataSource.publish_status, DataSource.reliability_status)
        .where(DataSource.organization_id == str(organization.id))
    )).all()
    listed = set()
    for ds_id, publish_status, reliability_status in rows:
        ds_id = str(ds_id)
        if ds_id not in visible or (ceiling is not None and ds_id not in ceiling):
            continue
        publish_status = publish_status or "published"
        if publish_status == "disabled":
            continue
        if publish_status == "draft" and not (is_gov or ds_id in manage_ids):
            continue
        if DataSourceService._development_hidden(reliability_status, ds_id, is_gov, resolved):
            continue
        listed.add(ds_id)
    return frozenset(listed)


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
    if query.agent_ids:
        named["agents"] = scope.has_agent(query.agent_ids)
    if query.created_from is not None or query.created_to is not None:
        named["created"] = _within(Artifact.created_at, query.created_from, query.created_to)
    if query.edited_from is not None or query.edited_to is not None:
        named["edited"] = _within(scope.edited, query.edited_from, query.edited_to)
    if query.filter == "my" and query.status is not None:
        # My only: unpublished changes are for the dashboard's editors.
        named["status"] = scope.statuses[query.status]
    if query.starred:
        named["starred"] = scope.starred
    if query.attention:
        # Closed by default: the routes refuse it to anyone else first.
        named["attention"] = scope.attention if scope.is_admin else false()
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


REASONS = ("direct", "group", "project", "org")  # a card's reasons, in this order; the first is primary
EDITOR_ROLES = ("owner", "edit")


async def _page_extras(db, scope: ReaderScope, rows, *, shared: bool) -> dict:
    """Everything the page's cards show beyond the artifact row, in a fixed
    number of queries whatever the page size: the live releases, owners,
    the draft thumbnails, old dashboards' conversation titles, and -- on
    Shared -- my shares (with their groups' names) and the projects' names;
    the stars; each card's agents the reader may see named (two queries,
    _page_agents); whether public links are on. `rows` are the page's rows:
    the artifact with its labeled columns (`edited`, `is_new`,
    `unpublished_changes`, `published_at`, `draft_report_id`)."""
    from app.models.group import Group
    from app.models.project import Project
    from app.models.report import Report
    from app.models.user import User

    artifacts = [row.Artifact for row in rows]
    ids = [str(a.id) for a in artifacts]
    extras = {"user_id": scope.user_id, "shared": shared, "columns": {str(row.Artifact.id): row for row in rows},
              "releases": {}, "owners": {}, "drafts": {}, "legacy_titles": {}, "shares": {}, "projects": {},
              "starred": set(), "agents": {}, "public_links": True}
    if not artifacts:
        return extras

    release_ids = {str(a.published_release_id) for a in artifacts if a.published_release_id}
    if release_ids:
        extras["releases"] = {str(r.id): r for r in (await db.execute(
            select(ArtifactRelease).where(ArtifactRelease.id.in_(release_ids)))).scalars().all()}
    owner_ids = {str(a.owner_id) for a in artifacts if a.owner_id}
    if owner_ids:
        extras["owners"] = {str(u.id): u for u in (await db.execute(
            select(User).where(User.id.in_(owner_ids)))).scalars().all()}

    # The newest completed version's thumbnail, for the cards of a dashboard
    # not live (its editors see the draft's).
    not_live = [str(a.id) for a in artifacts if _live_release(a, extras) is None]
    if not_live:
        version, newer = aliased(ArtifactVersion), aliased(ArtifactVersion)
        newest = select(newer.id).where(
            newer.artifact_id == version.artifact_id, newer.deleted_at.is_(None), newer.status == "completed",
        ).order_by(newer.version.desc()).limit(1).correlate_except(newer).scalar_subquery()
        extras["drafts"] = dict((await db.execute(
            select(version.artifact_id, version.thumbnail_path).where(version.artifact_id.in_(not_live),
                                                                      version.id == newest))).all())

    legacy = {str(a.legacy_report_id) for a in artifacts
              if a.legacy_report_id and not (a.title and a.title.strip())}
    if legacy:
        extras["legacy_titles"] = dict((await db.execute(
            select(Report.id, Report.title).where(Report.id.in_(legacy)))).all())

    if shared:
        mine = (await db.execute(
            select(ArtifactShare.artifact_id, ArtifactShare.user_id, Group.name)
            .outerjoin(Group, Group.id == ArtifactShare.group_id)
            .where(ArtifactShare.artifact_id.in_(ids),
                   or_(ArtifactShare.user_id == scope.user_id, ArtifactShare.group_id.in_(scope.groups)))
        )).all()
        for artifact_id, user_id, group_name in mine:
            entry = extras["shares"].setdefault(str(artifact_id), {"direct": False, "groups": []})
            if user_id is not None and str(user_id) == scope.user_id:
                entry["direct"] = True
            elif group_name is not None:
                entry["groups"].append(group_name)
        project_ids = {str(a.project_id) for a in artifacts if a.project_id and str(a.project_id) in scope.project_ids}
        if project_ids:
            extras["projects"] = dict((await db.execute(
                select(Project.id, Project.name).where(Project.id.in_(project_ids)))).all())

    extras["starred"] = await _starred_ids(db, scope.user_id, ids)
    extras["agents"] = await _page_agents(db, scope, artifacts, extras)
    if any(a.visibility == "public" for a in artifacts):
        from app.services.public_sharing_policy import public_sharing_allowed

        extras["public_links"] = await public_sharing_allowed(db, scope.organization_id)
    return extras


async def _page_agents(db, scope: ReaderScope, artifacts, extras: dict) -> dict:
    """Each card's agents the reader may see named (`visible_agents`), in two
    queries for the page: which agents (a live dashboard's release's,
    another's draft conversation's -- the page's `draft_report_id` column --
    as ReaderScope.has_agent), then their names and icons (_agent_details).
    A card lists them by name ignoring case, then id."""
    from app.models.artifact_release import artifact_release_data_source_association as on_release
    from app.models.report_data_source_association import report_data_source_association as on_conversation

    if not scope.visible_agents:
        return {}
    by_release, by_conversation = {}, {}  # release id -> card; conversation id -> its cards
    for artifact in artifacts:
        release = _live_release(artifact, extras)
        if release is not None:
            by_release[str(release.id)] = str(artifact.id)
        else:
            draft_report = extras["columns"][str(artifact.id)].draft_report_id
            if draft_report:
                by_conversation.setdefault(str(draft_report), []).append(str(artifact.id))
    parts = []
    if by_release:
        parts.append(select(on_release.c.artifact_release_id.label("owner"), on_release.c.data_source_id.label("agent"))
                     .where(on_release.c.artifact_release_id.in_(list(by_release))))
    if by_conversation:
        parts.append(select(on_conversation.c.report_id.label("owner"),
                            on_conversation.c.data_source_id.label("agent"))
                     .where(on_conversation.c.report_id.in_(list(by_conversation))))
    if not parts:
        return {}
    mapping = (await db.execute(parts[0] if len(parts) == 1 else union_all(*parts))).all()
    per_card: dict[str, set[str]] = {}
    for owner, agent in mapping:
        agent = str(agent)
        if agent not in scope.visible_agents:
            continue  # never named to a reader who cannot view it (default 1)
        owner = str(owner)
        for aid in ([by_release[owner]] if owner in by_release else by_conversation.get(owner, [])):
            per_card.setdefault(aid, set()).add(agent)
    shown = set().union(*per_card.values()) if per_card else set()
    if not shown:
        return {}
    agents = await _agent_details(db, shown)
    return {aid: sorted((agents[a] for a in found if a in agents), key=_agent_order)
            for aid, found in per_card.items()}


def _agent_order(agent: dict):
    return (agent["name"] or "").lower(), agent["id"]


async def _agent_details(db, ids) -> dict:
    """Agents by id, as a card names them: name, connection type and icon
    (DataSource with its connections in the order its relationship yields
    them, as agent_icon.resolve_agent_icon_token requires)."""
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.domain_connection import domain_connection
    from app.schemas.agent_icon import resolve_agent_icon_token

    rows = (await db.execute(
        select(DataSource.id, DataSource.name, DataSource.icon, Connection.type, Connection.config)
        .outerjoin(domain_connection, domain_connection.c.data_source_id == DataSource.id)
        .outerjoin(Connection, Connection.id == domain_connection.c.connection_id)
        .where(DataSource.id.in_(list(ids)))
        .order_by(Connection.created_at, Connection.id)
    )).all()
    names, icons, connections = {}, {}, {}
    for ds_id, name, icon, conn_type, conn_config in rows:
        ds_id = str(ds_id)
        names.setdefault(ds_id, name)
        icons.setdefault(ds_id, icon)
        found = connections.setdefault(ds_id, [])
        if conn_type is not None:
            found.append({"type": conn_type, "config": conn_config})
    return {ds_id: {"id": ds_id, "name": names[ds_id],
                    "type": connections[ds_id][0]["type"] if connections[ds_id] else None,
                    "icon_token": resolve_agent_icon_token(icons[ds_id], connections[ds_id])}
            for ds_id in names}


def _live_release(artifact: Artifact, extras: dict):
    """The dashboard's live release, as dashboard_access.published_release
    has it (dashboard_access.is_live)."""
    from app.services.dashboard_access import is_live

    if not artifact.published_release_id:
        return None
    release = extras["releases"].get(str(artifact.published_release_id))
    return release if is_live(release) else None


def _card_title(artifact: Artifact, extras: dict) -> str | None:
    """A dashboard is named by its own `title`; renaming it no longer touches
    the conversation it was born from. An old dashboard that never got its own
    name still falls back to the conversation's (phase 9), so an untouched card
    reads as it did before."""
    if artifact.title and artifact.title.strip():
        return artifact.title
    if artifact.legacy_report_id:
        name = extras["legacy_titles"].get(str(artifact.legacy_report_id))
        if name and name.strip():
            return name
    return artifact.title


def _access(artifact: Artifact, extras: dict) -> dict:
    """Why the reader has a dashboard of their Shared tab: every reason, in
    REASONS order; the first is primary; `via_name` names the group (the
    alphabetically first of mine it is shared with) or the project."""
    share = extras["shares"].get(str(artifact.id), {"direct": False, "groups": []})
    project_name = extras["projects"].get(str(artifact.project_id)) if artifact.project_id else None
    has = {
        "direct": share["direct"],
        "group": bool(share["groups"]),
        "project": project_name is not None,
        "org": artifact.published_release_id is not None and artifact.visibility in ("internal", "public"),
    }
    reasons = [r for r in REASONS if has[r]]
    primary = reasons[0] if reasons else None
    via_name = None
    if primary == "group":
        via_name = sorted(share["groups"])[0]
    elif primary == "project":
        via_name = project_name
    return {"reasons": reasons, "primary": primary, "via_name": via_name}


def _card(artifact: Artifact, extras: dict, editor: bool, report_id: str | None) -> dict:
    """One card, from the page's extras alone. `editor` (dashboard_access.
    can_edit) and the pencil's `report_id` are the reader's, per card."""
    aid = str(artifact.id)
    columns = extras["columns"][aid]
    mine = artifact.owner_id is not None and str(artifact.owner_id) == extras["user_id"]
    role = "owner" if mine else ("edit" if editor else "view")
    release = _live_release(artifact, extras)
    published = release is not None
    thumbnail = release.thumbnail_path if published else (extras["drafts"].get(aid) if editor else None)
    owner = extras["owners"].get(str(artifact.owner_id)) if artifact.owner_id else None
    visibility = artifact.visibility
    if visibility == "public" and not extras["public_links"]:
        visibility = "internal"  # as the access check treats it (public_sharing_policy)
    # The schedule, the last run's outcome and unpublished changes are the
    # editors' (default 8); when it last ran is everyone's ("Data as of").
    for_editors = role in EDITOR_ROLES
    return {
        "id": aid, "slug": artifact.slug, "title": _card_title(artifact, extras),
        "mode": artifact.mode,
        "published": published, "is_owner": mine, "report_id": report_id, "role": role,
        "owner": {"id": str(owner.id), "name": owner.name or owner.email} if owner else None,
        "thumbnail_url": _thumbnail_url(thumbnail), "updated_at": artifact.updated_at,
        "created_at": artifact.created_at, "edited_at": columns.edited,
        "is_starred": aid in extras["starred"],
        "published_at": columns.published_at if published else None,
        "visibility": visibility,
        "access": _access(artifact, extras) if extras["shared"] else None,
        "is_new": bool(columns.is_new) if extras["shared"] else False,
        "unpublished_changes": (published and bool(columns.unpublished_changes)) if for_editors else None,
        "cron_schedule": artifact.cron_schedule if for_editors else None,
        "last_run_at": artifact.last_run_at,
        "last_run_status": artifact.last_run_status if for_editors else None,
        "agents": extras["agents"].get(aid, []),
    }


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


def page_query(scope: ReaderScope, query: ListQuery):
    """One page of a tab's candidates, each with the card's columns computed
    in SQL: `edited`, `is_new` (Shared only), `unpublished_changes` (shown
    to editors only, by _card) and the live release's `published_at`."""
    is_new = scope.is_new if query.filter == "shared" else false()
    return (select(Artifact, scope.edited.label("edited"), is_new.label("is_new"),
                   scope.unpublished_changes.label("unpublished_changes"),
                   scope.published_at.label("published_at"),
                   scope.draft_report.label("draft_report_id"))
            .where(*conditions(scope, query)).order_by(*order_by(scope, query))
            .offset((query.page - 1) * query.limit).limit(query.limit))


async def list_dashboards(db, user, organization, query: ListQuery) -> dict:
    """One tab of the dashboards page. `meta.total` is an upper bound: it
    counts the SQL narrowing, and admit_reader can still leave a card out of
    its page (a release not ready yet, a monitoring dashboard the reader
    lacks the scope for), so a page may hold fewer than `limit` cards."""
    from app.services import dashboard_access
    from app.services.dashboard_collab import my_conversation

    scope = await reader_scope(db, user, organization, new_since=query.new_since)
    _admit_attention(scope, query)
    where = conditions(scope, query)
    page, limit = query.page, query.limit
    # The SQL narrows to what admit_reader can let in, counts and pages; the
    # check then guards only the cards returned (a card it refuses -- a
    # monitoring dashboard the reader lacks the scope for, a release not
    # ready -- is left out of this page).
    total = (await db.execute(select(func.count()).select_from(Artifact).where(*where))).scalar() or 0
    total_pages = (total + limit - 1) // limit if total else 0
    candidates = (await db.execute(page_query(scope, query))).all()
    shown = []
    for row in candidates:
        try:
            await dashboard_access.admit_reader(db, row.Artifact, user)
        except (AppError, HTTPException) as refused:
            # By id only: a title would say what the reader may not see.
            logger.debug("dashboards list: candidate %s refused by admit_reader (%s)",
                         row.Artifact.id, type(refused).__name__)
            continue
        shown.append(row)
    extras = await _page_extras(db, scope, shown, shared=query.filter == "shared")
    cards = []
    for row in shown:
        artifact = row.Artifact
        editor = await dashboard_access.can_edit(db, artifact, user)
        # The card's pencil opens the reader's own editing conversation
        # (collab01): several people edit a dashboard, each in their own.
        report_id = await my_conversation(db, artifact, user) if editor else None
        cards.append(_card(artifact, extras, editor, report_id))
    return {
        "dashboards": cards,
        "meta": {"total": total, "page": page, "limit": limit, "total_pages": total_pages,
                 "has_next": page < total_pages, "has_prev": page > 1},
    }


# ── facets (findability P7) ─────────────────────────────────────────────

ACCESS_CHIPS = ("direct", "group", "project", "org", "edit")
STATUSES = ("published", "changes", "draft")


def _counts(where, terms: dict):
    """One query: how many rows `where` admits, and how many of them each
    term holds for."""
    columns = [func.count().label("all")] + [
        func.coalesce(func.sum(case((term, 1), else_=0)), 0).label(name) for name, term in terms.items()]
    return select(*columns).select_from(Artifact).where(*where)


def facet_queries(scope: ReaderScope, query: ListQuery) -> dict:
    """The facets' queries for one request of the page, by facet. Each count
    is over `conditions` with its own filter left out (`ignore`), so it is the
    list's meta.total with that value applied; a facet the tab has no use
    for (access and owners on My, status on Shared) is absent, as is
    `attention` for anyone but an org admin and `agents` when the reader may
    see none."""
    from app.models.artifact_release import artifact_release_data_source_association
    from app.models.report_data_source_association import report_data_source_association
    from app.models.user import User

    shared = query.filter == "shared"
    queries = {
        # The tab alone: no filter moves them.
        "tabs_my": select(func.count()).select_from(Artifact).where(*conditions(scope, ListQuery(filter="my"))),
        "tabs_shared": select(func.count()).select_from(Artifact).where(
            *conditions(scope, ListQuery(filter="shared"))),
        # The Shared tab's New cards, no filter applied.
        "new_count": select(func.count()).select_from(Artifact).where(
            *conditions(scope, ListQuery(filter="shared")), scope.is_new),
    }
    if shared:
        queries["access"] = _counts(conditions(scope, query, ignore="access"),
                                    {chip: getattr(scope, chip) for chip in ACCESS_CHIPS})
        # An aliased owner: the search's own owner subquery must not correlate to it.
        owner = aliased(User)
        queries["owners"] = (
            select(owner.id, owner.name, owner.email, func.count(Artifact.id).label("count"))
            .select_from(Artifact).join(owner, owner.id == Artifact.owner_id)
            .where(*conditions(scope, query, ignore="owners"))
            .group_by(owner.id, owner.name, owner.email))
    else:
        queries["status"] = _counts(conditions(scope, query, ignore="status"), scope.statuses)
    if scope.visible_agents:
        # Each (dashboard, agent) pair by the card's rule (ReaderScope.has_agent):
        # a live dashboard's release's agents, another's draft conversation's.
        # Every agent is counted; list_facets keeps only `visible_agents`
        # (no per-agent bind parameters).
        where = conditions(scope, query, ignore="agents")
        on_release = artifact_release_data_source_association.alias()
        on_conversation = report_data_source_association.alias()
        pairs = union_all(
            select(Artifact.id.label("artifact_id"), on_release.c.data_source_id.label("agent"))
            .join(on_release, on_release.c.artifact_release_id == Artifact.published_release_id)
            .where(*where, scope.live),
            select(Artifact.id.label("artifact_id"), on_conversation.c.data_source_id.label("agent"))
            .join(on_conversation, on_conversation.c.report_id == scope.draft_report)
            .where(*where, ~scope.live),
        ).subquery()
        queries["agents"] = (select(pairs.c.agent, func.count(pairs.c.artifact_id.distinct()).label("count"))
                             .group_by(pairs.c.agent))
    if scope.is_admin:
        # The organization's dashboards on Shared, with the other filters.
        org = replace(query, filter="shared", access="org")
        queries["attention"] = _counts(conditions(scope, org, ignore="attention"),
                                       {"failing": scope.failing, "stale": scope.stale, "total": scope.attention})
    return queries


async def list_facets(db, user, organization, query: ListQuery) -> dict:
    """How many dashboards each filter value of the page would list: the
    tabs, the access chips (Shared), the agents, the owners (Shared), the
    status (My), New (Shared, unfiltered) and -- for org admins only, else
    null -- "needs attention" (Shared, access=org: failing, stale).

    Each count leaves its own filter out and keeps the others, so it equals
    the list's meta.total with that value applied (behavior contract 4).

    Known limitation 3, plainly: these counts and meta.total are upper
    bounds. Both count the SQL candidates; admit_reader runs only on the
    page shown, so a candidate it later refuses -- mostly a monitoring
    dashboard whose access lineage the reader cannot read, or a release not
    ready yet -- is counted here and never listed. The response itself never
    carries a dashboard's title or id; the owners facet names owners (name,
    else email), and only owners of dashboards the SQL admits. But a refused
    candidate is still counted, so by probing -- narrowing the search, the
    dates or the owner until a count moves -- a reader can infer such a
    dashboard's title words, its created and edited dates and its owner. The
    SQL does not restrict it (ADR-3: no second visibility gate)."""
    scope = await reader_scope(db, user, organization, new_since=query.new_since)
    _admit_attention(scope, query)
    queries = facet_queries(scope, query)

    async def one(name):
        return (await db.execute(queries[name])).one()

    facets = {
        "tabs": {"my": (await one("tabs_my"))[0], "shared": (await one("tabs_shared"))[0]},
        "new_count": (await one("new_count"))[0],
        "access": None, "agents": [], "owners": [], "status": None, "attention": None,
    }
    if "access" in queries:
        row = (await one("access"))._mapping
        facets["access"] = {name: int(row[name]) for name in ("all",) + ACCESS_CHIPS}
    if "owners" in queries:
        rows = (await db.execute(queries["owners"])).all()
        owners = [{"id": str(uid), "name": name or email, "count": count} for uid, name, email, count in rows]
        facets["owners"] = sorted(owners, key=lambda o: ((o["name"] or "").lower(), o["id"]))
    if "status" in queries:
        row = (await one("status"))._mapping
        facets["status"] = {name: int(row[name]) for name in STATUSES}
    if "agents" in queries:
        counts = {str(agent): count for agent, count in (await db.execute(queries["agents"])).all()
                  if str(agent) in scope.visible_agents}  # never named to a reader who may not see it (default 1)
        details = await _agent_details(db, counts) if counts else {}
        facets["agents"] = sorted(({**details[a], "count": n} for a, n in counts.items() if a in details),
                                  key=_agent_order)
    if "attention" in queries:
        row = (await one("attention"))._mapping
        facets["attention"] = {name: int(row[name]) for name in ("failing", "stale", "total")}
    return facets


# ── stars and the Shared tab's watermark (dashlist01) ───────────────────

async def _starred_ids(db, user_id: str, dashboard_ids: list[str]) -> set[str]:
    """Which of these dashboards the reader starred: one query per page."""
    if not dashboard_ids:
        return set()
    return set((await db.execute(
        select(ArtifactStar.artifact_id).where(ArtifactStar.user_id == str(user_id),
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
