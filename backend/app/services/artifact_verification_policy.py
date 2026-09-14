"""Live authorization for browser tools; independent of deployment settings."""
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload

from app.core.permissions_decorator import requires_permission
from app.core.permission_resolver import user_can_access_data_source
from app.models.artifact import Artifact
from app.models.data_source import DataSource
from app.models.organization_settings import OrganizationSettings
from app.models.report import Report
from app.models.user import User


@dataclass
class BrowserPolicy:
    allow_data: bool = False
    artifact_preview: bool = False
    connector: bool = False
    report: object = None

    @property
    def available(self):
        return self.allow_data and (self.artifact_preview or self.connector)


def _enabled(config, key, default=False):
    """Accept persisted feature objects and legacy booleans; fail closed otherwise."""
    value = config.get(key, (config.get("ai_features") or {}).get(key, default))
    if isinstance(value, dict):
        value = value.get("value", value.get("state") == "enabled")
    return value is True


@requires_permission("view_reports", model=Artifact, owner_only=True, allow_public=True)
async def _can_view_artifact(*, artifact_id, current_user, organization, db):
    return True


@requires_permission("view_reports", model=Report, owner_only=True, allow_public=True)
async def _can_view_report(*, report_id, current_user, organization, db):
    return True


async def browser_policy(ctx, artifact_id=None):
    """Read committed policy in a new session, never a run's cached ORM settings.

    Passing an artifact ID checks that exact target. Catalog checks without an
    ID look for any eligible page. The same route permission rules handle owners,
    admins, shares and projects; no parallel authorization model is introduced.
    """
    from app.dependencies import async_session_maker

    org, user, report = (ctx.get(k) for k in ("organization", "user", "report"))
    if not all(getattr(obj, "id", None) for obj in (org, user, report)):
        return BrowserPolicy()
    factory = ctx.get("session_maker") or async_session_maker
    async with factory() as db:
        config = (await db.execute(select(OrganizationSettings.config).where(
            OrganizationSettings.organization_id == str(org.id),
        ))).scalar_one_or_none() or {}
        policy = BrowserPolicy(allow_data=_enabled(config, "allow_llm_see_data", True))
        if not policy.allow_data:
            return policy
        principal = (await db.execute(select(User).options(lazyload("*")).where(
            User.id == str(user.id),
        ))).scalar_one_or_none()
        if principal is None or (not principal.is_active and not principal.is_service_account):
            return policy
        # Logout/password reset/admin sign-out invalidates the running principal
        # as well as its HTTP tokens. Service-account membership is checked by
        # the shared permission decorator below.
        epoch = getattr(user, "session_epoch", None)
        if epoch is not None and epoch != principal.session_epoch:
            return policy
        user = principal
        current = (await db.execute(select(Report).options(lazyload("*")).where(
            Report.id == str(report.id), Report.organization_id == str(org.id),
            Report.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if current is None or current.report_type == "artifact_chat":
            return policy
        access = dict(current_user=user, organization=org, db=db)
        if _enabled(config, "enable_artifact_verification"):
            stmt = select(Artifact.id).where(
                Artifact.report_id == str(report.id), Artifact.organization_id == str(org.id),
                Artifact.mode == "page", Artifact.deleted_at.is_(None),
            )
            if artifact_id is not None:
                stmt = stmt.where(Artifact.id == str(artifact_id))
            for candidate in (await db.execute(stmt)).scalars():
                try:
                    await _can_view_artifact(artifact_id=candidate, **access)
                    policy.artifact_preview = True
                    break
                except HTTPException as exc:
                    if exc.status_code not in (401, 403, 404):
                        raise
        # An explicit artifact target can never fall back to a browser connector.
        if artifact_id is not None:
            return policy
        try:
            await _can_view_report(report_id=str(report.id), **access)
        except HTTPException as exc:
            if exc.status_code not in (401, 403, 404):
                raise
            return policy
        current = (await db.execute(select(Report).options(
            lazyload("*"), selectinload(Report.data_sources).options(
                lazyload("*"), selectinload(DataSource.connections).lazyload("*")),
        ).where(Report.id == current.id))).scalar_one()
        sources = []
        for ds in current.data_sources:
            if ds.deleted_at or not ds.is_active or str(ds.organization_id) != str(org.id):
                continue
            connections = [c for c in ds.connections if c.type == "browser" and c.is_active
                           and not c.deleted_at and str(c.organization_id) == str(org.id)]
            if connections and await user_can_access_data_source(db, str(user.id), str(org.id), ds):
                sources.append(SimpleNamespace(connections=connections))
        policy.connector = bool(sources)
        policy.report = SimpleNamespace(id=current.id, report_type=current.report_type, data_sources=sources)
        return policy


async def require_browser_access(ctx, artifact_id=None):
    policy = await browser_policy(ctx, artifact_id)
    if not policy.allow_data or not (policy.artifact_preview if artifact_id else policy.connector):
        raise PermissionError("Browser access is restricted by the current organization policy or report access")
    return policy


async def artifact_verification_available(ctx, artifact_id):
    # Advisory hints must never turn a successful artifact save into a failure.
    try:
        return (await browser_policy(ctx, artifact_id)).artifact_preview
    except Exception:
        return False
