"""Who may open the monitoring console, and over which agents.

Every ``/console/*`` endpoint used to require org-level ``manage_settings``,
which made monitoring an admin-only surface. But the person who most needs to
see an agent's runs, failures and spend is whoever *manages that agent*, so the
gate is two-tier:

* org admins (``manage_settings``, or the ``full_admin_access`` wildcard) keep
  the org-wide view — nothing changes for them;
* anyone holding ``manage`` on at least one agent gets in, scoped to exactly
  those agents (the same permission that scopes the review feed and the inbox);
* everyone else is denied.

``ConsoleScope`` carries that decision. ``data_source_ids is None`` means
"org-wide, no filtering"; a list means "only these agents". Routes push the
caller's own agent filter through ``scoped_params`` so a scoped manager can
narrow their view but never widen it past their grants.
"""
from typing import List, Optional

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permission_resolver import (
    assert_principal_belongs_to_org,
    resolve_permissions,
)
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.report_data_source_association import report_data_source_association
from app.models.user import User
from app.schemas.console_schema import MetricsQueryParams
from app.settings.config import settings

# Org permission that grants the org-wide console.
CONSOLE_ADMIN_PERMISSION = "manage_settings"
# Per-agent grant that makes someone an "agent manager" (mirrors review_service
# and inbox_service, so all three surfaces agree on who manages an agent).
AGENT_MANAGE_PERMISSION = "manage"


class ConsoleScope:
    """The agents a console caller may see.

    ``data_source_ids is None`` → org-wide (admin). A list → only those agents.
    """

    __slots__ = ("data_source_ids",)

    def __init__(self, data_source_ids: Optional[List[str]] = None) -> None:
        self.data_source_ids = data_source_ids

    @property
    def is_org_wide(self) -> bool:
        return self.data_source_ids is None

    def resolve_ids(self, requested: Optional[str]) -> Optional[str]:
        """Narrow a caller-supplied comma-separated agent filter to this scope.

        Returns the comma-separated filter the service should apply, or None for
        "no filter" (only possible org-wide). A scoped caller asking exclusively
        for agents they don't manage is a 403 rather than a silent widening —
        returning an empty filter would read as "all agents" downstream.
        """
        req = [ds.strip() for ds in (requested or "").split(",") if ds.strip()]
        if self.data_source_ids is None:
            return ",".join(req) if req else None
        if not req:
            return ",".join(self.data_source_ids)
        allowed = set(self.data_source_ids)
        narrowed = [ds for ds in req if ds in allowed]
        if not narrowed:
            raise HTTPException(
                status_code=403,
                detail="You don't manage any of the requested agents",
            )
        return ",".join(narrowed)

    def scoped_params(self, params: MetricsQueryParams) -> MetricsQueryParams:
        """A copy of ``params`` whose agent filter is clamped to this scope."""
        return params.model_copy(
            update={"data_source_ids": self.resolve_ids(params.data_source_ids)}
        )

    async def assert_report_visible(self, db: AsyncSession, report_id: str) -> None:
        """Guard a per-report drill-down (trace modal, conversation replay).

        A report is visible when it draws on at least one in-scope agent — the
        same ANY-of-its-agents rule the list endpoints filter by, so every row a
        manager can see in the table can also be opened.
        """
        if self.is_org_wide:
            return
        hit = (await db.execute(
            select(report_data_source_association.c.report_id)
            .where(
                report_data_source_association.c.report_id == report_id,
                report_data_source_association.c.data_source_id.in_(self.data_source_ids),
            )
            .limit(1)
        )).first()
        if hit is None:
            raise HTTPException(
                status_code=403,
                detail="You don't manage the agents behind this report",
            )


async def console_scope(
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    user: User = Depends(current_user),
) -> ConsoleScope:
    """Console gate + scope resolution, used in place of ``@requires_permission``.

    Replays the membership/verification checks the permission decorator does,
    then answers the console's own question: org-wide, a specific set of agents,
    or 403.
    """
    if not user.is_verified and settings.bow_config.features.verify_emails:
        raise HTTPException(status_code=403, detail="User is not verified")
    await assert_principal_belongs_to_org(db, user, organization.id)

    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if resolved.has_org_permission(CONSOLE_ADMIN_PERMISSION):
        return ConsoleScope(None)

    managed = sorted(
        rid
        for (rtype, rid), perms in resolved.resource_permissions.items()
        if rtype == "data_source" and AGENT_MANAGE_PERMISSION in perms
    )
    if not managed:
        from app.core.permissions_decorator import _audit_access_denied
        await _audit_access_denied(
            db, user, organization, CONSOLE_ADMIN_PERMISSION, "console"
        )
        raise HTTPException(status_code=403, detail="Permission denied")
    return ConsoleScope(managed)
