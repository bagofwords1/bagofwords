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
    the decision is made against the conversation the step belongs to
    (app/services/row_owner.py). A step that belongs to no conversation — a
    published dashboard's copy, or an orphan — is withheld.
    """
    from app.services.bow_source_access import step_access, can_read
    step_report_id = None
    if report is None:
        from app.services.row_owner import step_report_id as _owner_report_id
        step_report_id = await _owner_report_id(db, step)
        if step_report_id is None:
            return StepDataResolution(data={}, withheld=True)
    bow_access = await step_access(db, step)
    if bow_access and not await can_read(db, bow_access, requesting_user):
        return StepDataResolution(data={}, withheld=True)
    shared = step.data or {}

    owner_id = str(report.user_id) if report is not None and getattr(report, "user_id", None) else None
    if requesting_user is not None and owner_id is not None and str(requesting_user.id) == owner_id:
        return StepDataResolution(data=shared)

    viewer_result = None
    if requesting_user is not None:
        from app.models.step_user_result import StepUserResult
        # A viewer can hold one cached result per parameter combination. The
        # initial render shows the dashboard's DEFAULT values (that is what
        # every host's controls start at), so serve the slice for those —
        # serving whichever slice ran last showed e.g. one region's rows
        # under a control reading "All". Other slices stay cached for when
        # the viewer picks those values again.
        stmt = select(StepUserResult).options(lazyload("*")).where(
            StepUserResult.step_id == str(step.id),
            StepUserResult.user_id == str(requesting_user.id),
        )
        default_fp = await _default_params_fingerprint(db, step, report, requesting_user)
        if default_fp is not None:
            # '' is the "no params / defaults" key (StepUserResult) — rows
            # written before a query declared params, or without values.
            stmt = stmt.where(StepUserResult.params_fingerprint.in_({default_fp, ""}))
        row = (await db.execute(
            stmt.order_by(StepUserResult.last_run_at.desc())
        )).scalars().first()
        if row is not None and await creator_result_holds_owner_slice(
            db, row, report.id if report is not None else step_report_id, getattr(step, "code", None)
        ):
            row = None
        if row is not None:
            from app.services.access_errors import viewer_error_code
            viewer_result = {
                "status": row.status,
                "status_reason": row.status_reason,
                # Machine-readable, so the dashboard can tell "you may not see
                # this chart" from a query that broke — see access_errors.
                "error_code": viewer_error_code(row.status, row.status_reason),
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
                code=step_code, bow_authorized=bool(bow_access),
            )
        )
    else:
        # Caller had no report in hand — resolve the policy from the
        # conversation the step belongs to.
        withheld = await report_snapshot_withheld(db, str(step_report_id), code=step_code)

    if withheld:
        return StepDataResolution(data={}, withheld=True, viewer_result=viewer_result)
    return StepDataResolution(data=shared, viewer_result=viewer_result)


async def step_run_data_source_ids(db: AsyncSession, report_id: str, code: str | None) -> list[str] | None:
    """The data sources one step of a report runs on: the report's attached
    ones, else the organization sources named by the step's code and by the
    code of every step it load_step()s (loaded_step_codes), each counting all
    of them when it names none -- the per-step rule
    snapshot_withheld_for_viewers applies. None when the report does not
    exist."""
    ds_ids = await _report_data_source_ids(db, report_id)
    if ds_ids:
        return ds_ids
    from app.models.query import Query
    from app.models.report import Report
    org_id = (await db.execute(
        select(Report.organization_id).where(Report.id == str(report_id))
    )).scalar_one_or_none()
    if org_id is None:
        return None
    codes = await loaded_step_codes(db, code, Query.report_id == str(report_id))
    return fallback_ds_ids_for_codes(await _org_data_sources(db, org_id), codes)


async def creator_result_holds_owner_slice(
    db: AsyncSession, row: Any, report_id: Any, code: str | None
) -> bool:
    """True when a viewer's stored result ran under the owner's credentials
    (executed_as 'creator') for a step whose sources -- its own and those of
    the steps it loads (step_run_data_source_ids with that step's `code`) --
    include a row-filtered relation: its rows were filtered for the owner, so
    they are not that viewer's to serve or reuse. Also True when the step's
    report cannot be resolved. Asked afresh on every call, so a change to the
    sources or their filters is seen at once; any other row returns False
    without a query."""
    if getattr(row, "executed_as", None) != "creator":
        return False
    if not report_id:
        return True
    ds_ids = await step_run_data_source_ids(db, str(report_id), code)
    return ds_ids is None or await _any_rls_relation(db, ds_ids)


def may_load_steps(code: str | None) -> bool:
    """Whether `code` can call load_step. Python reads identifiers in NFKC
    form (`load＿step` is `load_step`), so the text is normalized first."""
    import unicodedata
    return bool(code) and "load_step" in unicodedata.normalize("NFKC", code)


async def _loadable_steps(db: AsyncSession, scope) -> list:
    """(id, code, slug, title) of every successful step of the queries in
    `scope` (a Query filter: a report's queries or a release's copies),
    deleted queries included. Not only the current default steps: a stored
    snapshot was built from whichever step its load_step() reference matched
    then, which may since have been superseded by a newer run."""
    from app.models.query import Query
    from app.models.step import Step

    return (await db.execute(
        select(Step.id, Step.code, Step.slug, Step.title)
        .join(Query, Query.id == Step.query_id)
        .where(scope, Step.status == "success")
    )).all()


async def loaded_step_codes(db: AsyncSession, code: str | None, scope) -> list[str]:
    """`code` plus the code of every step it load_step()s, transitively,
    among the steps of `scope` (_loadable_steps). [] for no code. Code that
    cannot call load_step (may_load_steps) costs no query."""
    if not code or not code.strip():
        return []
    if not may_load_steps(code):
        return [code]
    return closure_codes(await _loadable_steps(db, scope), [code])


def closure_codes(loadable: list, codes: list[str]) -> list[str]:
    """`codes` plus the code of every step of `loadable` ((id, code, slug,
    title) rows) they reach through load_step, transitively. A reference
    counts every step it can match -- by id, by slug or by title, any of them
    current or not -- so a match is never missed. A reached step with no code
    is returned as "" (it names no source, so all of them count)."""
    from collections import defaultdict
    from app.ai.code_execution.loadables import extract_loadable_refs

    by_ref: dict[str, list] = defaultdict(list)
    for row in loadable:
        by_ref[str(row[0])].append(row)
        for key in (row[2], row[3]):
            if key:
                by_ref[str(key).lower()].append(row)

    out = list(codes)
    seen: set[str] = set()
    todo = [c for c in codes if may_load_steps(c)]
    while todo:
        refs, _ = extract_loadable_refs(todo.pop())
        for ref in refs:
            for row in by_ref.get(str(ref), []) + by_ref.get(str(ref).lower(), []):
                if str(row[0]) in seen:
                    continue
                seen.add(str(row[0]))
                out.append(row[1] or "")
                if may_load_steps(row[1]):
                    todo.append(row[1])
    return out


async def _default_params_fingerprint(db: AsyncSession, step: Any, report: Any, user: Any) -> Optional[str]:
    """params_fingerprint of this viewer's DEFAULT parameter values for the
    step's query — the key the host's initial viewer run caches under (same
    resolve_param_values + params_fingerprint as query_service's viewer run).

    None when it can't be determined (no query, unresolvable defaults); the
    caller then keeps the most-recent-slice behavior.
    """
    try:
        from app.models.query import Query
        from app.schemas.param_schema import parse_param_specs
        from app.ai.code_execution.query_params import params_fingerprint, resolve_param_values

        query_id = getattr(step, "query_id", None)
        if not query_id:
            return None
        row = (await db.execute(
            select(Query.parameters, Query.organization_id).where(Query.id == str(query_id))
        )).first()
        if row is None:
            return None
        specs = parse_param_specs(row[0])
        if not specs:
            return params_fingerprint({})
        org_id = row[1] or getattr(report, "organization_id", None)
        if not org_id:
            return None
        # One identity resolution per (viewer, org) per session: a dashboard
        # resolves every step through here.
        memo = db.info.setdefault("_default_params_identity", {})
        key = (str(user.id), str(org_id))
        identity = memo.get(key)
        if identity is None:
            from app.services.rls_identity_service import resolve_identity
            identity = await resolve_identity(db, user, str(org_id))
            memo[key] = identity
        return params_fingerprint(resolve_param_values(specs, {}, identity))
    except Exception:
        return None


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
    rows = await _org_data_sources(db, organization_id)
    hits = _named_in_code(rows, code)
    if hits:
        return hits
    return [str(r[0]) for r in rows]


async def _org_data_sources(db: AsyncSession, organization_id: str | None) -> list:
    """(id, name) of the organization's live data sources."""
    if not organization_id:
        return []
    from app.models.data_source import DataSource
    return (await db.execute(
        select(DataSource.id, DataSource.name).where(
            DataSource.organization_id == str(organization_id),
            DataSource.deleted_at.is_(None),
        )
    )).all()


def _named_in_code(rows: list, code: str | None) -> list[str]:
    """Ids of the (id, name) rows whose name the code contains — how generated
    code reaches a source (`ds_clients["<data source name>:<connection>"]`)."""
    if not code:
        return []
    return [str(r[0]) for r in rows if r[1] and str(r[1]) in code]


async def org_sources_named_in_code(
    db: AsyncSession, organization_id: str | None, code: str | None
) -> list[str]:
    """Ids of the organization's data sources the code names — and only
    those: unlike _org_fallback_ds_ids, code naming none gives none."""
    return _named_in_code(await _org_data_sources(db, organization_id), code)


async def _report_step_codes(db: AsyncSession, report_id: str) -> list[str]:
    """The non-empty code of the steps the report serves and reruns, and of
    the steps those load_step(). For each live query of the report -- its own
    (Query.report_id) or, for a legacy query, its widget's (as
    app/services/row_owner.py finds the owner) -- the step a rerun executes
    (ReportService._rerun_target_steps, which mirrors
    get_default_step_for_query), then every step load_step reaches from
    them (closure_codes). Earlier attempts (a failed run's step, a
    superseded version) are neither served nor run, so they do not count
    unless a served step's load_step() reference reaches them."""
    from sqlalchemy import or_
    from app.models.query import Query
    from app.models.widget import Widget
    from app.services.report_service import ReportService

    query_ids = (await db.execute(
        select(Query.id).where(
            or_(Query.report_id == str(report_id),
                Query.widget_id.in_(select(Widget.id).where(Widget.report_id == str(report_id)))),
            Query.deleted_at.is_(None),
        )
    )).scalars().all()
    targets, _ = await ReportService()._rerun_target_steps(db, [str(q) for q in query_ids], quiet=True)
    codes = [c for _, c in targets if c and c.strip()]
    if not any(may_load_steps(c) for c in codes):
        return codes
    return closure_codes(await _loadable_steps(db, Query.report_id == str(report_id)), codes)


async def report_steps_code(db: AsyncSession, report_id: str) -> str | None:
    """The code of every step of the report, joined with newlines; None when
    none has code."""
    return "\n".join(await _report_step_codes(db, report_id)) or None


def fallback_ds_ids_for_codes(rows: list, codes: list[str]) -> list[str]:
    """The organization sources (rows: (id, name)) a set of steps runs on,
    decided per step as the page decides it (snapshot_withheld_for_viewers
    with that step's code): each step counts the sources its own code names,
    or every source when it names none. The union over the steps; every
    source when no step has code. A step naming a plain source therefore
    never narrows a sibling step that reaches its clients without naming
    them."""
    all_ids = [str(r[0]) for r in rows]
    if not codes:
        return all_ids
    picked: set[str] = set()
    for code in codes:
        picked.update(_named_in_code(rows, code) or all_ids)
        if len(picked) == len(all_ids):
            break
    return [i for i in all_ids if i in picked]


async def report_run_data_source_ids(db: AsyncSession, report_id: str) -> list[str]:
    """The data sources a report's runs use: the attached ones, else the
    organization's, step by step (fallback_ds_ids_for_codes) --
    QueryService._report_data_sources builds clients for the whole
    organization set. The same rule as release_run_data_source_ids for a
    published dashboard."""
    ds_ids = await _report_data_source_ids(db, report_id)
    if ds_ids:
        return ds_ids
    from app.models.report import Report
    org_id = (await db.execute(
        select(Report.organization_id).where(Report.id == str(report_id))
    )).scalar_one_or_none()
    if not org_id:
        return []
    return fallback_ds_ids_for_codes(
        await _org_data_sources(db, org_id), await _report_step_codes(db, report_id))


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
    """True when any connection behind the data sources the report runs on
    (report_run_data_source_ids) resolves credentials per user (anything but
    system_only)."""
    return await _any_user_scoped_connection(db, await report_run_data_source_ids(db, report_id))


async def has_rls_relations(db: AsyncSession, report_id: str) -> bool:
    """True when the report reads an accelerated relation with row-level
    security enabled — the shared Step.data snapshot is then the OWNER's row
    slice and must be withheld from other viewers exactly as a user_required
    source would be. Asked of the data sources the report runs on
    (report_run_data_source_ids), attached or not."""
    return await _any_rls_relation(db, await report_run_data_source_ids(db, report_id))


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
    from app.services.bow_source_access import can_read
    bow_access = getattr(entity, "bow_source_access", None)
    if bow_access and not await can_read(db, bow_access, requesting_user):
        return True
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
    bow_authorized: bool = False,
) -> bool:
    """True when non-owner readers must not see the shared Step.data snapshot.

    RLS relations withhold UNCONDITIONALLY — creator mode is blocked for RLS
    reports (it would hand the owner's row slice to everyone, bypassing the
    policy), so an RLS snapshot is never legitimately shareable as-is. A
    user_required source is exempt in creator mode, where the owner has
    explicitly opted to share their own credential's view.

    A report with no data-source association executed against the org-level
    fallback, so when `fallback_org_id` is given the policy evaluates the org
    sources the step's `code` addresses -- and those addressed by the steps it
    load_step()s, whose rows it holds (loaded_step_codes) -- instead of an
    empty set.
    """
    from app.services.bow_source_access import report_access
    if not bow_authorized and await report_access(db, report_id):
        return True
    ds_ids = await _report_data_source_ids(db, report_id)
    if not ds_ids and fallback_org_id:
        from app.models.query import Query
        codes = await loaded_step_codes(db, code, Query.report_id == str(report_id))
        ds_ids = fallback_ds_ids_for_codes(await _org_data_sources(db, fallback_org_id), codes)
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
