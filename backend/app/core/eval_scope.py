"""Canonical eval authority resolution, shared by the HTTP routes and the agent
tools.

"Agent" == ``DataSource``. Evals follow the instruction access model:

  - **Read is a UNION** over the agents a case targets — authority over any one
    of them lets you see the row. An eval that verifies routing between agents A
    and B governs both, so each manager must see that it exists.
  - **Write is an INTERSECTION** — mutating or executing a case needs authority
    over EVERY agent it targets, so neither manager alone can change what it
    asserts about the other's agent.
  - **An agent-less case is org-wide**: visible to everyone (it runs against
    your agent too), editable org-level only — exactly like a global
    instruction.

This module exists because the same rule was being re-derived in seven places
and each copy consulted a narrower tier than the model defines. The agent tools
in particular tested ``has_org_permission("manage_evals")`` alone, which denies
every per-agent eval manager — while the tool catalog, which resolves per-agent
grants correctly, still advertised the tools to them. The result was an agent
owner in training mode being offered an eval tool that always failed.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence, Tuple


async def eval_agent_scope(db, user_id: str, org_id: str) -> Tuple[bool, set]:
    """``(unscoped, agent_ids)`` — the agents this caller may manage evals on.

    ``unscoped`` is True for org-level eval admins, who see everything.
    Otherwise ``agent_ids`` is every agent where they hold ``manage_evals``,
    directly or through a grant that implies it (a per-agent ``manage``).
    """
    from app.core.permission_resolver import resolve_permissions

    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    if resolved.has_org_permission("manage_evals"):
        return True, set()
    agent_ids = {
        rid
        for (rtype, rid) in resolved.resource_permissions
        if rtype == "data_source"
        and resolved.has_resource_permission("data_source", rid, "manage_evals")
    }
    return False, agent_ids


def can_view_case(case: Any, unscoped: bool, agent_ids: set) -> bool:
    """Read authority over one case — union over its agents; globals visible."""
    if unscoped:
        return True
    if case is None:
        return False
    ds = {str(x) for x in (getattr(case, "data_source_ids_json", None) or [])}
    if not ds:
        return True  # org-wide — visible to all, editable org-level only
    return bool(ds & agent_ids)


def can_edit_case(case: Any, unscoped: bool, agent_ids: set) -> bool:
    """Write authority over one case — intersection; globals are org-level."""
    if unscoped:
        return True
    if case is None:
        return False
    ds = {str(x) for x in (getattr(case, "data_source_ids_json", None) or [])}
    if not ds:
        return False  # agent-less case runs against every agent
    return ds <= agent_ids


def filter_cases(cases: Iterable[Any], unscoped: bool, agent_ids: set) -> list:
    if unscoped:
        return list(cases)
    return [c for c in cases if can_view_case(c, unscoped, agent_ids)]


def holds_any_eval_authority(unscoped: bool, agent_ids: Sequence | set) -> bool:
    """Whether the caller may use the eval surface at all.

    The admission test, matching ``requires_permission(..., resource_scoped=True)``
    on the routes: org-level OR a grant on at least one agent. What they can then
    see or change is decided per case by the predicates above — never by this.
    """
    return bool(unscoped or agent_ids)
