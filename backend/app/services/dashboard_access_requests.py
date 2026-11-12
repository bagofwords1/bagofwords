"""Request-to-view a dashboard, answered by its editors (access requests).

A member who opens a published dashboard link they cannot see asks its editors
for view access with one click (the no-access page). The owner and the editors
added by name are notified in-app and by email; any editor answers from the
share settings. First answer wins: approving adds the requester as a viewer
(and switches a private dashboard to "Specific people"); denying is silent.

Rules enforced here (the product spec):
- Only for dashboards whose visibility is 'none' (private) or 'shared' -- a
  whole-org or public dashboard needs no request.
- One open (pending) request per (artifact, requester); a denied requester may
  ask again only after DENIAL_COOLDOWN_DAYS.
- The org setting `allow_access_requests` must be on for a NEW request; already
  sent requests stay answerable when it is turned off.
- Editors (CAP_SHARE) approve or deny; org admins get no extra approval rights
  (the route gates on CAP_SHARE, which an admin does not hold).
- Who is notified: the owner and editors granted by name. Group-only editors
  see requests in the share settings but are not notified (no spamming a big
  group).

The no-access response is deliberately thin (blocked_state): the owner's
display name, whether requests are allowed, whether one is already pending,
and whether anyone can approve -- never the dashboard's title.
"""
import html
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError, ErrorCode
from app.models.dashboard_access_request import (
    DENIAL_COOLDOWN_DAYS,
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_PENDING,
    DashboardAccessRequest,
)

logger = logging.getLogger(__name__)

# Visibilities for which a request makes sense: private and specific-people.
# 'internal' (whole org) and 'public' open to the asker already.
REQUESTABLE_VISIBILITIES = ("none", "shared")

MAX_NOTE_LENGTH = 1000


def _display_name(user) -> str:
    return (getattr(user, "name", None) or getattr(user, "email", None) or "Someone")


async def _is_member(db, user_id, organization_id) -> bool:
    from app.models.membership import Membership

    return (await db.execute(
        select(Membership.id).where(
            Membership.user_id == str(user_id),
            Membership.organization_id == str(organization_id),
            Membership.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none() is not None


async def _active_member_ids(db, organization_id, user_ids: list[str]) -> set[str]:
    from app.models.membership import Membership

    if not user_ids:
        return set()
    rows = (await db.execute(
        select(Membership.user_id).where(
            Membership.user_id.in_([str(u) for u in user_ids]),
            Membership.organization_id == str(organization_id),
            Membership.deleted_at.is_(None),
        )
    )).all()
    return {str(u) for (u,) in rows}


async def notify_recipient_ids(db, artifact) -> list[str]:
    """Who is told about a request: the owner (while a member) and the editors
    granted by name (a role='edit' grant to a user, while a member). Group-only
    editors are deliberately left out so a large group is not spammed."""
    from app.models.artifact_release import ArtifactShare

    candidates: list[str] = []
    if artifact.owner_id:
        candidates.append(str(artifact.owner_id))
    named_editors = (await db.execute(
        select(ArtifactShare.user_id).where(
            ArtifactShare.artifact_id == str(artifact.id),
            ArtifactShare.role == "edit",
            ArtifactShare.user_id.isnot(None),
        )
    )).all()
    candidates += [str(u) for (u,) in named_editors]
    active = await _active_member_ids(db, artifact.organization_id, candidates)
    # Preserve order, owner first, de-duped.
    return [u for u in dict.fromkeys(candidates) if u in active]


async def has_active_approvers(db, artifact) -> bool:
    """Whether anyone can still approve a request for this dashboard: the owner
    (while a member), a named or group editor who resolves to an active member,
    or -- when the dashboard belongs to a project -- that project's editors.

    Used by the no-access page ("No one can approve requests for this dashboard
    right now") and to refuse a request no one could ever answer."""
    from app.models.artifact_release import ArtifactShare
    from app.models.group_membership import GroupMembership
    from app.models.membership import Membership

    if artifact.owner_id and await _is_member(db, artifact.owner_id, artifact.organization_id):
        return True
    # A project's viewers can edit the dashboard (dashboard_access.can_edit), so
    # a live project means there may be approvers we don't enumerate per-user.
    if artifact.project_id:
        from app.models.project import Project

        project = await db.get(Project, str(artifact.project_id))
        if project is not None and project.deleted_at is None:
            return True
    # A named editor still in the organization.
    named = (await db.execute(
        select(ArtifactShare.user_id)
        .join(Membership, (Membership.user_id == ArtifactShare.user_id)
              & (Membership.organization_id == str(artifact.organization_id))
              & Membership.deleted_at.is_(None))
        .where(ArtifactShare.artifact_id == str(artifact.id), ArtifactShare.role == "edit",
               ArtifactShare.user_id.isnot(None)).limit(1)
    )).scalar_one_or_none()
    if named is not None:
        return True
    # A group editor whose group still has an active member.
    group_editor = (await db.execute(
        select(ArtifactShare.id)
        .join(GroupMembership, GroupMembership.group_id == ArtifactShare.group_id)
        .join(Membership, (Membership.user_id == GroupMembership.user_id)
              & (Membership.organization_id == str(artifact.organization_id))
              & Membership.deleted_at.is_(None))
        .where(ArtifactShare.artifact_id == str(artifact.id), ArtifactShare.role == "edit",
               ArtifactShare.group_id.isnot(None), GroupMembership.deleted_at.is_(None)).limit(1)
    )).scalar_one_or_none()
    return group_editor is not None


async def _latest_request(db, artifact_id, user_id) -> DashboardAccessRequest | None:
    return (await db.execute(
        select(DashboardAccessRequest).where(
            DashboardAccessRequest.artifact_id == str(artifact_id),
            DashboardAccessRequest.requester_user_id == str(user_id),
            DashboardAccessRequest.deleted_at.is_(None),
        ).order_by(DashboardAccessRequest.created_at.desc()).limit(1)
    )).scalar_one_or_none()


def _cooldown_retry_at(req: DashboardAccessRequest | None) -> datetime | None:
    """When a denied requester may ask again, or None if they may ask now."""
    if req is None or req.status != STATUS_DENIED:
        return None
    base = req.decided_at or req.updated_at or req.created_at
    if base is None:
        return None
    retry_at = base + timedelta(days=DENIAL_COOLDOWN_DAYS)
    return retry_at if retry_at > datetime.utcnow() else None


async def _has_view_access(db, artifact, user) -> bool:
    from app.services import dashboard_access

    return dashboard_access.CAP_VIEW in await dashboard_access.capabilities(db, artifact, user)


async def blocked_state(db, artifact, user) -> dict:
    """The thin payload the no-access page needs. Never the dashboard's title.

    `user` is the signed-in member who could not open the dashboard."""
    from app.models.user import User

    owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
    requests_enabled = await _access_requests_enabled(db, artifact.organization_id)
    eligible = (artifact.visibility or "none") in REQUESTABLE_VISIBILITIES
    approvers = await has_active_approvers(db, artifact)
    latest = await _latest_request(db, artifact.id, user.id) if user is not None else None
    retry_at = _cooldown_retry_at(latest)
    return {
        "owner_name": _display_name(owner) if owner else None,
        "requests_enabled": requests_enabled,
        "eligible": eligible,
        "has_approvers": approvers,
        "pending": latest is not None and latest.status == STATUS_PENDING,
        "retry_at": retry_at.isoformat() if retry_at else None,
    }


async def _access_requests_enabled(db, organization_id) -> bool:
    from app.services.access_requests_policy import access_requests_allowed

    return await access_requests_allowed(db, organization_id)


async def create_request(db, artifact, user, note, organization) -> DashboardAccessRequest:
    """A member asks to view the dashboard. Validates every rule, stores the
    request, and notifies the owner and named editors (in-app + email)."""
    from app.services.access_requests_policy import assert_access_requests_allowed

    if not await _is_member(db, user.id, artifact.organization_id):
        raise AppError.forbidden()
    if (artifact.visibility or "none") not in REQUESTABLE_VISIBILITIES:
        raise AppError.bad_request(ErrorCode.ACCESS_REQUEST_NOT_ALLOWED,
                                   "This dashboard does not need an access request")
    if await _has_view_access(db, artifact, user):
        raise AppError.bad_request(ErrorCode.ACCESS_REQUEST_ALREADY_HAS_ACCESS,
                                   "You already have access to this dashboard")
    await assert_access_requests_allowed(db, artifact.organization_id)
    if not await has_active_approvers(db, artifact):
        raise AppError.forbidden(ErrorCode.ACCESS_REQUEST_NO_APPROVERS,
                                 "No one can approve requests for this dashboard right now")

    latest = await _latest_request(db, artifact.id, user.id)
    if latest is not None and latest.status == STATUS_PENDING:
        raise AppError.conflict(ErrorCode.ACCESS_REQUEST_PENDING, "Your request is pending")
    retry_at = _cooldown_retry_at(latest)
    if retry_at is not None:
        raise AppError.forbidden(ErrorCode.ACCESS_REQUEST_TOO_SOON,
                                 "You can ask again later", retry_at=retry_at.isoformat())

    clean_note = (note or "").strip()[:MAX_NOTE_LENGTH] or None
    req = DashboardAccessRequest(
        organization_id=str(artifact.organization_id),
        artifact_id=str(artifact.id),
        requester_user_id=str(user.id),
        note=clean_note,
        status=STATUS_PENDING,
    )
    db.add(req)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request won the partial unique index (one pending per
        # artifact+requester): report it as the typed "pending" conflict.
        await db.rollback()
        raise AppError.conflict(ErrorCode.ACCESS_REQUEST_PENDING, "Your request is pending")
    await db.refresh(req)

    await _notify_editors_of_request(db, artifact, req, user)
    return req


async def _notify_editors_of_request(db, artifact, req, requester) -> None:
    """In-app + email to the owner and named editors: '<name> asked to view
    <dashboard>'. The group_key ties every editor's row to this request so one
    answer can resolve them all."""
    recipients = await notify_recipient_ids(db, artifact)
    recipients = [u for u in recipients if u != str(requester.id)]
    if not recipients:
        return
    asker = _display_name(requester)
    dash = artifact.title or "a dashboard"
    link = _editor_link(artifact)
    try:
        from app.services.inbox_service import inbox_service

        await inbox_service.notify_users(
            db, organization_id=str(artifact.organization_id), user_ids=recipients,
            source="share", type="access_request",
            title=f"{asker} asked to view {dash}",
            body=(f'{asker} asked to view "{dash}".'
                  + (f' "{req.note}"' if req.note else "")),
            actor_user_id=str(requester.id),
            link=link,
            subject={"kind": "access_request", "artifact_id": str(artifact.id),
                     "request_id": str(req.id),
                     "i18n": {"actor": asker, "dashboard": dash, "note": req.note or ""}},
            group_key=f"access_request:{req.id}",
        )
    except Exception:
        logger.warning("access-request in-app notification failed", exc_info=True)

    await _email_editors(db, artifact, req, asker, dash, recipients)


def _editor_link(artifact) -> str:
    """Where an editor's notification lands: the dashboard's editing workspace
    with the share settings open, or the dashboard page as a fallback."""
    if artifact.report_id:
        return f"/reports/{artifact.report_id}?share=access_requests"
    return f"/d/{artifact.id}"


def _base_url() -> str:
    from app.settings.config import settings

    return (getattr(settings.bow_config, "base_url", None) or "http://localhost:3000").rstrip("/")


async def _email_editors(db, artifact, req, asker, dash, recipient_ids) -> None:
    """Email each notified editor, best-effort (never raises into the caller).
    Reuses the request's session -- the request row is already committed."""
    try:
        from app.models.user import User
        from app.services.email_client_resolver import is_outbound_available
        from app.services.notification_service import notification_service

        if not await is_outbound_available(db, str(artifact.organization_id), purpose="system"):
            return
        safe_asker = html.escape(asker)
        safe_dash = html.escape(dash)
        note_html = (f'<br /><br /><em>"{html.escape(req.note)}"</em>' if req.note else "")
        link = f"{_base_url()}{_editor_link(artifact)}"
        subject = f"{asker} asked to view {dash}"
        body = (
            f"{safe_asker} asked to view <strong>{safe_dash}</strong> on BOW.{note_html}<br /><br />"
            f"<a href=\"{link}\" style=\"display:inline-block;padding:10px 18px;background:#111827;"
            f"color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;\">Review the request</a>"
        )
        emails = [e for (e,) in (await db.execute(
            select(User.email).where(User.id.in_([str(u) for u in recipient_ids]),
                                     User.email.isnot(None))
        )).all() if e]
        for email in emails:
            await notification_service.send_custom_email(
                recipients=[email], subject=subject, body=body, subtype="html",
                retries=2, timeout=15, db=db,
                organization_id=str(artifact.organization_id), purpose="system")
    except Exception:
        logger.warning("access-request email failed", exc_info=True)


async def list_for_editor(db, artifact) -> dict:
    """Pending requests plus the context the approval dialog needs: whether the
    dashboard is private (approving switches it to 'shared') and whether it runs
    with its creator's data access (approving lets the viewer see what the
    creator sees)."""
    from app.models.user import User

    rows = (await db.execute(
        select(DashboardAccessRequest).where(
            DashboardAccessRequest.artifact_id == str(artifact.id),
            DashboardAccessRequest.status == STATUS_PENDING,
            DashboardAccessRequest.deleted_at.is_(None),
        ).order_by(DashboardAccessRequest.created_at.asc())
    )).scalars().all()
    requests = []
    for r in rows:
        requester = await db.get(User, str(r.requester_user_id))
        requests.append({
            "id": str(r.id),
            "requester_name": _display_name(requester) if requester else None,
            "requester_email": getattr(requester, "email", None) if requester else None,
            "note": r.note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    owner = await db.get(User, str(artifact.owner_id)) if artifact.owner_id else None
    return {
        "requests": requests,
        "is_private": (artifact.visibility or "none") == "none",
        "runs_as_creator": (artifact.shared_run_identity or "viewer") == "creator",
        "creator_name": _display_name(owner) if owner else None,
    }


async def get_request(db, request_id, organization) -> DashboardAccessRequest:
    req = (await db.execute(
        select(DashboardAccessRequest).where(
            DashboardAccessRequest.id == str(request_id),
            DashboardAccessRequest.organization_id == str(organization.id),
            DashboardAccessRequest.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if req is None:
        raise AppError.not_found(ErrorCode.ACCESS_REQUEST_NOT_FOUND)
    return req


async def decide(db, artifact, req, user, *, approve: bool) -> dict:
    """An editor approves or denies. First answer wins: a request already
    answered is refused. Approving adds the requester as a viewer (switching a
    private dashboard to 'shared') and notifies them; denying is silent. Either
    way every editor's notification for this request is resolved."""
    if req.status != STATUS_PENDING:
        raise AppError.conflict(ErrorCode.ACCESS_REQUEST_ALREADY_DECIDED,
                                "This request was already answered")
    # Atomic claim: only the writer that flips pending -> decided proceeds, so
    # two editors answering at once cannot both win (first answer wins).
    claimed = await db.execute(
        update(DashboardAccessRequest)
        .where(DashboardAccessRequest.id == str(req.id),
               DashboardAccessRequest.status == STATUS_PENDING)
        .values(status=STATUS_APPROVED if approve else STATUS_DENIED,
                decided_by_user_id=str(user.id), decided_at=datetime.utcnow())
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise AppError.conflict(ErrorCode.ACCESS_REQUEST_ALREADY_DECIDED,
                                "This request was already answered")

    if approve:
        await _grant_viewer(db, artifact, req.requester_user_id)
    await db.commit()
    await db.refresh(req)

    if approve:
        await _notify_requester_granted(db, artifact, req, user)
    await _resolve_editor_notifications(db, req)
    return {"id": str(req.id), "status": req.status}


async def _grant_viewer(db, artifact, requester_user_id) -> None:
    """Add the requester as a viewer. A private dashboard becomes 'shared' so
    the grant counts (view grants apply only while visibility is 'shared')."""
    from app.models.artifact_release import ArtifactShare

    if (artifact.visibility or "none") == "none":
        artifact.visibility = "shared"
    existing = (await db.execute(
        select(ArtifactShare).where(
            ArtifactShare.artifact_id == str(artifact.id),
            ArtifactShare.user_id == str(requester_user_id),
        )
    )).scalar_one_or_none()
    if existing is None:
        db.add(ArtifactShare(artifact_id=str(artifact.id), user_id=str(requester_user_id), role="view"))
    # An existing grant (e.g. 'edit') is left as it is -- it already grants more.


async def _notify_requester_granted(db, artifact, req, approver) -> None:
    """The requester is told their request was approved, with a link to the
    dashboard (in-app + email). Denials send nothing."""
    dash = artifact.title or "a dashboard"
    link = f"/d/{artifact.id}"
    try:
        from app.services.inbox_service import inbox_service

        await inbox_service.notify_users(
            db, organization_id=str(artifact.organization_id),
            user_ids=[str(req.requester_user_id)],
            source="share", type="access_granted",
            title=f"You can now view {dash}",
            body=f'Your request to view "{dash}" was approved.',
            actor_user_id=str(approver.id),
            link=link,
            subject={"kind": "access_granted", "artifact_id": str(artifact.id),
                     "i18n": {"dashboard": dash}},
            group_key=f"access_granted:{artifact.id}:{req.requester_user_id}",
        )
    except Exception:
        logger.warning("access-granted in-app notification failed", exc_info=True)

    try:
        from app.models.user import User
        from app.services.email_client_resolver import is_outbound_available
        from app.services.notification_service import notification_service

        if not await is_outbound_available(db, str(artifact.organization_id), purpose="system"):
            return
        safe_dash = html.escape(dash)
        url = f"{_base_url()}{link}"
        subject = f"You can now view {dash}"
        body = (
            f"Your request to view <strong>{safe_dash}</strong> on BOW was approved.<br /><br />"
            f"<a href=\"{url}\" style=\"display:inline-block;padding:10px 18px;background:#111827;"
            f"color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;\">Open {safe_dash}</a>"
        )
        requester = await db.get(User, str(req.requester_user_id))
        email = getattr(requester, "email", None)
        if email:
            await notification_service.send_custom_email(
                recipients=[email], subject=subject, body=body, subtype="html",
                retries=2, timeout=15, db=db,
                organization_id=str(artifact.organization_id), purpose="system")
    except Exception:
        logger.warning("access-granted email failed", exc_info=True)


async def _resolve_editor_notifications(db, req) -> None:
    """Dismiss every editor's in-app notification for this request once it is
    answered, so it closes for all editors at once (first answer wins)."""
    from app.models.notification import Notification

    try:
        rows = (await db.execute(
            select(Notification).where(and_(
                Notification.group_key == f"access_request:{req.id}",
                Notification.dismissed_at.is_(None),
                Notification.deleted_at.is_(None),
            ))
        )).scalars().all()
        now = datetime.utcnow()
        for n in rows:
            n.dismissed_at = now
            if n.read_at is None:
                n.read_at = now
        if rows:
            await db.commit()
    except Exception:
        logger.warning("resolving access-request notifications failed", exc_info=True)
