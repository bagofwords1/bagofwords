"""A dashboard's sharing and viewer settings, on the dashboard (phase 4).

The owner sets them through /api/dashboards/{id}. The rules are those of
ReportService.set_visibility, today's write path on the conversation, with
two differences: a dashboard is shared only once it is published
(Databricks' rule; docs/design/dashboards-first-class.md), and every person
granted must be a member of the organization. The conversation's own
sharing and settings are never touched: /r keeps reading them until the
data migration.

The schedule (phase 6): a cron, refreshing on view, and who is told after a
scheduled refresh -- the conversation's schedule rules, on the dashboard.
A schedule needs a published release (there is nothing to refresh before);
unscheduling clears the subscribers and leaves refreshing on view as it is.
Saving registers the dashboard's job (dashboard_refresh).
"""
import logging

from sqlalchemy import delete, select

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactRelease, ArtifactShare
from app.models.user import User
from app.services.dashboard_access import published_release

logger = logging.getLogger(__name__)


async def owned_dashboard(db, dashboard_id: str, user: User, organization) -> Artifact:
    """The caller's own dashboard in this organization, if they may still
    read what it is built on (monitoring access, as on the conversation)."""
    from app.services.bow_source_access import assert_read, report_access

    artifact = (await db.execute(
        select(Artifact).where(
            Artifact.id == str(dashboard_id),
            Artifact.organization_id == str(organization.id),
            Artifact.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if artifact is None:
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    if artifact.owner_id is None or str(artifact.owner_id) != str(user.id):
        raise AppError.forbidden()
    release = await published_release(db, artifact)
    access = release.bow_source_access if release is not None else await report_access(db, artifact.report_id)
    await assert_read(db, access, user)
    return artifact


# ── sharing ──────────────────────────────────────────────────────────────

async def _grants(db, artifact) -> tuple[list[str], list[str]]:
    """The grants that still count: people still in the organization, groups
    not deleted. A grant to someone who left (or to an LDAP group synced
    away) stays in the table, opens nothing, and goes on the next save."""
    from app.models.group import Group
    from app.models.membership import Membership

    users = (await db.execute(
        select(ArtifactShare.user_id)
        .join(Membership, (Membership.user_id == ArtifactShare.user_id)
              & (Membership.organization_id == str(artifact.organization_id))
              & Membership.deleted_at.is_(None))
        .where(ArtifactShare.artifact_id == str(artifact.id))
    )).scalars().all()
    groups = (await db.execute(
        select(ArtifactShare.group_id)
        .join(Group, (Group.id == ArtifactShare.group_id)
              & (Group.organization_id == str(artifact.organization_id))
              & Group.deleted_at.is_(None))
        .where(ArtifactShare.artifact_id == str(artifact.id))
    )).scalars().all()
    return sorted({str(u) for u in users}), sorted({str(g) for g in groups})


async def get_sharing(db, artifact) -> dict:
    users, groups = await _grants(db, artifact)
    return {"visibility": artifact.visibility or "none",
            "published": await published_release(db, artifact) is not None,
            "user_ids": users, "group_ids": groups}


async def _check_members(db, organization, user_ids: list[str]) -> None:
    from app.models.membership import Membership

    if not user_ids:
        return
    found = {str(r[0]) for r in (await db.execute(
        select(Membership.user_id).where(
            Membership.user_id.in_(user_ids),
            Membership.organization_id == str(organization.id),
            Membership.deleted_at.is_(None),
        )
    )).all()}
    if any(u not in found for u in user_ids):
        raise AppError.bad_request(ErrorCode.USER_NOT_FOUND)


async def _check_groups(db, organization, group_ids: list[str]) -> None:
    from app.models.group import Group

    if not group_ids:
        return
    found = {str(r[0]) for r in (await db.execute(
        select(Group.id).where(
            Group.id.in_(group_ids),
            Group.organization_id == str(organization.id),
            Group.deleted_at.is_(None),
        )
    )).all()}
    if any(g not in found for g in group_ids):
        raise AppError.bad_request(ErrorCode.GROUP_NOT_FOUND)


async def set_sharing(db, artifact, *, visibility: str, user_ids: list[str] | None,
                      group_ids: list[str] | None, current_user: User, organization) -> dict:
    release = await published_release(db, artifact)
    if visibility != "none" and release is None:
        raise AppError.conflict(ErrorCode.DASHBOARD_SHARE_REQUIRES_PUBLISH)
    if visibility == "public" and release.bow_source_access:
        # A monitoring dashboard is never public (bow_source_access.assert_shareable).
        raise AppError.forbidden()
    user_ids = list(dict.fromkeys(str(u) for u in user_ids)) if user_ids is not None else None
    group_ids = list(dict.fromkeys(str(g) for g in group_ids)) if group_ids is not None else None
    await _check_members(db, organization, user_ids or [])
    await _check_groups(db, organization, group_ids or [])

    old_users, old_groups = await _grants(db, artifact)
    previous = artifact.visibility
    artifact.visibility = visibility
    added_users: list[str] = []
    added_groups: list[str] = []
    if visibility == "shared":
        # A list left out keeps that kind of grant as it is.
        if user_ids is not None:
            await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == str(artifact.id),
                                                         ArtifactShare.user_id.isnot(None)))
            db.add_all(ArtifactShare(artifact_id=str(artifact.id), user_id=u) for u in user_ids)
            added_users = [u for u in user_ids if u not in old_users]
        if group_ids is not None:
            await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == str(artifact.id),
                                                         ArtifactShare.group_id.isnot(None)))
            db.add_all(ArtifactShare(artifact_id=str(artifact.id), group_id=g) for g in group_ids)
            added_groups = [g for g in group_ids if g not in old_groups]
    else:
        await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == str(artifact.id)))
    await db.commit()
    await db.refresh(artifact)

    await _notify(db, artifact, added_users, added_groups, current_user)
    if previous != visibility or user_ids is not None or group_ids is not None:
        await _audit(db, organization, current_user, artifact, "dashboard.sharing_changed",
                     {"visibility": visibility})
    return await get_sharing(db, artifact)


async def _notify(db, artifact, user_ids, group_ids, actor) -> None:
    """New readers get the in-app 'shared with you', linking to /d."""
    recipients = list(user_ids)
    try:
        if group_ids:
            from app.models.group_membership import GroupMembership

            members = (await db.execute(
                select(GroupMembership.user_id).where(
                    GroupMembership.group_id.in_(group_ids),
                    GroupMembership.user_id.isnot(None),
                    GroupMembership.deleted_at.is_(None),
                )
            )).all()
            recipients += [str(u) for (u,) in members]
        skip = {str(actor.id), str(artifact.owner_id)}
        recipients = [u for u in dict.fromkeys(recipients) if u not in skip]
        if recipients:
            from app.services.inbox_service import inbox_service

            await inbox_service.notify_dashboard_share(db, artifact=artifact, user_ids=recipients,
                                                       actor_user=actor)
    except Exception:
        # Sharing must not depend on the notification (as on the conversation).
        logger.warning("dashboard share notification failed", exc_info=True)


async def _audit(db, organization, user, artifact, action, details) -> None:
    try:
        from app.ee.audit.service import audit_service

        await audit_service.log(db=db, organization_id=str(organization.id), action=action,
                                user_id=str(user.id), resource_type="artifact", resource_id=str(artifact.id),
                                details={"title": artifact.title, **details})
    except Exception:
        logger.warning("dashboard audit log failed", exc_info=True)


# ── viewer settings ──────────────────────────────────────────────────────

def get_settings(artifact) -> dict:
    return {"shared_run_identity": artifact.shared_run_identity,
            "include_data_tab": bool(artifact.include_data_tab),
            "chat_enabled": bool(artifact.chat_enabled),
            "chat_data_source_ids": artifact.chat_data_source_ids,
            "chat_model_id": artifact.chat_model_id,
            "cron_schedule": artifact.cron_schedule,
            "refresh_on_view": bool(artifact.refresh_on_view),
            "notification_subscribers": artifact.notification_subscribers}


async def _data_source_ids(db, artifact) -> list[str]:
    """The sources viewers' runs read: the published release's (an "Auto"
    one's: the organization's its code names), else the editing
    conversation's, else all of the organization's."""
    from app.services.dashboard_data import release_run_data_source_ids
    from app.services.viewer_data_policy import _org_fallback_ds_ids, _report_data_source_ids

    release = await published_release(db, artifact)
    if release is not None:
        return await release_run_data_source_ids(db, release)
    ids = await _report_data_source_ids(db, artifact.report_id) if artifact.report_id else []
    return ids or await _org_fallback_ds_ids(db, artifact.organization_id, None)


async def _drop_viewer_results(db, artifact) -> None:
    """Viewers' own runs of this dashboard's published copies, every release:
    they ran under the identity being switched away from."""
    from app.models.query import Query
    from app.models.step import Step
    from app.models.step_user_result import StepUserResult

    copies = (select(Step.id).join(Query, Query.id == Step.query_id)
              .join(ArtifactRelease, ArtifactRelease.id == Query.artifact_release_id)
              .where(ArtifactRelease.artifact_id == str(artifact.id)))
    await db.execute(delete(StepUserResult).where(StepUserResult.step_id.in_(copies)))


async def set_settings(db, artifact, changes: dict, *, current_user: User, organization) -> dict:
    """Apply the settings given; a setting left out (None) stays as it is.
    Everything is checked before anything changes."""
    identity = changes.get("shared_run_identity")
    switch_identity = identity is not None and identity != artifact.shared_run_identity
    if switch_identity and identity == "creator":
        from app.services.viewer_data_policy import _any_rls_relation

        if await _any_rls_relation(db, await _data_source_ids(db, artifact)):
            raise AppError.bad_request(ErrorCode.DASHBOARD_RLS_REQUIRES_VIEWER)

    sources = changes.get("chat_data_source_ids")
    if sources is not None and sources != ["*"]:
        from app.models.data_source import DataSource

        sources = [str(s) for s in sources]
        found = {str(r[0]) for r in (await db.execute(
            select(DataSource.id).where(DataSource.id.in_(sources),
                                        DataSource.organization_id == str(organization.id),
                                        DataSource.deleted_at.is_(None))
        )).all()} if sources else set()
        if any(i not in found for i in sources):
            raise AppError.bad_request(ErrorCode.DATA_SOURCE_NOT_FOUND)

    from app.services.artifact_chat_service import ORG_DEFAULT_CHAT_MODEL

    model = changes.get("chat_model_id")
    if model not in (None, "", ORG_DEFAULT_CHAT_MODEL):
        from app.services.llm_service import LLMService

        await LLMService().validate_model_for_user(db, organization, current_user, model)

    cron = changes.get("cron_schedule")
    unschedule = cron is not None and (not cron.strip() or cron.strip().lower() in {"none", "null", "false"})
    if cron is not None and not unschedule:
        from app.services.dashboard_refresh import parse_cron

        try:
            parse_cron(cron)
        except Exception:
            raise AppError.bad_request(ErrorCode.VALIDATION, "Invalid schedule")
        if await published_release(db, artifact) is None:
            raise AppError.conflict(ErrorCode.DASHBOARD_SCHEDULE_REQUIRES_PUBLISH,
                                    "Publish the dashboard before scheduling it")
        if artifact.mode == "slides":
            raise AppError.bad_request(ErrorCode.VALIDATION, "A published deck is frozen and does not refresh")

    if switch_identity:
        await _drop_viewer_results(db, artifact)
        artifact.shared_run_identity = identity
    if changes.get("include_data_tab") is not None:
        artifact.include_data_tab = bool(changes["include_data_tab"])
    if changes.get("chat_enabled") is not None:
        artifact.chat_enabled = bool(changes["chat_enabled"])
    if sources is not None:
        # ["*"]: inherit the conversation's sources, as on the conversation.
        artifact.chat_data_source_ids = None if sources == ["*"] else sources
    if model is not None:
        artifact.chat_model_id = model or None
    if cron is not None:
        artifact.cron_schedule = None if unschedule else cron.strip()
    if unschedule:
        artifact.notification_subscribers = None
    elif changes.get("notification_subscribers") is not None:
        artifact.notification_subscribers = list(changes["notification_subscribers"])
    if changes.get("refresh_on_view") is not None:
        artifact.refresh_on_view = bool(changes["refresh_on_view"])

    await db.commit()
    await db.refresh(artifact)
    if cron is not None:
        from app.services.dashboard_refresh import org_timezone, register_job

        register_job(str(artifact.id), artifact.cron_schedule, await org_timezone(db, artifact.organization_id))
    await _audit(db, organization, current_user, artifact, "dashboard.settings_changed",
                 {k: v for k, v in changes.items() if v is not None})
    return get_settings(artifact)
