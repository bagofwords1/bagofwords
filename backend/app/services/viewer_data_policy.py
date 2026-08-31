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

    # Identity-scoped queries — declared OR inherited through the loadable
    # dependency graph (a step consuming an identity-scoped step/entity is
    # exactly as identity-differentiated as its input): the shared snapshot
    # is ONE identity's row slice. It must never be served to a different
    # viewer; they get their own per-viewer run (the host triggers it on
    # load for signed-in viewers) or nothing.
    from app.services.identity_taint import step_identity_scope
    _tainted, _ = await step_identity_scope(db, step)
    if _tainted:
        return StepDataResolution(data={}, withheld=True, viewer_result=viewer_result)

    # No successful result of their own. Withhold the credential-differentiated
    # snapshot when the policy applies; otherwise serve it (system-only /
    # creator mode / plain sharing).
    step_code = getattr(step, "code", None)
    if report is not None:
        report_id = getattr(report, "id", None)
        withheld = (
            report_id is not None
            and await snapshot_withheld_for_viewers(
                db, str(report_id), getattr(report, "shared_run_identity", None),
                fallback_org_id=getattr(report, "organization_id", None),
                code=step_code,
            )
        )
    else:
        # Caller had no report in hand — resolve the policy from the step's
        # own report_id.
        report_id = getattr(step, "report_id", None) or (
            getattr(step.query, "report_id", None) if getattr(step, "query", None) else None
        )
        withheld = report_id is not None and await report_snapshot_withheld(
            db, str(report_id), code=step_code
        )

    if withheld:
        return StepDataResolution(data={}, withheld=True, viewer_result=viewer_result)
    return StepDataResolution(data=shared, viewer_result=viewer_result)


def redact_applied_params(
    applied: Optional[dict], parameters: Any, *, withheld: bool
) -> Optional[dict]:
    """What a non-owner reader may see of a step's `applied_params`.

    The values a snapshot was materialized with include, for an
    identity-sourced param, an identity that is NOT the reader's — an email,
    a department, a group list. That crosses exactly the boundary `code` and
    `data` are already gated on, so it gets the same treatment: a withheld
    reader gets nothing, and identity-derived names are dropped for everyone
    else (the shared-report routes serve anonymous readers).

    `input_identity_default` is dropped too. Once resolved there is no way to
    tell a client-supplied value from the identity fallback, so this fails
    closed. Nothing reads identity values back on the client — the dashboard
    controls skip them when seeding filter state.

    `parameters` is the raw Query.parameters list; returns None rather than an
    empty dict so the field stays absent instead of shipping `{}`.
    """
    if withheld or not applied:
        return None
    from app.schemas.param_schema import parse_param_specs

    identity_names = {
        s.name for s in parse_param_specs(parameters)
        if s.source in ("identity", "input_identity_default")
    }
    if not identity_names:
        return dict(applied) or None
    return {k: v for k, v in applied.items() if k not in identity_names} or None


async def _org_fallback_ds_ids(
    db: AsyncSession, organization_id: str | None, code: str | None
) -> list[str]:
    """Data-source ids a DS-less report/entity may have executed against.

    Chat-created reports don't associate data sources with the report row —
    execution falls back to the org's data sources and generated code addresses
    clients by "<data source name>:<connection>" keys (see
    QueryService._report_data_sources). The withholding policy must mirror that
    fallback or a snapshot built on a user-scoped source slips past it. When the
    step/entity code is available, only the sources it names by key are counted
    (a Membership-DB report must not start withholding because the org also has
    a delegated Power BI source); with no code to inspect, all org sources
    count — over-withholding is the safe direction.
    """
    if not organization_id:
        return []
    from app.models.data_source import DataSource
    rows = (await db.execute(
        select(DataSource.id, DataSource.name).where(
            DataSource.organization_id == str(organization_id),
            DataSource.deleted_at.is_(None),
        )
    )).all()
    if code:
        hits = [str(r[0]) for r in rows if r[1] and str(r[1]) in code]
        if hits:
            return hits
    return [str(r[0]) for r in rows]


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
    # Identity-parameterized entities — declared or inherited through their
    # load_entity dependencies: the shared snapshot is the owner's identity
    # slice by construction — withhold it from every other reader (they
    # resolve their own slice via entity_user_results).
    try:
        if any(
            isinstance(p, dict) and p.get("source") == "identity"
            for p in (getattr(entity, "parameters", None) or [])
        ):
            return True
        if db is not None:
            from app.services.identity_taint import entity_identity_scope
            _tainted, _ = await entity_identity_scope(db, entity)
            if _tainted:
                return True
    except Exception:
        pass
    ids = await _entity_data_source_ids(db, str(entity.id))
    if not ids:
        # Entities promoted from chat steps carry no data-source association —
        # they executed against the org-level fallback, exactly like DS-less
        # reports. Evaluate the org sources their code addresses.
        ids = await _org_fallback_ds_ids(
            db, getattr(entity, "organization_id", None), getattr(entity, "code", None)
        )
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
    db: AsyncSession, report_id: str, shared_run_identity: str | None,
    *, fallback_org_id: str | None = None, code: str | None = None,
) -> bool:
    """True when non-owner readers must not see the shared Step.data snapshot.

    RLS relations withhold UNCONDITIONALLY — creator mode is blocked for RLS
    reports (it would hand the owner's row slice to everyone, bypassing the
    policy), so an RLS snapshot is never legitimately shareable as-is. A
    user_required source is exempt in creator mode, where the owner has
    explicitly opted to share their own credential's view.

    A report with no data-source association executed against the org-level
    fallback, so when `fallback_org_id` is given the policy evaluates the org
    sources the step's `code` addresses instead of an empty set.
    """
    ds_ids = await _report_data_source_ids(db, report_id)
    if not ds_ids and fallback_org_id:
        ds_ids = await _org_fallback_ds_ids(db, fallback_org_id, code)
    if await _any_rls_relation(db, ds_ids):
        return True
    if (shared_run_identity or 'viewer') != 'viewer':
        return False
    return await _any_user_scoped_connection(db, ds_ids)


async def sources_credential_scoped(
    db: AsyncSession, report_id: str, *,
    fallback_org_id: str | None = None, code: str | None = None,
    memo: dict | None = None,
) -> bool:
    """True when the report's sources include a user-scoped (delegated)
    connection — runs authenticate per user, so a View-as preview cannot
    reproduce another user's source-level rows (it swaps identity params only;
    credentials stay the caller's). Deliberately excludes bow-RLS relations:
    those filter from the resolved identity server-side, so View-as is
    accurate for them.

    `memo` (caller-owned dict) collapses repeat lookups when annotating a
    whole query list: DS-associated reports resolve once per report; DS-less
    reports resolve once per (report, code) since the org fallback is filtered
    by the data-source names the code addresses.
    """
    ds_ids = await _report_data_source_ids(db, str(report_id))
    key = (str(report_id), None if ds_ids else (code or ""))
    if memo is not None and key in memo:
        return memo[key]
    if not ds_ids and fallback_org_id:
        ds_ids = await _org_fallback_ds_ids(db, fallback_org_id, code)
    result = await _any_user_scoped_connection(db, ds_ids)
    if memo is not None:
        memo[key] = result
    return result


async def report_snapshot_withheld(
    db: AsyncSession, report_id: str, *, code: str | None = None
) -> bool:
    """Same policy, loading the report's mode itself (for callers that only
    have a report_id, e.g. the email/PDF paths)."""
    from app.models.report import Report

    row = (await db.execute(
        select(Report.shared_run_identity, Report.organization_id)
        .where(Report.id == str(report_id))
    )).first()
    if row is None:
        return False
    return await snapshot_withheld_for_viewers(
        db, report_id, row[0], fallback_org_id=row[1], code=code
    )
