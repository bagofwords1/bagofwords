"""Who may open a dashboard, keyed on the dashboard (phase 4).

Two doors, one check each (docs/design/dashboards-first-class.md):

- `/d/{id|name}` serves the published release. The order is today's
  conversation check (ReportService._check_visibility), asked of the
  dashboard: monitoring access, the owner, whoever can view its project,
  then its visibility -- private (owner only, "not found"), shared (people
  and groups), the organization, public. Sharing a conversation opens no
  dashboard. Three things are stricter than the conversation check,
  because this is new code: the owner and a grant count only while their
  holder is still in the organization (a live membership; for `internal`
  too), and a visibility value this code does not know opens to no one.
- `/d/{id}/draft` serves the draft to the people who can open the editor:
  the owner and whoever can view the project. Anyone else is told it does
  not exist, whatever the dashboard's visibility.

A dashboard without a ready release is unpublished: its editors are told so
(the page then sends them to the draft), everyone else that it does not
exist.
"""

from sqlalchemy import or_, select

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactRelease, ArtifactShare

VISIBILITIES = ("none", "shared", "internal", "public")

from app.services.dashboard_slug import _UUID  # noqa: E402  (one pattern for ids and names)


def _not_found() -> AppError:
    return AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)


async def resolve(db, ref: str) -> Artifact:
    """The live dashboard a /d reference names: an id, or else a name."""
    column = Artifact.id if _UUID.match(ref or "") else Artifact.slug
    artifact = (await db.execute(
        select(Artifact).where(column == ref, Artifact.deleted_at.is_(None))
    )).scalar_one_or_none()
    if artifact is None and column is Artifact.slug:
        # A name the dashboard had keeps leading to it (phase 8).
        from app.services.dashboard_slug import by_old_name

        old = await by_old_name(db, ref)
        artifact = (await db.execute(
            select(Artifact).where(Artifact.id == str(old), Artifact.deleted_at.is_(None))
        )).scalar_one_or_none() if old else None
    if artifact is None:
        raise _not_found()
    return artifact


async def published_release(db, artifact: Artifact) -> ArtifactRelease | None:
    if not artifact.published_release_id:
        return None
    release = await db.get(ArtifactRelease, str(artifact.published_release_id))
    if release is None or release.deleted_at is not None or release.status != "ready":
        return None
    return release


def is_owner(artifact: Artifact, user) -> bool:
    return user is not None and artifact.owner_id is not None and str(artifact.owner_id) == str(user.id)


async def counts_as_owner(db, artifact: Artifact, user) -> bool:
    """The owner, while still in the dashboard's organization: who gets the
    owner's view of the data."""
    from app.core.permission_resolver import principal_belongs_to_org

    return is_owner(artifact, user) and await principal_belongs_to_org(db, user, str(artifact.organization_id))


async def can_edit(db, artifact: Artifact, user) -> bool:
    """The owner, whoever can view the dashboard's project, or a person or
    group granted 'edit' (collab01) -- while still in the dashboard's
    organization."""
    from app.core.permission_resolver import principal_belongs_to_org

    if user is None or not await principal_belongs_to_org(db, user, str(artifact.organization_id)):
        return False
    if is_owner(artifact, user):
        return True
    if await _edit_granted(db, artifact, user):
        return True
    if not artifact.project_id:
        return False
    from app.models.project import Project
    from app.services.project_service import project_service

    project = await db.get(Project, str(artifact.project_id))
    return project is not None and await project_service.user_can_view_project(db, user, project)


async def holds_snapshot(db, artifact: Artifact, release, user) -> bool:
    """Whether a release's stored rows are `user`'s own run (collab01):
    whoever produced them -- the publisher, then whoever each refresh ran as
    (`run_by`) -- while still in the organization. Several people publish
    one dashboard now, so owning it no longer says whose rows these are."""
    from app.core.permission_resolver import principal_belongs_to_org

    if user is None or release is None:
        return False
    runner = getattr(release, "run_by", None) or release.published_by
    if runner is None:  # nothing records it: the owner published, as before
        return await counts_as_owner(db, artifact, user)
    return str(runner) == str(user.id) and await principal_belongs_to_org(db, user, str(artifact.organization_id))


async def assert_may_write(db, artifact_id: str | None, user_id: str | None) -> None:
    """Refuse a change to a dashboard (a new version, a rename, a delete) by
    someone who can no longer edit it (collab01). Several people edit one
    dashboard, each through their own conversation: owning a version or the
    conversation it lives in is no longer permission to change the shared
    dashboard -- a removed editor keeps both. The owner and a dashboard
    without an owner (not one any more, or never) keep the conversation's
    own rules."""
    if not artifact_id or not user_id:
        return
    artifact = await db.get(Artifact, str(artifact_id))
    if artifact is None or not artifact.owner_id or str(artifact.owner_id) == str(user_id):
        return
    from app.models.user import User

    user = await db.get(User, str(user_id))
    if user is None or not await can_edit(db, artifact, user):
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED, "You can no longer edit this dashboard")


async def _edit_granted(db, artifact: Artifact, user) -> bool:
    """An 'edit' grant to `user` or one of their groups. The caller checked
    the live membership (a grant to someone who left opens nothing)."""
    from app.services.report_service import ReportService

    groups = ReportService._user_group_ids_subquery(user.id, artifact.organization_id)
    return (await db.execute(
        select(ArtifactShare.id).where(
            ArtifactShare.artifact_id == str(artifact.id),
            ArtifactShare.role == "edit",
            or_(ArtifactShare.user_id == str(user.id), ArtifactShare.group_id.in_(groups)),
        ).limit(1)
    )).scalar_one_or_none() is not None


# ── what a person may do with a dashboard (collab01) ─────────────────────
#
# One answer for every route and every button: the page meta returns it and
# the frontend shows only what is in it.

CAP_VIEW = "view"                # open the published dashboard
CAP_EDIT = "edit"                # open the draft, edit it in a conversation of their own
CAP_PUBLISH = "publish"          # publish a version, roll back to a kept release
CAP_SHARE = "share"              # add or remove viewers and editors
CAP_MAKE_PUBLIC = "make_public"  # visibility 'public'
CAP_SETTINGS = "settings"        # schedule, chat, Data tab, link name, notes
CAP_RUN_IDENTITY = "run_identity"  # viewer <-> creator credentials, "editors can share"
CAP_TRANSFER = "transfer"        # hand the dashboard to another editor
CAP_ANALYTICS = "analytics"      # read the dashboard's view counts

_EDITOR_CAPS = {CAP_VIEW, CAP_EDIT, CAP_PUBLISH, CAP_SHARE, CAP_SETTINGS, CAP_ANALYTICS}
_OWNER_CAPS = _EDITOR_CAPS | {CAP_MAKE_PUBLIC, CAP_RUN_IDENTITY, CAP_TRANSFER}


async def capabilities(db, artifact: Artifact, user) -> set[str]:
    """What `user` may do with this dashboard. The owner: everything. An
    editor: edit, publish (not a dashboard that runs as its creator -- its
    readers run with the owner's credentials), share, settings, analytics. The organization's admin: read and hand
    it over. Anyone /d lets in: view. Nobody else: nothing."""
    if await counts_as_owner(db, artifact, user):
        return set(_OWNER_CAPS)
    caps: set[str] = set()
    if await can_edit(db, artifact, user):
        caps = set(_EDITOR_CAPS)
        if (artifact.shared_run_identity or "viewer") == "creator":
            caps.discard(CAP_PUBLISH)
    if await is_org_admin(db, artifact, user):
        caps |= {CAP_VIEW, CAP_TRANSFER}
    if CAP_VIEW not in caps:
        try:
            await admit_reader(db, artifact, user)
            caps.add(CAP_VIEW)
        except AppError:
            pass
    return caps


async def _is_member(db, user, organization_id) -> bool:
    """A live Membership, as the conversation check asks for 'internal'
    (service accounts have none)."""
    from app.models.membership import Membership

    return (await db.execute(
        select(Membership.id).where(
            Membership.user_id == str(user.id),
            Membership.organization_id == str(organization_id),
            Membership.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none() is not None


async def _granted(db, artifact: Artifact, user) -> bool:
    from app.services.report_service import ReportService

    groups = ReportService._user_group_ids_subquery(user.id, artifact.organization_id)
    return (await db.execute(
        select(ArtifactShare.id).where(
            ArtifactShare.artifact_id == str(artifact.id),
            or_(ArtifactShare.user_id == str(user.id), ArtifactShare.group_id.in_(groups)),
        ).limit(1)
    )).scalar_one_or_none() is not None


async def is_org_admin(db, artifact: Artifact, user) -> bool:
    """The organization's full admin, while a member of it: opens every
    published dashboard of the organization, as /r lets an admin open every
    conversation's dashboard (main, #1400), and reads drafts (phase 9: after
    the data migration a private conversation's dashboard is a draft) --
    without editing them."""
    from app.core.permission_resolver import FULL_ADMIN, principal_belongs_to_org, resolve_permissions

    if user is None:
        return False
    organization_id = str(artifact.organization_id)
    if not await principal_belongs_to_org(db, user, organization_id):
        return False
    return FULL_ADMIN in (await resolve_permissions(db, str(user.id), organization_id)).org_permissions


async def check_visibility(db, artifact: Artifact, user) -> None:
    """Raise unless the dashboard's visibility lets `user` in (owner and
    project already handled by the caller); the organization's admin always."""
    if await is_org_admin(db, artifact, user):
        return
    visibility = artifact.visibility or "none"
    if visibility == "public":
        return
    if visibility not in ("shared", "internal"):
        raise _not_found()
    if user is None:
        raise AppError.unauthorized()
    if not await _is_member(db, user, artifact.organization_id):
        raise AppError.forbidden()
    if visibility == "shared" and not await _granted(db, artifact, user):
        raise AppError.forbidden()


async def open_published(db, ref: str, user) -> tuple[Artifact, ArtifactRelease]:
    """The dashboard and its live release, or the error `user` should get."""
    from app.services.bow_source_access import assert_read

    artifact = await resolve(db, ref)
    editor = await can_edit(db, artifact, user)
    release = await published_release(db, artifact)
    if release is None:
        # Told to whoever may read the draft, so the page takes them there.
        if editor or await is_org_admin(db, artifact, user):
            raise AppError.not_found(ErrorCode.DASHBOARD_NOT_PUBLISHED)
        raise _not_found()
    await assert_read(db, release.bow_source_access, user)
    if not editor:
        await check_visibility(db, artifact, user)
    return artifact, release


async def open_draft(db, ref: str, user) -> Artifact:
    """The dashboard, for its editors -- and, to read, the organization's
    admin, as main's /r shows an admin every conversation's dashboard (#1400).
    Every draft route is a read; running and editing go through the
    conversation, which stays its owner's."""
    from app.services.bow_source_access import assert_read, report_access

    if user is None:
        # Said to every anonymous caller alike, so it reveals nothing.
        raise AppError.unauthorized()
    artifact = await resolve(db, ref)
    if not await can_edit(db, artifact, user) and not await is_org_admin(db, artifact, user):
        raise _not_found()
    # The draft lives in the editing conversations: none once they were all
    # deleted (phase 7; "Edit" starts a new one from the published release).
    # Several people may edit it, each in a conversation of their own
    # (collab01).
    from app.services.dashboard_collab import editing_report_ids

    editing = await editing_report_ids(db, artifact)
    if not editing:
        raise _not_found()
    await assert_read(db, await report_access(db, editing[0]), user)
    return artifact


async def admit_reader(db, artifact: Artifact, user) -> None:
    """Raise unless `user` may read this dashboard through /d: its editors,
    or -- once published -- whoever its sharing lets in. For what the page
    reaches besides the release (its runtime: forms, records, views)."""
    from app.services.bow_source_access import assert_read, report_access

    release = await published_release(db, artifact)
    if await can_edit(db, artifact, user):
        # As the doors do: /d checks the release's monitoring access, the
        # draft its conversation's.
        if release is not None:
            await assert_read(db, release.bow_source_access, user)
        elif artifact.report_id:
            await assert_read(db, await report_access(db, artifact.report_id), user)
        return
    if release is None:
        raise _not_found()
    await assert_read(db, release.bow_source_access, user)
    await check_visibility(db, artifact, user)
