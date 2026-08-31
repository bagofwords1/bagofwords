"""Transitive identity taint over the loadable dependency graph.

A step or entity is identity-differentiated when it declares an
identity-source parameter OR when its code consumes (load_step/load_entity)
anything that is. The derived asset's shared snapshot is then one identity's
slice by construction and must never be served to another reader — exactly
the rule the direct case already enforces; this module extends it through
the dependency chain.

Refs are string literals by contract (the loadables resolver enforces that),
so the graph is statically computable from stored code. Analysis also
returns the newest upstream refresh timestamp so per-viewer caches go stale
when ANY upstream refreshes, not only the asset itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Set, Tuple

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

_MAX_DEPTH = 5


@dataclass
class TaintResult:
    identity: bool = False
    # Newest last-materialized timestamp among all upstreams (None = no deps)
    latest_upstream_refresh: Optional[datetime] = None

    def fold(self, other: "TaintResult") -> None:
        self.identity = self.identity or other.identity
        for ts in (other.latest_upstream_refresh,):
            if ts is not None and (
                self.latest_upstream_refresh is None or ts > self.latest_upstream_refresh
            ):
                self.latest_upstream_refresh = ts


# Param sources whose value is derived from an identity, and which therefore
# make the snapshot they produced one identity's slice.
#
# `input_identity_default` counts. Its value is the viewer's identity binding
# whenever the client submits nothing — which is exactly how a snapshot is
# materialized (the owner's refresh, the schedule, refresh-on-view all run
# with no submitted value). So the shared snapshot of such a query is filtered
# by the OWNER's identity, and serving it to another reader shows them the
# owner's slice, the very thing this module exists to prevent. After
# resolution there is no way to tell a client-supplied value from the identity
# fallback, so this fails closed. The frontend already groups the two sources
# this way when deciding which queries to re-run on a view-as switch
# (queriesWithIdentityParams).
_IDENTITY_SOURCES = ("identity", "input_identity_default")


def _has_identity_param(parameters) -> bool:
    try:
        return any(
            isinstance(p, dict) and p.get("source") in _IDENTITY_SOURCES
            for p in (parameters or [])
        )
    except Exception:
        return False


async def analyze_code_taint(
    db: AsyncSession,
    code: str,
    *,
    organization_id: Optional[str],
    report_id: Optional[str],
    _visited: Optional[Set[str]] = None,
    _depth: int = 0,
) -> TaintResult:
    """Walk the code's load_step/load_entity refs and fold their identity
    scope + refresh timestamps. Failure-closed on identity: a ref that cannot
    be resolved contributes nothing (its data would not resolve at run time
    either); an analysis error on a resolved node treats it as NOT tainting
    only if its declarations are unreadable — declared identity is read
    directly from the row, so that path has no exception surface in practice.
    """
    from app.ai.code_execution.loadables import extract_loadable_refs

    result = TaintResult()
    if not code or _depth >= _MAX_DEPTH:
        return result
    visited = _visited if _visited is not None else set()

    step_refs, entity_refs = extract_loadable_refs(code)

    if step_refs and report_id:
        from app.models.step import Step
        from app.models.query import Query
        from app.models.widget import Widget
        steps = (await db.execute(
            select(Step)
            .options(lazyload("*"))
            .join(Widget, Step.widget_id == Widget.id)
            .where(Widget.report_id == str(report_id), Step.status == "success")
        )).scalars().all()
        by_key = {}
        for s in steps:
            by_key[str(s.id)] = s
            if s.slug:
                by_key.setdefault(s.slug.lower(), s)
            if s.title:
                by_key.setdefault(s.title.lower(), s)
        for ref in step_refs:
            s = by_key.get(str(ref)) or by_key.get(str(ref).lower())
            if s is None:
                continue
            vkey = f"step:{s.id}"
            if vkey in visited:
                continue
            visited.add(vkey)
            sub = TaintResult(latest_upstream_refresh=getattr(s, "updated_at", None))
            if getattr(s, "query_id", None):
                row = (await db.execute(
                    select(Query.parameters).where(Query.id == str(s.query_id))
                )).first()
                if row and _has_identity_param(row[0]):
                    sub.identity = True
            deeper = await analyze_code_taint(
                db, s.code or "", organization_id=organization_id,
                report_id=report_id, _visited=visited, _depth=_depth + 1,
            )
            sub.fold(deeper)
            result.fold(sub)

    if entity_refs and organization_id:
        from app.models.entity import Entity
        for ref in entity_refs:
            key = str(ref)
            ent = (await db.execute(
                select(Entity)
                .options(lazyload("*"))
                .where(
                    Entity.organization_id == str(organization_id),
                    Entity.status == "published",
                    Entity.deleted_at.is_(None),
                    or_(
                        Entity.id == key,
                        Entity.slug.ilike(key),
                        Entity.title.ilike(key),
                    ),
                )
                .limit(1)
            )).scalar_one_or_none()
            if ent is None:
                continue
            vkey = f"entity:{ent.id}"
            if vkey in visited:
                continue
            visited.add(vkey)
            sub = TaintResult(
                identity=_has_identity_param(getattr(ent, "parameters", None)),
                latest_upstream_refresh=getattr(ent, "last_refreshed_at", None),
            )
            deeper = await analyze_code_taint(
                db, ent.code or "", organization_id=organization_id,
                report_id=None,  # entity code loads entities, not report steps
                _visited=visited, _depth=_depth + 1,
            )
            sub.fold(deeper)
            result.fold(sub)

    return result


async def step_identity_scope(db: AsyncSession, step) -> Tuple[bool, Optional[datetime]]:
    """(identity_differentiated, latest_upstream_refresh) for a step:
    its query's own identity params OR anything its code consumes."""
    own = False
    org_id = None
    report_id = None
    if getattr(step, "query_id", None):
        from app.models.query import Query
        row = (await db.execute(
            select(Query.parameters, Query.organization_id, Query.report_id)
            .where(Query.id == str(step.query_id))
        )).first()
        if row:
            own = _has_identity_param(row[0])
            org_id, report_id = row[1], row[2]
    taint = await analyze_code_taint(
        db, getattr(step, "code", "") or "",
        organization_id=str(org_id) if org_id else None,
        report_id=str(report_id) if report_id else None,
    )
    return own or taint.identity, taint.latest_upstream_refresh


async def entity_identity_scope(db: AsyncSession, entity) -> Tuple[bool, Optional[datetime]]:
    """(identity_differentiated, latest_upstream_refresh) for an entity."""
    own = _has_identity_param(getattr(entity, "parameters", None))
    taint = await analyze_code_taint(
        db, getattr(entity, "code", "") or "",
        organization_id=str(getattr(entity, "organization_id", "") or "") or None,
        report_id=None,
    )
    return own or taint.identity, taint.latest_upstream_refresh
