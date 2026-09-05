"""Who may see which catalog entities ("queries").

"Agent" == ``DataSource``. An Entity is m:n with agents, and its visibility has
two tiers:

**Catalog tier** — an approved, published entity is part of the org's catalog.
Anyone who can reach every agent it is attached to may read it. That access cut
is applied separately (``list_entities`` excludes entities carrying an agent the
caller cannot access); this module never widens it.

**Workshop tier** — everything that is not yet approved-and-published: a private
draft, a suggestion awaiting review, an admin draft, an archived row. These are
work in progress and belong to their author plus whoever may approve them:

* the owner, always;
* an org-level ``manage_entities`` admin;
* a manager holding ``create_entities`` on EVERY agent the entity is attached to
  — the same bar the write routes already enforce (``create_private_entity`` /
  ``create_global_entity`` check ``create_entities`` across all of
  ``data_source_ids``).

This module exists because that rule lived only in the browser
(``pages/queries/index.vue`` hid the rows it must not show) while the API
returned every workshop row in the org to any member who could reach its agents
— titles, slugs, descriptions and owner ids included. The tree now asks the same
question the list page did, so the rule is written once, server-side, and the
per-agent badge counts are derived from it rather than re-implementing it.

An entity attached to NO agent cannot clear the manager clause: no per-agent
grant covers "all of them", so it stays owner-and-admin only, matching how an
agent-less eval case takes org-level authority.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from sqlalchemy import and_, exists, false, or_, select, true


async def entity_authority_scope(db, user_id: str, org_id: str) -> Tuple[bool, set]:
    """``(unscoped, agent_ids)`` — the workshop authority this caller holds.

    ``unscoped`` is True for org-level ``manage_entities`` admins, who see every
    row. Otherwise ``agent_ids`` is every agent where they hold
    ``create_entities``, directly or through a grant that implies it.
    """
    from app.core.permission_resolver import resolve_permissions

    resolved = await resolve_permissions(db, str(user_id), str(org_id))
    if resolved.has_org_permission("manage_entities"):
        return True, set()
    agent_ids = {
        str(rid)
        for (rtype, rid) in resolved.resource_permissions
        if rtype == "data_source"
        and resolved.has_resource_permission("data_source", rid, "create_entities")
    }
    return False, agent_ids


def is_catalog_row(entity: Any) -> bool:
    """Published, and not somebody's private work — the tier every reader of
    its agents may see.

    ``global_status`` is allowed to be NULL as well as ``approved`` because the
    direct create endpoints (``POST /entities`` and ``POST /entities/global``)
    persist neither dual-status field, so every row they ever made carries
    ``(NULL, NULL, published)``. Those have always been readable — the mention
    picker lists them on ``status == "published"`` alone — and this rule is here
    to stop leaking *workshop* rows, not to retire catalog rows that predate the
    dual-status workflow.

    A rejected suggestion is ``(published, rejected, archived)`` and a live
    suggestion ``(published, suggested, draft)``: both carry ``private_status``
    and neither is published, so both stay in the workshop tier.
    """
    return (
        not getattr(entity, "private_status", None)
        and getattr(entity, "status", None) == "published"
        and getattr(entity, "global_status", None) in (None, "approved")
    )


def can_view_entity(entity: Any, viewer_id: Optional[str], unscoped: bool, agent_ids: set) -> bool:
    """Whether this caller may see one entity, ASSUMING agent access is settled.

    Never call this instead of the data-source access filter — only after it.
    """
    if entity is None:
        return False
    if is_catalog_row(entity):
        return True
    if unscoped:
        return True
    if viewer_id and str(getattr(entity, "owner_id", "")) == str(viewer_id):
        return True
    ds = {str(d.id) for d in (getattr(entity, "data_sources", None) or [])}
    if not ds:
        return False  # agent-less workshop row — owner and org admin only
    return ds <= agent_ids


def visibility_clause(entity_model, association, viewer_id: Optional[str], unscoped: bool, agent_ids: set):
    """The same rule as :func:`can_view_entity`, in SQL.

    Applied before ``LIMIT`` so a page of results is a page of rows the caller
    may actually see — filtering after the fact would let other people's drafts
    consume the window and silently truncate the caller's own.
    """
    catalog = and_(
        entity_model.private_status.is_(None),
        entity_model.status == "published",
        or_(
            entity_model.global_status.is_(None),
            entity_model.global_status == "approved",
        ),
    )
    clauses = [catalog]
    if viewer_id:
        clauses.append(entity_model.owner_id == str(viewer_id))
    if unscoped:
        clauses.append(true())
    elif agent_ids:
        # Attached to at least one agent, and to none the caller lacks
        # create_entities on.
        # select_from + correlate, not a bare select(1).where(...): the counts
        # query already has the association table in its own FROM, and SQLAlchemy
        # would auto-correlate it out of these subqueries, leaving them with no
        # FROM clause at all ("returned no FROM clauses due to auto-correlation").
        attached = (
            select(1)
            .select_from(association)
            .where(association.c.entity_id == entity_model.id)
            .correlate(entity_model)
        )
        on_a_foreign_agent = (
            select(1)
            .select_from(association)
            .where(
                and_(
                    association.c.entity_id == entity_model.id,
                    association.c.data_source_id.notin_(list(agent_ids)),
                )
            )
            .correlate(entity_model)
        )
        clauses.append(and_(exists(attached), ~exists(on_a_foreign_agent)))
    else:
        clauses.append(false())
    return or_(*clauses)
