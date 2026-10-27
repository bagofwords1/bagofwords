"""A conversation's old API, acting on its dashboard (phase 9, plan approved
2026-10-22).

Once a conversation has a dashboard, the dashboard holds its sharing and
schedule; the conversation's own columns are the data migration's backup and
are never written again. The conversation endpoints a customer may still call
-- schedule, publish, status, artifact sharing, shares, notify, the report
itself -- keep working and answer in their old shape, through this module:

- the conversation's dashboard is the one its old link leads to (recorded
  by the migration); deleted, the old API refuses rather than act on another;
- a schedule goes to that dashboard, or -- a deck never refreshes -- to the
  page that runs it (else the one with the newest version, as the migration
  chooses); only decks: refused;
- "publish" shares the dashboard publicly (publishing it first when it has no
  release), "unpublish" makes it private (its release stays).

A conversation without a dashboard (widget era) is served as before.
"""
from datetime import datetime

from sqlalchemy import select

from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactShare


async def _live_owned(db, report_id) -> list[Artifact]:
    return list((await db.execute(
        select(Artifact).where(Artifact.report_id == str(report_id), Artifact.deleted_at.is_(None),
                               Artifact.owner_id.isnot(None))
    )).scalars().all())


async def dashboard_of(db, report) -> Artifact | None:
    """The dashboard the conversation's old endpoints act on: the one the
    data migration recorded for its link (a conversation it moved, whose
    columns are the migration's backup). None otherwise -- a conversation
    made since answers its old API as main does (the UI never calls it for a
    dashboard), as does one with no dashboard -- and None when the recorded
    one was deleted: as its link (dashboard_legacy.link_target), the old API
    does not move to another dashboard, which nobody shared that way."""
    return (await db.execute(
        select(Artifact).where(Artifact.legacy_report_id == str(report.id), Artifact.deleted_at.is_(None))
    )).scalar_one_or_none()


async def require(db, report, *, schedule: bool = False) -> Artifact | None:
    """The dashboard to act on; None for a conversation the migration did not
    move (main's path serves it). A moved one with nothing to act on -- only
    decks for a schedule, or its dashboard deleted -- is refused: its columns
    are the migration's backup, never written (nor a hidden conversation-level
    schedule made)."""
    from app.errors import AppError, ErrorCode
    from app.services.dashboard_legacy import is_link_taken_over

    dashboard = await (schedule_dashboard_of(db, report) if schedule else dashboard_of(db, report))
    if dashboard is None and await is_link_taken_over(db, report.id):
        raise AppError.conflict(
            ErrorCode.DASHBOARD_CONVERSATION_MOVED,
            "This conversation's sharing and schedule are on its dashboard; it has none to change here"
            + (" that refreshes" if schedule else "") + ".")
    return dashboard


async def assert_may(db, dashboard: Artifact, user, *needs: str) -> None:
    """The caller's rights on the dashboard, as its own API asks for them.
    The old endpoints are gated by owning the conversation, which stopped
    meaning owning its dashboard once ownership can move (collab01: a
    previous owner keeps the conversation, not the dashboard)."""
    from app.errors import AppError
    from app.services.dashboard_access import capabilities

    if not set(needs) <= await capabilities(db, dashboard, user):
        raise AppError.forbidden()


def share_needs(dashboard: Artifact, visibility: str) -> tuple[str, ...]:
    """What sharing through the old API asks: share, and publish when it
    publishes first (no release yet, a visibility other than private)."""
    from app.services.dashboard_access import CAP_PUBLISH, CAP_SHARE

    publishes = not dashboard.published_release_id and visibility != "none"
    return (CAP_SHARE, CAP_PUBLISH) if publishes else (CAP_SHARE,)


async def schedule_dashboard_of(db, report, dashboard: Artifact | None = None) -> Artifact | None:
    """Where the conversation's schedule is: its dashboard, unless that is a
    deck (never refreshed) -- then the page that already runs it, else the
    page the migration would choose (the one with the newest version); None
    when it has only decks (or no dashboard)."""
    from sqlalchemy import func

    dashboard = dashboard if dashboard is not None else await dashboard_of(db, report)
    if dashboard is None or dashboard.mode != "slides":
        return dashboard
    pages = [a for a in await _live_owned(db, report.id) if a.mode == "page"]
    scheduled = [a for a in pages if (a.cron_schedule or "").strip()]
    if scheduled:
        return scheduled[0]
    newest = dict((await db.execute(
        select(ArtifactVersion.artifact_id, func.max(ArtifactVersion.created_at))
        .where(ArtifactVersion.artifact_id.in_([str(a.id) for a in pages]), ArtifactVersion.deleted_at.is_(None))
        .group_by(ArtifactVersion.artifact_id)
    )).all()) if pages else {}
    pages.sort(key=lambda a: newest.get(str(a.id)) or a.created_at or datetime.min, reverse=True)
    return pages[0] if pages else None


def _shared(dashboard) -> bool:
    return bool(dashboard.published_release_id) and (dashboard.visibility or "none") != "none"


async def _grant_ids(db, dashboard) -> tuple[list[str], list[str]]:
    rows = (await db.execute(
        select(ArtifactShare.user_id, ArtifactShare.group_id).where(ArtifactShare.artifact_id == str(dashboard.id))
    )).all()
    return [str(u) for u, _ in rows if u], [str(g) for _, g in rows if g]


async def report_fields(db, report, dashboard) -> dict:
    """The ReportSchema fields the dashboard now answers for."""
    users, groups = await _grant_ids(db, dashboard)
    fields = {
        "artifact_visibility": dashboard.visibility or "none",
        "cron_schedule": dashboard.cron_schedule,
        "notification_subscribers": dashboard.notification_subscribers,
        "refresh_on_view": bool(dashboard.refresh_on_view),
        "shared_run_identity": dashboard.shared_run_identity or "viewer",
        "include_data_tab": bool(dashboard.include_data_tab),
        "artifact_chat_enabled": bool(dashboard.chat_enabled),
        "artifact_chat_data_source_ids": dashboard.chat_data_source_ids,
        "artifact_chat_model_id": dashboard.chat_model_id,
        "artifact_shared_user_ids": users,
        "artifact_shared_group_ids": groups,
    }
    if report.status != "archived":
        fields["status"] = "published" if _shared(dashboard) else "draft"
    return fields


async def as_report_schema(db, report, schema=None):
    """The report's schema with its dashboard's answers in it (the report's
    own when it has no dashboard)."""
    from app.schemas.report_schema import ReportSchema

    schema = schema if schema is not None else ReportSchema.from_orm(report)
    dashboard = await dashboard_of(db, report)
    if dashboard is not None:
        fields = await report_fields(db, report, dashboard)
        # The schedule is read where it is: on the newest page when the
        # dashboard is a deck (schedule_dashboard_of), else on it.
        scheduled = await schedule_dashboard_of(db, report, dashboard)
        if scheduled is None or scheduled.mode == "slides":
            fields.update(cron_schedule=None, notification_subscribers=None)
        elif str(scheduled.id) != str(dashboard.id):
            fields.update(cron_schedule=scheduled.cron_schedule,
                          notification_subscribers=scheduled.notification_subscribers,
                          refresh_on_view=bool(scheduled.refresh_on_view))
        for name, value in fields.items():
            if name in type(schema).model_fields:
                setattr(schema, name, value)
    return schema


async def share(db, dashboard, *, visibility: str, user_ids, group_ids, current_user, organization) -> None:
    """Share the dashboard as asked; with no release yet and a visibility
    other than private, save the sharing and publish it (decision 3)."""
    from app.core import fire_and_forget
    from app.services import dashboard_publish, dashboard_settings_service

    if visibility == "public" and dashboard.visibility != "public":
        # Checked before the publish below starts, so a refused public share
        # does not publish either (set_sharing checks it again).
        from app.services.public_sharing_policy import assert_public_sharing_allowed
        await assert_public_sharing_allowed(db, dashboard.organization_id)
    unpublished = not dashboard.published_release_id and visibility != "none"
    release = None
    if unpublished:
        # Publishing first: when it is refused (no finished version, one
        # already running...), nothing is shared and nobody is told.
        release = await dashboard_publish.start_publish(db, dashboard, current_user, organization)
    try:
        await dashboard_settings_service.set_sharing(
            db, dashboard, visibility=visibility, user_ids=user_ids, group_ids=group_ids,
            current_user=current_user, organization=organization, before_publish=unpublished)
    finally:
        if release is not None:
            # The publish runs even when the sharing was refused (someone not
            # in the organization...): it was started, and is the owner's.
            fire_and_forget.spawn(dashboard_publish.run_publish(str(release.id)))


async def set_published(db, dashboard, published: bool, *, current_user, organization) -> None:
    """The old publish toggle: public, or private (decision 4: the release
    stays, readers lose access)."""
    await share(db, dashboard, visibility="public" if published else "none", user_ids=None, group_ids=None,
                current_user=current_user, organization=organization)


def is_published(dashboard) -> bool:
    return _shared(dashboard)


async def visibility_answer(db, dashboard, share_type: str) -> dict:
    """PUT /reports/{id}/visibility/artifact's answer, from the dashboard."""
    users, groups = await _grant_ids(db, dashboard)
    return {
        "share_type": share_type,
        "visibility": dashboard.visibility or "none",
        "shared_user_ids": users,
        "shared_group_ids": groups,
        "shared_run_identity": dashboard.shared_run_identity or "viewer",
        "include_data_tab": bool(dashboard.include_data_tab),
        "artifact_chat_enabled": bool(dashboard.chat_enabled),
        "artifact_chat_data_source_ids": dashboard.chat_data_source_ids,
        "artifact_chat_model_id": dashboard.chat_model_id,
        "conversation_share_token": None,
    }


async def shares_answer(db, dashboard) -> list[dict]:
    """GET /reports/{id}/shares/artifact's answer, from the dashboard."""
    from app.models.group import Group
    from app.models.user import User

    out = []
    rows = (await db.execute(
        select(ArtifactShare).where(ArtifactShare.artifact_id == str(dashboard.id))
    )).scalars().all()
    for s in rows:
        user = await db.get(User, str(s.user_id)) if s.user_id else None
        group = await db.get(Group, str(s.group_id)) if s.group_id else None
        out.append({
            "id": str(s.id), "principal_type": "group" if s.group_id else "user",
            "user_id": str(s.user_id) if s.user_id else None,
            "user_name": user.name if user else None, "user_email": user.email if user else None,
            "group_id": str(s.group_id) if s.group_id else None, "group_name": group.name if group else None,
            "member_count": None, "share_type": "artifact",
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return out


async def refresh_on_view_dashboard(db, report, artifact_id: str | None, user=None) -> Artifact | None:
    """The dashboard whose "refresh when opened" governs the editor's refresh:
    the one open in it (a version id from the editor) -- named by one who
    may edit it, and not deleted: a reader does not choose which setting
    lets them run the owner's queries -- else the conversation's dashboard."""
    if artifact_id and user is not None:
        from app.services.dashboard_access import can_edit

        version = await db.get(ArtifactVersion, str(artifact_id))
        target = await db.get(Artifact, str(version.artifact_id)) if version is not None else \
            await db.get(Artifact, str(artifact_id))
        if (target is not None and target.deleted_at is None and str(target.report_id) == str(report.id)
                and target.owner_id and await can_edit(db, target, user)):
            return target
    return await dashboard_of(db, report)
