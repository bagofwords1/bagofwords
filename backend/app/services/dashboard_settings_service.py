"""A dashboard's sharing and viewer settings, on the dashboard (phase 4).

The owner sets them through /api/artifacts/{id}. The rules are those of
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
from sqlalchemy.exc import IntegrityError

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactRelease, ArtifactShare
from app.models.user import User
from app.services.dashboard_access import published_release

logger = logging.getLogger(__name__)


async def dashboard_for(db, dashboard_id: str, user: User, organization, need: str) -> Artifact:
    """The dashboard, if `user` holds the capability `need` on it
    (dashboard_access.capabilities, collab01) and may still read what it is
    built on (monitoring access, as on the conversation). A publish refused
    only because the dashboard runs as its creator says so."""
    from app.services import dashboard_access
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
    caps = await dashboard_access.capabilities(db, artifact, user)
    if need not in caps:
        if (need == dashboard_access.CAP_PUBLISH and dashboard_access.CAP_EDIT in caps
                and (artifact.shared_run_identity or "viewer") == "creator"):
            raise AppError.forbidden(ErrorCode.DASHBOARD_CREATOR_PUBLISH_OWNER_ONLY,
                                     "Only the owner can publish a dashboard that runs with the owner's credentials")
        raise AppError.forbidden()
    release = await published_release(db, artifact)
    access = release.bow_source_access if release is not None else (
        await report_access(db, artifact.report_id) if artifact.report_id else None)
    await assert_read(db, access, user)
    return artifact


async def owned_dashboard(db, dashboard_id: str, user: User, organization) -> Artifact:
    """The caller's own dashboard: what only the owner may do."""
    from app.services.dashboard_access import CAP_RUN_IDENTITY

    return await dashboard_for(db, dashboard_id, user, organization, CAP_RUN_IDENTITY)


# ── sharing ──────────────────────────────────────────────────────────────

async def _grants(db, artifact) -> dict:
    """The grants that still count, by role: people still in the
    organization, groups not deleted. A grant to someone who left (or to an
    LDAP group synced away) stays in the table, opens nothing, and goes on
    the next save. {"view": (users, groups), "edit": (users, groups)}."""
    from app.models.group import Group
    from app.models.membership import Membership

    users = (await db.execute(
        select(ArtifactShare.user_id, ArtifactShare.role)
        .join(Membership, (Membership.user_id == ArtifactShare.user_id)
              & (Membership.organization_id == str(artifact.organization_id))
              & Membership.deleted_at.is_(None))
        .where(ArtifactShare.artifact_id == str(artifact.id))
    )).all()
    groups = (await db.execute(
        select(ArtifactShare.group_id, ArtifactShare.role)
        .join(Group, (Group.id == ArtifactShare.group_id)
              & (Group.organization_id == str(artifact.organization_id))
              & Group.deleted_at.is_(None))
        .where(ArtifactShare.artifact_id == str(artifact.id))
    )).all()

    def pick(rows, role):
        return sorted({str(p) for p, r in rows if (r or "view") == role})
    return {"view": (pick(users, "view"), pick(groups, "view")),
            "edit": (pick(users, "edit"), pick(groups, "edit"))}


async def get_sharing(db, artifact) -> dict:
    grants = await _grants(db, artifact)
    users, groups = grants["view"]
    editor_users, editor_groups = grants["edit"]
    return {"visibility": artifact.visibility or "none",
            "published": await published_release(db, artifact) is not None,
            "user_ids": users, "group_ids": groups,
            "editor_user_ids": editor_users, "editor_group_ids": editor_groups,
            "owner_id": str(artifact.owner_id) if artifact.owner_id else None,
            "project_id": str(artifact.project_id) if artifact.project_id else None}


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
                      group_ids: list[str] | None, current_user: User, organization,
                      before_publish: bool = False, editor_user_ids: list[str] | None = None,
                      editor_group_ids: list[str] | None = None) -> dict:
    """`before_publish`: the conversation's old publish call (phase 9) saves
    the sharing and then publishes; /d serves only a ready release, so it
    takes effect when that one is ready.

    Roles (collab01): view grants count while the visibility is 'shared';
    edit grants stand whatever the visibility, before the first publish too
    (people edit a draft together before anyone else sees it). Someone in
    both lists is an editor; the owner is never a grant. Only the owner makes
    a dashboard public; editors share like the owner."""
    from app.services.dashboard_access import CAP_MAKE_PUBLIC, capabilities

    caps = await capabilities(db, artifact, current_user)
    release = await published_release(db, artifact)
    if visibility != "none" and release is None and not before_publish:
        raise AppError.conflict(ErrorCode.DASHBOARD_SHARE_REQUIRES_PUBLISH)
    if visibility == "public" and artifact.visibility != "public":
        # No new public links while the organization has them off. An
        # unchanged 'public' re-sent with other edits opens nothing new.
        from app.services.public_sharing_policy import assert_public_sharing_allowed
        await assert_public_sharing_allowed(db, artifact.organization_id)
    if visibility == "public" and artifact.visibility != "public" and CAP_MAKE_PUBLIC not in caps:
        raise AppError.forbidden(ErrorCode.DASHBOARD_PUBLIC_OWNER_ONLY,
                                 "Only the owner can make this dashboard public")
    if visibility == "public":
        if release is not None:
            monitoring = release.bow_source_access
        else:
            from app.services.bow_source_access import report_access
            monitoring = await report_access(db, artifact.report_id) if artifact.report_id else None
        if monitoring:
            # A monitoring dashboard is never public (bow_source_access.assert_shareable).
            raise AppError.forbidden()
    owner = str(artifact.owner_id) if artifact.owner_id else None

    def clean(ids):
        return [i for i in dict.fromkeys(str(x) for x in ids) if i != owner] if ids is not None else None

    user_ids, group_ids = clean(user_ids), clean(group_ids)
    editor_user_ids, editor_group_ids = clean(editor_user_ids), clean(editor_group_ids)
    await _check_members(db, organization, [*(user_ids or []), *(editor_user_ids or [])])
    await _check_groups(db, organization, [*(group_ids or []), *(editor_group_ids or [])])

    old = await _grants(db, artifact)
    old_users = set(old["view"][0]) | set(old["edit"][0])
    old_groups = set(old["view"][1]) | set(old["edit"][1])
    previous = artifact.visibility
    artifact.visibility = visibility
    aid = str(artifact.id)

    async def replace(column, role, ids):
        await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == aid, column.isnot(None),
                                                     ArtifactShare.role == role))
        for i in ids:
            db.add(ArtifactShare(artifact_id=aid, role=role, **{column.key: i}))

    # Editors first: someone in both lists is an editor, never twice.
    if editor_user_ids is not None:
        await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == aid,
                                                     ArtifactShare.user_id.in_(editor_user_ids)))
        await replace(ArtifactShare.user_id, "edit", editor_user_ids)
    if editor_group_ids is not None:
        await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == aid,
                                                     ArtifactShare.group_id.in_(editor_group_ids)))
        await replace(ArtifactShare.group_id, "edit", editor_group_ids)
    await db.flush()
    editors_now = await _grants(db, artifact) if (editor_user_ids is None or editor_group_ids is None) else None
    edit_users = set(editor_user_ids if editor_user_ids is not None else editors_now["edit"][0])
    edit_groups = set(editor_group_ids if editor_group_ids is not None else editors_now["edit"][1])
    if visibility == "shared":
        # A list left out keeps that kind of grant as it is.
        if user_ids is not None:
            await replace(ArtifactShare.user_id, "view", [u for u in user_ids if u not in edit_users])
        if group_ids is not None:
            await replace(ArtifactShare.group_id, "view", [g for g in group_ids if g not in edit_groups])
    else:
        await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == aid,
                                                     ArtifactShare.role == "view"))
    await db.commit()
    await db.refresh(artifact)

    now = await _grants(db, artifact)
    new_users = set(now["view"][0]) | set(now["edit"][0])
    new_groups = set(now["view"][1]) | set(now["edit"][1])
    await _notify(db, artifact, sorted(new_users - old_users), sorted(new_groups - old_groups), current_user)
    if (previous != visibility or user_ids is not None or group_ids is not None
            or editor_user_ids is not None or editor_group_ids is not None):
        await _audit(db, organization, current_user, artifact, "dashboard.sharing_changed",
                     {"visibility": visibility, "editors": len(now["edit"][0]) + len(now["edit"][1])})
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
            "bow_credit": bool(getattr(artifact, "bow_credit", True)),
            "chat_enabled": bool(artifact.chat_enabled),
            "chat_data_source_ids": artifact.chat_data_source_ids,
            "chat_model_id": artifact.chat_model_id,
            "cron_schedule": artifact.cron_schedule,
            "refresh_on_view": bool(artifact.refresh_on_view),
            "notification_subscribers": artifact.notification_subscribers,
            "slug": artifact.slug,
            "notes": artifact.notes}


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


async def _drop_viewer_results(db, artifact, *, executed_as: str | None = None) -> None:
    """Viewers' own runs of this dashboard's published copies, every release
    (kept ones too, so a roll back serves none of them): they ran under the
    identity being switched away from. `executed_as`: only those run so
    (an ownership transfer drops the runs on the previous owner's
    credentials)."""
    from app.models.query import Query
    from app.models.step import Step
    from app.models.step_user_result import StepUserResult

    copies = (select(Step.id).join(Query, Query.id == Step.query_id)
              .join(ArtifactRelease, ArtifactRelease.id == Query.artifact_release_id)
              .where(ArtifactRelease.artifact_id == str(artifact.id)))
    stale = delete(StepUserResult).where(StepUserResult.step_id.in_(copies))
    if executed_as is not None:
        stale = stale.where(StepUserResult.executed_as == executed_as)
    await db.execute(stale)


async def check_settings(db, artifact, changes: dict, *, current_user: User, organization) -> None:
    """set_settings' checks, changing nothing: refused here, set_settings
    would refuse. A caller that saves something else with the settings (the
    conversation's old sharing call, phase 9) checks them first."""
    identity = changes.get("shared_run_identity")
    switch_identity = identity is not None and identity != artifact.shared_run_identity
    if switch_identity:
        # Viewers would run with the owner's credentials: the owner's call
        # alone (collab01).
        from app.services.dashboard_access import CAP_RUN_IDENTITY, capabilities

        if CAP_RUN_IDENTITY not in await capabilities(db, artifact, current_user):
            raise AppError.forbidden()
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
        # Published or not (phase 8): before publishing the schedule refreshes
        # the draft, as a conversation's does today.
        if artifact.mode == "slides":
            raise AppError.bad_request(ErrorCode.VALIDATION, "A deck does not refresh on a schedule")


async def set_settings(db, artifact, changes: dict, *, current_user: User, organization) -> dict:
    """Apply the settings given; a setting left out (None) stays as it is.
    Everything is checked before anything changes."""
    await check_settings(db, artifact, changes, current_user=current_user, organization=organization)
    identity = changes.get("shared_run_identity")
    switch_identity = identity is not None and identity != artifact.shared_run_identity
    sources = changes.get("chat_data_source_ids")
    if sources is not None and sources != ["*"]:
        sources = [str(s) for s in sources]
    model = changes.get("chat_model_id")
    cron = changes.get("cron_schedule")
    unschedule = cron is not None and (not cron.strip() or cron.strip().lower() in {"none", "null", "false"})

    if switch_identity:
        await _drop_viewer_results(db, artifact)
        artifact.shared_run_identity = identity
    if changes.get("include_data_tab") is not None:
        artifact.include_data_tab = bool(changes["include_data_tab"])
    if changes.get("bow_credit") is not None:
        artifact.bow_credit = bool(changes["bow_credit"])
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
    if changes.get("slug") is not None:
        from app.services.dashboard_slug import set_slug

        await set_slug(db, artifact, changes["slug"])
    if changes.get("notes") is not None:
        artifact.notes = changes["notes"].strip() or None

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError.conflict(ErrorCode.DASHBOARD_SLUG_TAKEN)
    await db.refresh(artifact)
    if switch_identity and identity == "creator":
        from app.services.dashboard_data import release_snapshot_withheld

        release = await published_release(db, artifact)
        if release is not None and await release_snapshot_withheld(db, artifact, release):
            # The stored rows are not the owner's (an editor published them):
            # the owner's run replaces them now.
            from app.core import fire_and_forget
            from app.services.dashboard_refresh import refresh_as_identity

            fire_and_forget.spawn(refresh_as_identity(str(artifact.id)))
    if cron is not None:
        from app.services.dashboard_refresh import org_timezone, register_job

        register_job(str(artifact.id), artifact.cron_schedule, await org_timezone(db, artifact.organization_id))
    await _audit(db, organization, current_user, artifact, "dashboard.settings_changed",
                 {k: v for k, v in changes.items() if v is not None})
    return get_settings(artifact)
