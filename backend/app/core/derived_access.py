"""
Derived access helpers — MVP RBAC Phase 5.

In MVP, reports/builds/widgets/entities do NOT have their own resource
grants. Access is derived from the user's data_source resource grants
(plus the full_admin_access wildcard and relevant org-level perms).

These helpers centralize the derivation logic so services and routes
don't re-implement it. Post-MVP, report-level grants can be added here
without changing call sites.

Decision §3.5: A report touching multiple data sources is visible to a
user who can access ANY of those data sources. No artifact blanking.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User


async def visible_data_source_ids(
    db: AsyncSession,
    user: User,
    organization: Organization,
) -> set[str]:
    """
    Return the set of data_source IDs the user can at least `view`.

    - full_admin_access → all DS in the org
    - Otherwise → DS where user has the `view` resource grant, plus public DS
    """
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))

    # full_admin_access sees everything in the org
    if FULL_ADMIN in resolved.org_permissions:
        result = await db.execute(
            select(DataSource.id).where(DataSource.organization_id == organization.id)
        )
        return {str(r[0]) for r in result.all()}

    # Public data sources are visible to any org member
    public_result = await db.execute(
        select(DataSource.id).where(
            DataSource.organization_id == organization.id,
            DataSource.is_public.is_(True),
        )
    )
    visible: set[str] = {str(r[0]) for r in public_result.all()}

    # Plus any DS the user has any grant on (any per-DS permission implies view)
    for (rtype, rid), perms in resolved.resource_permissions.items():
        if rtype == "data_source" and perms:
            visible.add(str(rid))

    return visible


async def can_view_report(
    db: AsyncSession,
    user: User,
    organization: Organization,
    report: Report,
) -> bool:
    """
    A report is visible if the user can view AT LEAST ONE of its data sources
    (decision §3.5). Reports with no data sources attached are visible to any
    org member — they're usually text-only or still being built.
    """
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if FULL_ADMIN in resolved.org_permissions:
        return True

    ds_list = report.data_sources or []
    if not ds_list:
        return True

    visible = await visible_data_source_ids(db, user, organization)
    return any(str(ds.id) in visible for ds in ds_list)


async def can_edit_report(
    db: AsyncSession,
    user: User,
    organization: Organization,
    report: Report,
) -> bool:
    """
    A report is editable if the user has `update_reports` at the org level
    (or full_admin_access) AND can view every data source the report touches.
    Stricter than view: editing can run steps against all attached DS, so we
    require access to all of them.
    """
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if FULL_ADMIN in resolved.org_permissions:
        return True

    if not resolved.has_org_permission("update_reports"):
        return False

    ds_list = report.data_sources or []
    if not ds_list:
        return True

    visible = await visible_data_source_ids(db, user, organization)
    return all(str(ds.id) in visible for ds in ds_list)


async def filter_visible_reports(
    db: AsyncSession,
    user: User,
    organization: Organization,
    reports: Iterable[Report],
) -> list[Report]:
    """Convenience: filter a collection of reports through can_view_report."""
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if FULL_ADMIN in resolved.org_permissions:
        return list(reports)

    visible = await visible_data_source_ids(db, user, organization)

    def _visible(r: Report) -> bool:
        ds_list = r.data_sources or []
        if not ds_list:
            return True
        return any(str(ds.id) in visible for ds in ds_list)

    return [r for r in reports if _visible(r)]
