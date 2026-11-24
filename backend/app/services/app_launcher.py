"""The app launcher (/apps): the published artifacts a member can open that
are *apps* — associated with at least one enabled host application.

It is the home page of an app user (a member without ``access_workspace``)
and an "Apps" page for everyone else. It answers exactly as the artifact's own
page would: the candidates come from the dashboards list's ``ReaderScope`` and
every row is re-admitted with ``dashboard_access.admit_reader``, so a card is
never shown for an artifact ``/d/{ref}`` would refuse.

Also "Request full access": an app user asking the organization's member
managers for the workspace.
"""
from __future__ import annotations

import logging
from typing import List

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactRelease
from app.models.host_application import ArtifactHostApplication, HostApplication
from app.models.notification import SOURCE_SHARE, Notification

logger = logging.getLogger(__name__)

MAX_APPS = 200
ACCESS_REQUEST_TYPE = "workspace_access_request"


def _request_key(user_id: str) -> str:
    return f"workspace_access:{user_id}"


def is_app():
    """An artifact some enabled host application may show."""
    return exists(select(ArtifactHostApplication.id).join(
        HostApplication, HostApplication.id == ArtifactHostApplication.host_application_id,
    ).where(
        ArtifactHostApplication.artifact_id == Artifact.id,
        ArtifactHostApplication.deleted_at.is_(None),
        HostApplication.enabled.is_(True),
        HostApplication.deleted_at.is_(None),
    ))


async def organization_has_apps(db: AsyncSession, organization_id: str) -> bool:
    return (await db.execute(
        select(Artifact.id).where(
            Artifact.organization_id == organization_id, Artifact.deleted_at.is_(None),
            Artifact.published_release_id.isnot(None), is_app()).limit(1)
    )).first() is not None


async def _thumbnail_withheld(db, artifact, release, user) -> bool:
    """A thumbnail pictures the publisher's data. Whoever the published
    snapshot itself is withheld from (the artifact runs as each viewer, or
    one of its charts holds one identity's slice) gets the placeholder tile."""
    from app.services.dashboard_access import holds_snapshot
    from app.services.dashboard_data import any_copy_tainted, release_snapshot_withheld

    if await holds_snapshot(db, artifact, release, user):
        return False
    if await release_snapshot_withheld(db, artifact, release):
        return True
    return await any_copy_tainted(db, release)


async def list_apps(db: AsyncSession, user, organization) -> List[dict]:
    from app.services.dashboard_access import admit_reader, is_live
    from app.services.dashboard_list import (
        _thumbnail_url, not_an_older_legacy_version, not_only_failed, reader_scope,
    )

    scope = await reader_scope(db, user, organization)
    artifacts = (await db.execute(
        select(Artifact).where(
            Artifact.organization_id == str(organization.id), Artifact.deleted_at.is_(None),
            Artifact.owner_id.isnot(None), Artifact.published_release_id.isnot(None),
            not_an_older_legacy_version(), not_only_failed(), is_app(),
            or_(Artifact.owner_id == str(user.id), scope.shared_with),
        ).order_by(Artifact.title.asc(), Artifact.id).limit(MAX_APPS)
    )).scalars().all()
    if not artifacts:
        return []

    ids = [str(a.id) for a in artifacts]
    releases = {str(r.id): r for r in (await db.execute(
        select(ArtifactRelease).where(ArtifactRelease.id.in_([a.published_release_id for a in artifacts]))
    )).scalars().all()}
    names: dict[str, list[str]] = {}
    for artifact_id, name in (await db.execute(
        select(ArtifactHostApplication.artifact_id, HostApplication.name).join(
            HostApplication, HostApplication.id == ArtifactHostApplication.host_application_id,
        ).where(ArtifactHostApplication.artifact_id.in_(ids), ArtifactHostApplication.deleted_at.is_(None),
                HostApplication.enabled.is_(True), HostApplication.deleted_at.is_(None))
        .order_by(HostApplication.name)
    )).all():
        names.setdefault(str(artifact_id), []).append(name)

    apps = []
    for artifact in artifacts:
        release = releases.get(str(artifact.published_release_id))
        if release is None or not is_live(release):
            continue
        try:
            await admit_reader(db, artifact, user)
        except AppError:
            continue
        except Exception:  # an HTTPException from a deeper check: not theirs to open
            continue
        thumbnail = None
        if release.thumbnail_path:
            try:
                if not await _thumbnail_withheld(db, artifact, release, user):
                    thumbnail = _thumbnail_url(release.thumbnail_path, artifact_id=str(artifact.id))
            except Exception:
                logger.debug("app thumbnail check failed for %s", artifact.id, exc_info=True)
        apps.append({
            "id": str(artifact.id),
            "ref": artifact.slug or str(artifact.id),
            "title": (artifact.title or "").strip() or None,
            "description": (artifact.description or "").strip() or None,
            "mode": artifact.mode,
            "thumbnail_url": thumbnail,
            "applications": names.get(str(artifact.id), []),
        })
    return apps


# ── Request full access ────────────────────────────────────────────────────

async def _member_manager_ids(db: AsyncSession, organization_id: str) -> List[str]:
    """The users who can change a member's role here: full admins and
    holders of ``manage_members``, directly or through a group."""
    from app.core.permission_resolver import FULL_ADMIN
    from app.models.group import Group
    from app.models.group_membership import GroupMembership
    from app.models.membership import Membership
    from app.models.role import Role
    from app.models.role_assignment import RoleAssignment

    rows = (await db.execute(
        select(RoleAssignment.principal_type, RoleAssignment.principal_id, Role.permissions)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.organization_id == organization_id, RoleAssignment.deleted_at.is_(None),
               Role.deleted_at.is_(None), RoleAssignment.principal_type.in_(("user", "group")))
    )).all()
    users, groups = set(), set()
    for principal_type, principal_id, permissions in rows:
        if not isinstance(permissions, list) or not ({FULL_ADMIN, "manage_members"} & set(permissions)):
            continue
        (users if principal_type == "user" else groups).add(str(principal_id))
    if groups:
        users.update(str(u) for u in (await db.execute(
            select(GroupMembership.user_id).join(Group, Group.id == GroupMembership.group_id).where(
                GroupMembership.group_id.in_(groups), GroupMembership.user_id.isnot(None),
                GroupMembership.deleted_at.is_(None), Group.deleted_at.is_(None))
        )).scalars().all())
    # Legacy admins with no role assignment at all.
    users.update(str(u) for u in (await db.execute(
        select(Membership.user_id).where(Membership.organization_id == organization_id, Membership.role == "admin",
                                         Membership.user_id.isnot(None), Membership.deleted_at.is_(None))
    )).scalars().all())
    if not users:
        return []
    # Still members.
    return [str(u) for u in (await db.execute(
        select(Membership.user_id).where(Membership.organization_id == organization_id,
                                         Membership.user_id.in_(users), Membership.deleted_at.is_(None))
    )).scalars().all()]


async def access_requested(db: AsyncSession, user, organization) -> bool:
    return (await db.execute(
        select(Notification.id).where(
            Notification.organization_id == str(organization.id), Notification.source == SOURCE_SHARE,
            Notification.group_key == _request_key(str(user.id)), Notification.deleted_at.is_(None),
            Notification.dismissed_at.is_(None), Notification.resolved_at.is_(None)).limit(1)
    )).first() is not None


async def request_access(db: AsyncSession, user, organization) -> int:
    """Tell the organization's member managers this app user wants the
    workspace. One live notification per requester and manager, refreshed
    when asked again. Returns how many managers were told."""
    from app.services.inbox_service import InboxService

    managers = await _member_manager_ids(db, str(organization.id))
    name = getattr(user, "name", None) or getattr(user, "email", None) or "A user"
    rows = await InboxService().notify_users(
        db, organization_id=str(organization.id), user_ids=managers, source=SOURCE_SHARE,
        type=ACCESS_REQUEST_TYPE, title=f"{name} asked for full access",
        body=f"{name} can use the apps shared with them and is asking for access to the workspace.",
        actor_user_id=str(user.id), link="/settings/members",
        subject={"kind": "workspace_access_request", "user_id": str(user.id),
                 "i18n": {"actor": name}},
        group_key=_request_key(str(user.id)),
    )
    await db.commit()
    return len(rows)


async def resolve_requests(db: AsyncSession, organization_id: str, user_id: str) -> None:
    """The requester got the workspace (or was removed): close the ask in
    every manager's inbox. Does NOT commit."""
    from datetime import datetime

    from app.models.notification import RESOLUTION_RESOLVED

    for row in (await db.execute(
        select(Notification).where(
            Notification.organization_id == str(organization_id), Notification.source == SOURCE_SHARE,
            Notification.group_key == _request_key(str(user_id)), Notification.deleted_at.is_(None),
            Notification.resolved_at.is_(None))
    )).scalars().all():
        row.resolved_at = datetime.utcnow()
        row.resolution = RESOLUTION_RESOLVED
