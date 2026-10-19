"""Access rules for lists: they follow the agents (DataSource) they are linked to.

view   -> any user who can VIEW one linked agent (org grant, resource grant,
          or the agent is public within the org)
manage -> rows: any user who can MANAGE the agent they work from;
          schema (and deleting the list from every agent): MANAGE on every
          linked agent, the same bar as editing an instruction shared by
          several agents

Mirrors ``requires_resource_permission`` so route checks and runtime checks
(tool registration, bow.<agent>.lists tables) can never disagree.
"""
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set

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


@dataclass
class ListAccess:
    """What one user may do with one list across its linked agents."""
    visible_agents: List[DataSource] = field(default_factory=list)
    hidden_agent_count: int = 0
    can_manage_schema: bool = False
    home_id: Optional[str] = None

    @property
    def shared(self) -> bool:
        return len(self.visible_agents) + self.hidden_agent_count > 1

    @property
    def name_agent(self) -> Optional[DataSource]:
        """The agent a shared list's table is named after for this user: its
        home agent when they can see it, else the first linked agent (by id)
        they can see. Independent of the report or page, so every surface
        (schema context, tool description, REST, UI) shows the same
        ``bow.<agent>.lists.<slug>``."""
        if not self.visible_agents:
            return None
        home = next((a for a in self.visible_agents if str(a.id) == str(self.home_id)), None)
        return home or min(self.visible_agents, key=lambda a: str(a.id))


async def resolved_permissions(db, user, organization):
    """Resolve once, then evaluate many lists with ``access_from``."""
    if user is None or organization is None:
        return None
    return await _resolved(db, user, organization)


def access_from(resolved, organization, agents: List[DataSource], home_id: Optional[str] = None) -> ListAccess:
    """``agents`` are the list's linked agents and ``home_id`` its home agent;
    ``resolved`` comes from ``resolved_permissions`` (None for no user)."""
    if resolved is None or organization is None:
        return ListAccess(hidden_agent_count=len(agents), home_id=home_id)
    mine = [a for a in agents if str(a.organization_id) == str(organization.id)]
    visible = [a for a in mine if _allowed(resolved, a, "view")]
    return ListAccess(
        visible_agents=visible,
        hidden_agent_count=len(agents) - len(visible),
        can_manage_schema=bool(mine) and len(mine) == len(agents)
        and all(_allowed(resolved, a, "manage") for a in mine),
        home_id=home_id,
    )


def _can_submit(resolved, organization, ds: DataSource, agent_list) -> bool:
    if ds is None or str(ds.organization_id) != str(organization.id):
        return False
    if _allowed(resolved, ds, "manage"):
        return True
    return bool(getattr(agent_list, "allow_viewer_submissions", False)) and _allowed(resolved, ds, "view")


async def first_submittable_agent(db, user, organization, agents: Iterable[DataSource],
                                  agent_list, *, resolved=None) -> Optional[DataSource]:
    """The first of ``agents`` (in the given order) through which ``user`` may
    save rows to ``agent_list`` (MANAGE on the agent, or VIEW when the list allows
    viewer submissions), or None.
    Pass ``resolved`` (``resolved_permissions``) to reuse one resolution."""
    if user is None or organization is None:
        return None
    if resolved is None:
        resolved = await _resolved(db, user, organization)
    return next((ds for ds in agents if _can_submit(resolved, organization, ds, agent_list)), None)
