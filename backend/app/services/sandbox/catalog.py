"""Whether the sandbox tools are offered to a user's agent in a report, and
what the agent is told about that report's sandbox."""
from __future__ import annotations

from typing import Any, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.sandbox.access import SandboxAccess
from app.services.sandbox.feature import sandbox_feature_enabled
from app.services.sandbox.prompt import run_note
from app.services.sandbox.settings import load_settings


async def sandbox_run_note(db: AsyncSession, organization: Any, report: Any, user: Any,
                           data_sources: Optional[Sequence[Any]] = None) -> Optional[str]:
    """``None`` when the tools must not be offered; otherwise the paragraph to
    append to their descriptions for this run."""
    if not sandbox_feature_enabled() or organization is None or report is None:
        return None
    organization_id = str(organization.id)
    # A run nobody triggered in person (a scheduled run) acts as the report's owner.
    user_id = str(user.id) if user is not None and getattr(user, "id", None) else None
    if user_id is None and getattr(report, "user_id", None):
        user_id = str(report.user_id)

    settings = await load_settings(db, organization_id)
    if not settings.enabled:
        return None
    access = SandboxAccess()
    if not await access.tools_offered(db, organization_id, user_id):
        return None
    environments = await access.usable_environments(db, organization_id, user_id)

    from app.services.sandbox import environments as environment_service
    from app.services.sandbox.errors import SandboxError
    from app.services.sandbox.service import ReportRef, SandboxService

    ref = ReportRef(id=str(report.id), organization_id=organization_id, title=getattr(report, "title", "") or "")
    existing = await SandboxService().describe(db, ref)
    if existing and existing.get("state") in ("failed", "deleted"):
        existing = None
    default_name = None
    try:
        agent_default = await environment_service.agent_default_environment_id(
            db, [str(ds.id) for ds in (data_sources or []) if getattr(ds, "id", None)])
        default_name = environment_service.choose_environment(
            environments, requested=None, agent_default_id=agent_default,
            org_default_id=settings.default_environment_id,
        ).name
    except SandboxError:
        pass
    return run_note(existing=existing, environments=environments, default_name=default_name)
