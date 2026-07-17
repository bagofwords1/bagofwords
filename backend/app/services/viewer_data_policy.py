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

Used by the step read paths (report_service.get_public_step,
query_service.get_default_step_for_query) and by every email path that
attaches a snapshot-rendered PDF (notification_service).
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.domain_connection import domain_connection
from app.models.report_data_source_association import report_data_source_association


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
