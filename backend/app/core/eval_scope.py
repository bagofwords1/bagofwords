"""Canonical eval authority resolution, shared by the HTTP routes and the agent
tools.

"Agent" == ``DataSource``. Evals follow the instruction access model:

**One rule: authority over EVERY agent a case targets — to see it, run it, or
edit it.** An eval spanning agents A and B belongs to whoever manages both; a
manager of A alone neither sees nor touches it. An agent-less case implicitly
covers every agent, so it takes org-level ``manage_evals``.

Evals are deliberately stricter here than instructions, which grant read on a
UNION (``user_can_view_instruction``). The asymmetry is the point: an
instruction CHANGES your agent's behaviour, so you must be able to see what is
governing it, whereas an eval only tests — and its results carry real query
output (``/results/{id}/transcript`` renders the same view the agent sees
internally). Tighter is correct where data, not behaviour, is at stake.

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


def can_edit_case(case: Any, unscoped: bool, agent_ids: set) -> bool:
    """Authority over one case: ``manage_evals`` on EVERY agent it targets.

    An agent-less case runs against every agent in the org, so it needs
    org-level authority rather than any per-agent grant.
    """
    if unscoped:
        return True
    if case is None:
        return False
    ds = {str(x) for x in (getattr(case, "data_source_ids_json", None) or [])}
    if not ds:
        return False  # org-wide — org-level manage_evals only
    return ds <= agent_ids


# Seeing and changing a case are the same bar (see the module docstring), so
# this is an alias rather than a second rule that could drift from it.
can_view_case = can_edit_case


def is_relevant_to_session(case: Any, session_ids: set) -> bool:
    """Whether a case concerns the agents attached to THIS session.

    Deliberately a union where authority is an intersection, because they answer
    different questions. Authority asks "is this mine?" — a routing eval spanning
    A and B belongs to whoever manages both. Relevance asks "does this concern
    what I am looking at?" — and that same eval does concern a session pinned to
    A, so it should not be filtered out of it.

    Applied AFTER an authority check, never instead of one: it narrows what an
    authorized caller is shown, it never widens what they may see. An empty
    ``session_ids`` (an Auto session pins nothing) imposes no bound at all.
    """
    if not session_ids:
        return True
    ds = {str(x) for x in (getattr(case, "data_source_ids_json", None) or [])}
    if not ds:
        return True  # org-wide — it runs against the pinned agent too
    return bool(ds & session_ids)


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
