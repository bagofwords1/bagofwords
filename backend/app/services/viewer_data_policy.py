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
        row = (await db.execute(
            select(StepUserResult).options(lazyload("*")).where(
                StepUserResult.step_id == str(step.id),
                StepUserResult.user_id == str(requesting_user.id),
            )
        )).scalar_one_or_none()
        if row is not None:
            viewer_result = {
                "status": row.status,
                "status_reason": row.status_reason,
                "executed_as": row.executed_as,
                "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            }
            if row.status == 'success' and row.data:
                return StepDataResolution(data=row.data, viewer_result=viewer_result)

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


async def has_user_scoped_connections(db: AsyncSession, report_id: str) -> bool:
    """True when any connection behind the report's data sources resolves
    credentials per user (anything but system_only)."""
    stmt = (
        select(func.count(Connection.id))
        .select_from(report_data_source_association)
        .join(
            domain_connection,
            domain_connection.c.data_source_id == report_data_source_association.c.data_source_id,
        )
        .join(Connection, Connection.id == domain_connection.c.connection_id)
        .where(
            report_data_source_association.c.report_id == str(report_id),
            Connection.deleted_at.is_(None),
            Connection.auth_policy != 'system_only',
        )
    )
    return bool((await db.execute(stmt)).scalar() or 0)


async def snapshot_withheld_for_viewers(
    db: AsyncSession, report_id: str, shared_run_identity: str | None
) -> bool:
    """True when non-owner readers must not see the shared Step.data snapshot."""
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
