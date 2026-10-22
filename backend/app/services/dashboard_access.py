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
import re

from sqlalchemy import or_, select

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactRelease, ArtifactShare

VISIBILITIES = ("none", "shared", "internal", "public")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


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
    """The owner, or whoever can view the dashboard's project -- while still
    in the dashboard's organization."""
    from app.core.permission_resolver import principal_belongs_to_org

    if user is None or not await principal_belongs_to_org(db, user, str(artifact.organization_id)):
        return False
    if is_owner(artifact, user):
        return True
    if not artifact.project_id:
        return False
    from app.models.project import Project
    from app.services.project_service import project_service

    project = await db.get(Project, str(artifact.project_id))
    return project is not None and await project_service.user_can_view_project(db, user, project)


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
    # The draft lives in the editing conversation: none once it was deleted
    # (phase 7; "Edit" starts a new one from the published release).
    from app.models.report import Report
    editing = await db.get(Report, str(artifact.report_id)) if artifact.report_id else None
    if editing is None or editing.status == "archived" or editing.deleted_at is not None:
        raise _not_found()
    await assert_read(db, await report_access(db, artifact.report_id), user)
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
