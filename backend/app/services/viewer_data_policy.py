"""Policy: when must a shared artifact's creator snapshot be hidden from viewers?

A shared dashboard's Step.data snapshot is materialized under the CREATOR's
data-source credentials. When the report is in viewer-identity mode
(reports.shared_run_identity == 'viewer') and its data comes from a
user-scoped connection (auth_policy != 'system_only', i.e. every user
queries with their own credentials), showing that snapshot to other users
leaks rows their own credentials would never return. In that combination,
non-owner readers get no snapshot — they see their own step_user_results
after running, or nothing.

System-only connections are exempt: everyone's credentials are the same
ones, so the snapshot is not credential-differentiated and hiding it would
only regress plain sharing. 'creator' mode is exempt by definition — the
owner explicitly opted to run and share their own view.

Every surface that serves step rows — the read endpoints, CSV/XLSX export,
PDF/thumbnail/PPTX renders, the websocket step broadcast, Slack step
notifications — must resolve what a given reader may see through
resolve_step_data (or report_snapshot_withheld for report-level renders with
no user in scope) instead of reading Step.data directly. See the comment
banner on the Step.data column.
"""
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.models.connection import Connection
from app.models.domain_connection import domain_connection
from app.models.report_data_source_association import report_data_source_association


@dataclass
class StepDataResolution:
    """What a specific reader is allowed to see for a step.

    Produced by resolve_step_data — the single authority every surface that
    serves step rows (read endpoints, CSV/XLSX export, PDF/thumbnail/PPTX
    renders, websocket broadcasts) must go through instead of reading
    Step.data directly. `data` is already the correct payload for the
    requesting user; readers must not fall back to step.data when withheld.
    """
    data: dict = field(default_factory=dict)
    withheld: bool = False
    viewer_result: Optional[dict] = None


async def resolve_step_data(
    db: AsyncSession,
    step: Any,
    report: Any,
    requesting_user: Any = None,
) -> StepDataResolution:
    """Resolve the step rows a given reader may see.

    - The report owner (and callers with no report context) get the shared
      Step.data snapshot.
    - A non-owner (or anonymous) reader gets their own successful
      step_user_results row if one exists; otherwise the shared snapshot,
      UNLESS the withholding policy applies (viewer-identity mode on a
      user-scoped connection), in which case they get nothing until they run
      as themselves.

    `report` may be None (report-level renders with no report loaded) — then
    only the owner/snapshot vs. withheld decision is made via report_id on
    the step's query, falling back to serving the snapshot.
    """
    shared = step.data or {}

    owner_id = str(report.user_id) if report is not None and getattr(report, "user_id", None) else None
    if requesting_user is not None and owner_id is not None and str(requesting_user.id) == owner_id:
        return StepDataResolution(data=shared)

    viewer_result = None
    if requesting_user is not None:
        from app.models.step_user_result import StepUserResult
        # A viewer can hold one cached result per parameter combination —
        # serve their most recent successful slice for the initial render.
        row = (await db.execute(
            select(StepUserResult).options(lazyload("*")).where(
                StepUserResult.step_id == str(step.id),
                StepUserResult.user_id == str(requesting_user.id),
            ).order_by(StepUserResult.last_run_at.desc())
        )).scalars().first()
        if row is not None:
            viewer_result = {
                "status": row.status,
                "status_reason": row.status_reason,
                "executed_as": row.executed_as,
                "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            }
            if row.status == 'success' and row.data:
                return StepDataResolution(data=row.data, viewer_result=viewer_result)

    # Identity-scoped queries: the shared snapshot is ONE identity's row
    # slice (whoever produced it — usually the creator). It must never be
    # served to a different viewer; they get their own per-viewer run (the
    # host triggers it on load for signed-in viewers) or nothing.
    if await _step_query_has_identity_params(db, step):
        return StepDataResolution(data={}, withheld=True, viewer_result=viewer_result)

    # No successful result of their own. Withhold the credential-differentiated
    # snapshot when the policy applies; otherwise serve it (system-only /
    # creator mode / plain sharing).
    if report is not None:
        report_id = getattr(report, "id", None)
        withheld = (
            report_id is not None
            and await snapshot_withheld_for_viewers(
                db, str(report_id), getattr(report, "shared_run_identity", None)
            )
        )
    else:
        # Caller had no report in hand — resolve the policy from the step's
        # own report_id.
        report_id = getattr(step, "report_id", None) or (
            getattr(step.query, "report_id", None) if getattr(step, "query", None) else None
        )
        withheld = report_id is not None and await report_snapshot_withheld(db, str(report_id))

    if withheld:
        return StepDataResolution(data={}, withheld=True, viewer_result=viewer_result)
    return StepDataResolution(data=shared, viewer_result=viewer_result)


async def _step_query_has_identity_params(db: AsyncSession, step) -> bool:
    """Does the step's query declare any identity-source parameter?"""
    query_id = getattr(step, "query_id", None)
    if not query_id:
        return False
    from app.models.query import Query
    row = (await db.execute(
        select(Query.parameters).where(Query.id == str(query_id))
    )).first()
    if not row or not row[0]:
        return False
    try:
        return any(
            isinstance(p, dict) and p.get("source") == "identity"
            for p in row[0]
        )
    except Exception:
        return False


async def _report_data_source_ids(db: AsyncSession, report_id: str) -> list[str]:
    rows = (await db.execute(
        select(report_data_source_association.c.data_source_id).where(
            report_data_source_association.c.report_id == str(report_id)
        )
    )).all()
    return [r[0] for r in rows]


async def _entity_data_source_ids(db: AsyncSession, entity_id: str) -> list[str]:
    from app.models.entity import entity_data_source_association
    rows = (await db.execute(
        select(entity_data_source_association.c.data_source_id).where(
            entity_data_source_association.c.entity_id == str(entity_id)
        )
    )).all()
    return [r[0] for r in rows]


async def _any_user_scoped_connection(db: AsyncSession, data_source_ids: list[str]) -> bool:
    """True when any connection behind the given data sources resolves
    credentials per user (anything but system_only)."""
    if not data_source_ids:
        return False
    stmt = (
        select(func.count(Connection.id))
        .select_from(domain_connection)
        .join(Connection, Connection.id == domain_connection.c.connection_id)
        .where(
            domain_connection.c.data_source_id.in_(data_source_ids),
            Connection.deleted_at.is_(None),
            Connection.auth_policy != 'system_only',
        )
    )
    return bool((await db.execute(stmt)).scalar() or 0)


async def _any_rls_relation(db: AsyncSession, data_source_ids: list[str]) -> bool:
    """True when any of the given data sources exposes an active bow relation
    with row-level security enabled.

    Built-in RLS (custom-query / DuckDB acceleration) filters a shared,
    single-credential materialization per requesting user, so a shared
    snapshot is materialized as ONE identity's row slice — a different
    identity-differentiation than user_required auth, and one that lives on a
    system_only connection (RLS is only permitted there today).
    """
    if not data_source_ids:
        return False
    from app.models.connection_table import ConnectionTable, KIND_BOW
    from app.models.datasource_table import DataSourceTable

    stmt = (
        select(func.count(ConnectionTable.id))
        .select_from(DataSourceTable)
        .join(ConnectionTable, ConnectionTable.id == DataSourceTable.connection_table_id)
        .where(
            DataSourceTable.datasource_id.in_(data_source_ids),
            DataSourceTable.is_active.is_(True),
            DataSourceTable.deleted_at.is_(None),
            ConnectionTable.kind == KIND_BOW,
            ConnectionTable.rls_enabled.is_(True),
            ConnectionTable.deleted_at.is_(None),
        )
    )
    return bool((await db.execute(stmt)).scalar() or 0)


async def has_user_scoped_connections(db: AsyncSession, report_id: str) -> bool:
    """True when any connection behind the report's data sources resolves
    credentials per user (anything but system_only)."""
    return await _any_user_scoped_connection(db, await _report_data_source_ids(db, report_id))


async def has_rls_relations(db: AsyncSession, report_id: str) -> bool:
    """True when the report reads an accelerated relation with row-level
    security enabled — the shared Step.data snapshot is then the OWNER's row
    slice and must be withheld from other viewers exactly as a user_required
    source would be."""
    return await _any_rls_relation(db, await _report_data_source_ids(db, report_id))


async def entity_data_withheld(
    db: AsyncSession, entity: Any, requesting_user: Any = None
) -> bool:
    """True when a non-owner must not see a shared Entity.data snapshot.

    Entities (global/approved ones are org-visible via GET /entities/{id})
    carry a single materialized `data` snapshot produced under one identity's
    credentials via the standard client-construction path, so it honors
    user_required auth and RLS. When the entity reads such a
    credential-differentiated source, the snapshot is one identity's row slice
    and serving it to another user leaks rows their own credentials would
    never return.

    The entity owner is the trusted identity (the report analog): they see the
    snapshot; every other reader is withheld. Entities have no per-user result
    store, so a withheld reader gets empty data and must run/refresh the entity
    themselves to see their own rows. (Refresh persistence honors the same
    predicate: run_entity_with_update only writes the shared snapshot when this
    returns False for the runner, so a non-owner refresh on a user-scoped
    source stays transient.)
    """
    owner_id = str(getattr(entity, "owner_id", "") or "")
    if requesting_user is not None and owner_id and str(requesting_user.id) == owner_id:
        return False
    ids = await _entity_data_source_ids(db, str(entity.id))
    return await _any_user_scoped_connection(db, ids) or await _any_rls_relation(db, ids)


async def resolve_entity_data(
    db: AsyncSession, entity: Any, requesting_user: Any = None
) -> dict:
    """The entity snapshot a given reader may see — {} when withheld.

    Single authority for every surface that serves Entity.data rows: the
    entity read endpoint, the planner context builders (entities section,
    mentions), code-execution loadables and the describe_entity tool must
    resolve through this instead of reading Entity.data directly, exactly
    like step surfaces go through resolve_step_data.
    """
    if await entity_data_withheld(db, entity, requesting_user):
        return {}
    return entity.data or {}


async def snapshot_withheld_for_viewers(
    db: AsyncSession, report_id: str, shared_run_identity: str | None
) -> bool:
    """True when non-owner readers must not see the shared Step.data snapshot.

    RLS relations withhold UNCONDITIONALLY — creator mode is blocked for RLS
    reports (it would hand the owner's row slice to everyone, bypassing the
    policy), so an RLS snapshot is never legitimately shareable as-is. A
    user_required source is exempt in creator mode, where the owner has
    explicitly opted to share their own credential's view.
    """
    if await has_rls_relations(db, report_id):
        return True
    if (shared_run_identity or 'viewer') != 'viewer':
        return False
    return await has_user_scoped_connections(db, report_id)


async def report_snapshot_withheld(db: AsyncSession, report_id: str) -> bool:
    """Same policy, loading the report's mode itself (for callers that only
    have a report_id, e.g. the email/PDF paths)."""
    from app.models.report import Report

    row = (await db.execute(
        select(Report.shared_run_identity).where(Report.id == str(report_id))
    )).first()
    if row is None:
        return False
    return await snapshot_withheld_for_viewers(db, report_id, row[0])
