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

from typing import Any, Iterable, Optional, Sequence, Tuple

from sqlalchemy import String, cast, or_


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


# --- Agent narrowing, in SQL -------------------------------------------------
#
# The predicates above answer "may this caller see it?". These answer "does it
# belong to the agent being looked at?" — presentation, not authority, and they
# must be applied on top of an authority filter, never instead of one.
#
# They exist in SQL because the per-agent eval views were narrowing a page of
# ORG-WIDE rows in the browser: the client asked for the newest 100 runs in the
# organization and kept the ones touching its agent. With enough traffic from
# other agents, an agent's own runs fell off the end of that page and the panel
# said "no runs" for an agent that had plenty. Filtering before LIMIT makes the
# window per-agent, so the cap bounds one agent's history instead of the org's.
#
# Matching is textual because ``data_source_ids_json`` is a portable ``JSON``
# column, and JSON containment operators differ between Postgres and SQLite.
# Casting to text and substring-matching a quoted id behaves identically on
# both; the same cast is already how case search works.

_EMPTY_JSON_TEXT = ("[]", "null", "")


def _targets_no_agent(column):
    """The row names no agent — i.e. it applies to every agent in the org."""
    return or_(column.is_(None), cast(column, String).in_(_EMPTY_JSON_TEXT))


def _targets_agent(column, agent_id: str):
    # autoescape so an id carrying % or _ cannot widen the match.
    return cast(column, String).contains(f'"{agent_id}"', autoescape=True)


def agent_scope_clause(column, data_source_id: Optional[str] = None, scope: Optional[str] = None):
    """Narrow eval rows to one agent's world. ``None`` means "no narrowing".

    ``data_source_id`` matches that agent's rows PLUS the agent-less ones, which
    run against every agent and so are genuinely part of what that agent is
    tested by. ``scope="global"`` matches only the agent-less rows.

    Note this is deliberately wider than ``TestSuite.data_source_id`` filtering
    in ``list_suites``: a suite's home agent is a filing location and belongs to
    exactly one shelf, whereas an agent-less CASE really does execute against
    the agent you are looking at.
    """
    if data_source_id:
        return or_(_targets_no_agent(column), _targets_agent(column, str(data_source_id)))
    if scope == "global":
        return _targets_no_agent(column)
    return None


def holds_any_eval_authority(unscoped: bool, agent_ids: Sequence | set) -> bool:
    """Whether the caller may use the eval surface at all.

    The admission test, matching ``requires_permission(..., resource_scoped=True)``
    on the routes: org-level OR a grant on at least one agent. What they can then
    see or change is decided per case by the predicates above — never by this.
    """
    return bool(unscoped or agent_ids)
