"""Access rules for lists: they follow the owning agent (DataSource).

view   -> any user who can VIEW the agent (org grant, resource grant, or the
          agent is public within the org)
manage -> any user who can MANAGE the agent (org grant or resource grant; the
          agent's owner holds a manage grant)

Mirrors ``requires_resource_permission`` so route checks and runtime checks
(tool registration, bow.<agent>.lists tables) can never disagree.
"""
from typing import Iterable, List, Set

from sqlalchemy import select

from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
from app.models.data_source import DataSource


async def _resolved(db, user, organization):
    return await resolve_permissions(db, str(user.id), str(organization.id))


def _allowed(resolved, ds: DataSource, permission: str) -> bool:
    if FULL_ADMIN in resolved.org_permissions:
        return True
    if resolved.has_org_permission(permission):
        return True
    if resolved.has_resource_permission("data_source", str(ds.id), permission):
        return True
    if permission == "view" and getattr(ds, "is_public", False):
        return True
    return False


async def can_access_agent(db, user, organization, ds: DataSource, permission: str) -> bool:
    if user is None or organization is None or ds is None:
        return False
    if str(ds.organization_id) != str(organization.id):
        return False
    return _allowed(await _resolved(db, user, organization), ds, permission)


async def can_submit_to_list(db, user, organization, ds: DataSource, agent_list) -> bool:
    """Agent submissions follow the edit rule (MANAGE on the agent) unless the
    list opts in to submissions from anyone who can use (VIEW) the agent."""
    if await can_access_agent(db, user, organization, ds, "manage"):
        return True
    return bool(getattr(agent_list, "allow_viewer_submissions", False)) and await can_access_agent(
        db, user, organization, ds, "view"
    )


async def viewable_agent_ids(db, user, organization, data_source_ids: Iterable[str]) -> Set[str]:
    """Subset of ``data_source_ids`` the user may view (org-scoped)."""
    ids: List[str] = [str(i) for i in data_source_ids if i]
    if not ids or user is None or organization is None:
        return set()
    rows = (await db.execute(
        select(DataSource).where(
            DataSource.id.in_(ids),
            DataSource.organization_id == str(organization.id),
        )
    )).scalars().all()
    resolved = await _resolved(db, user, organization)
    return {str(ds.id) for ds in rows if _allowed(resolved, ds, "view")}
