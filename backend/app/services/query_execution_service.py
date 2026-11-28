"""Reads and the manager kill for a connection's query history (Queries view).

Rows are written by the query path (``app.data_sources.query_executions``);
this service only lists them for a connection's managers and records a kill
request. Access is gated at the route (``manage_connection`` on the
connection); every read here is also scoped to the organization and the
connection, so an id from another connection or org is simply not found.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Text, func, or_, select, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.data_sources import query_executions
from app.errors import AppError, ErrorCode
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.models.query_execution import QueryExecution
from app.models.report import Report
from app.models.user import User

HISTORY_PAGE_MAX = 200
# Search over encrypted query text cannot run in SQL: the newest rows are
# decrypted and matched in Python, up to this many per search.
ENCRYPTED_SEARCH_SCAN_MAX = 5000
ENCRYPTED_SEARCH_BATCH = 500


async def _db_now(db: AsyncSession) -> datetime:
    """The database's clock (naive UTC): leases are written with it, so they
    are judged by it too (``query_executions._now_sql``)."""
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        return (await db.execute(select(func.timezone("UTC", func.clock_timestamp())))).scalar()
    return datetime.utcnow()


def effective_status(row: QueryExecution, now: datetime | None = None) -> str:
    """A live row whose lease ran out belongs to a dead worker: show it stale."""
    now = now or datetime.utcnow()
    if row.status in query_executions.LIVE_STATUSES and (
        row.lease_expires_at is None or row.lease_expires_at < now
    ):
        return "stale"
    return row.status


def _live_filter(now: datetime):
    return (
        QueryExecution.status.in_(query_executions.LIVE_STATUSES),
        QueryExecution.lease_expires_at > now,
    )


async def _connection(db: AsyncSession, organization: Organization, connection_id: str) -> Connection:
    connection = (await db.execute(
        select(Connection).where(
            Connection.id == str(connection_id),
            Connection.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()
    if connection is None:
        raise AppError.not_found(ErrorCode.CONNECTION_NOT_FOUND)
    return connection


async def _effective_limit(db: AsyncSession, organization: Organization, connection: Connection) -> dict[str, Any]:
    import json

    from app.data_sources.query_concurrency import DEFAULT_MAX_CONCURRENT_QUERIES

    config = connection.config
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}
    config = config if isinstance(config, dict) else {}
    own = config.get("max_concurrent_queries")
    if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0:
        return {"limit": int(own), "limit_source": "connection"}
    try:
        settings = await organization.get_settings(db)
        cfg = settings.get_config("max_concurrent_queries_per_connection") if settings is not None else None
        value = cfg.value if hasattr(cfg, "value") else cfg
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return {"limit": int(value), "limit_source": "organization"}
    except Exception:
        pass
    return {"limit": DEFAULT_MAX_CONCURRENT_QUERIES, "limit_source": "default"}


def _reason_code(row: QueryExecution, status: str) -> str | None:
    """A stable code for outcomes BOW itself causes, so the UI can say them in
    the viewer's language. Errors from the source (SQL errors, timeouts) have
    none and are shown as the source wrote them."""
    if status == "stale":
        return "stale"
    if status == "busy":
        return "busy"
    if status == "cancelled":
        if row.killed_by_user_id:
            return "killed" if row.started_at is not None else "killed_waiting"
        if row.error == "The run was stopped":
            return "run_stopped"
    return None


async def _visible_reports(
    db: AsyncSession, report_ids: set[str], viewer: User | None, organization: Organization | None,
) -> dict[str, Any]:
    """Reports the viewer may open, by id. Managing a connection does not
    grant its reports: a report in a restricted folder, or one built on data
    sources the viewer cannot see, keeps its title and link to itself."""
    if not report_ids:
        return {}
    reports = (await db.execute(
        select(Report).options(selectinload(Report.data_sources)).where(Report.id.in_(report_ids))
    )).scalars().all()
    if viewer is not None and organization is not None:
        from app.core.derived_access import filter_visible_reports

        reports = await filter_visible_reports(db, viewer, organization, reports)
    elif viewer is None:
        reports = []
    return {str(r.id): {"id": str(r.id), "title": r.title} for r in reports}


async def _serialize(
    db: AsyncSession,
    rows: list[QueryExecution],
    *,
    viewer: User | None = None,
    organization: Organization | None = None,
) -> list[dict[str, Any]]:
    now = await _db_now(db)
    user_ids = {r.user_id for r in rows if r.user_id} | {r.killed_by_user_id for r in rows if r.killed_by_user_id}
    ds_ids = {r.data_source_id for r in rows if r.data_source_id}
    report_ids = {r.report_id for r in rows if r.report_id}
    users: dict[str, Any] = {}
    if user_ids:
        for u in (await db.execute(select(User.id, User.name, User.email).where(User.id.in_(user_ids)))).all():
            users[str(u.id)] = {"id": str(u.id), "name": u.name, "email": u.email}
    agents: dict[str, Any] = {}
    if ds_ids:
        for d in (await db.execute(select(DataSource.id, DataSource.name).where(DataSource.id.in_(ds_ids)))).all():
            agents[str(d.id)] = {"id": str(d.id), "name": d.name}
    reports = await _visible_reports(db, report_ids, viewer, organization)

    out = []
    for r in rows:
        status = effective_status(r, now)
        running_ms = None
        if status == "running" and r.started_at is not None:
            running_ms = max(0.0, (now - r.started_at).total_seconds() * 1000.0)
        waiting_ms = None
        if status == "queued" and r.queued_at is not None:
            waiting_ms = max(0.0, (now - r.queued_at).total_seconds() * 1000.0)
        out.append({
            "error_code": _reason_code(r, status),
            "killable": bool(r.killable) if r.killable is not None else True,
            "id": str(r.id),
            "connection_id": str(r.connection_id),
            "status": status,
            "query_text": r.query_text,
            "policy_version": r.policy_version,
            "protected_statement": r.protected_statement,
            "query_truncated": bool(r.query_truncated),
            "source": r.source,
            "client_type": r.client_type,
            "user": users.get(str(r.user_id)) if r.user_id else None,
            "agent": agents.get(str(r.data_source_id)) if r.data_source_id else None,
            "report": reports.get(str(r.report_id)) if r.report_id else None,
            # Only a report the viewer may open is linked.
            "report_id": r.report_id if r.report_id and str(r.report_id) in reports else None,
            "completion_id": r.completion_id,
            "agent_execution_id": r.agent_execution_id,
            "tool_execution_id": r.tool_execution_id,
            "step_id": r.step_id,
            "query_id": r.query_id,
            "entity_id": r.entity_id,
            "tool_attempt": r.tool_attempt,
            "code_attempt": r.code_attempt,
            "seq": r.seq,
            "queued_at": r.queued_at.isoformat() + "Z" if r.queued_at else None,
            "started_at": r.started_at.isoformat() + "Z" if r.started_at else None,
            "ended_at": r.ended_at.isoformat() + "Z" if r.ended_at else None,
            "queued_ms": r.queued_ms if r.queued_ms is not None else waiting_ms,
            "duration_ms": r.duration_ms if r.duration_ms is not None else running_ms,
            "rows": r.rows,
            # A live row whose worker died reads stale at once; say why even
            # before the leader sweep stamps it.
            "error": r.error or (query_executions.STALE_ERROR if status == "stale" else None),
            "cancel_outcome": r.cancel_outcome,
            "kill_requested_at": r.kill_requested_at.isoformat() + "Z" if r.kill_requested_at else None,
            "killed_by": users.get(str(r.killed_by_user_id)) if r.killed_by_user_id else None,
            "worker_id": r.worker_id,
        })
    return out


async def list_queries(
    db: AsyncSession,
    organization: Organization,
    connection_id: str,
    *,
    scope: str = "history",
    statuses: list[str] | None = None,
    user_id: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    viewer: User | None = None,
    policy_only: bool = False,
) -> dict[str, Any]:
    """Live queries, or a page of finished ones, plus the connection's slot summary."""
    connection = await _connection(db, organization, connection_id)
    limit = max(1, min(int(limit or 50), HISTORY_PAGE_MAX))
    offset = max(0, int(offset or 0))

    async def serialize(rows):
        items = await _serialize(db, rows, viewer=viewer, organization=organization)
        if policy_only:
            for item in items:
                for key in ("agent", "report", "error", "completion_id", "report_id", "step_id"):
                    item[key] = None
        return items

    now = await _db_now(db)
    base = select(QueryExecution).where(
        QueryExecution.connection_id == str(connection.id),
        QueryExecution.organization_id == str(organization.id),
    )
    if policy_only:
        base = base.where(QueryExecution.policy_version.is_not(None))
    counts = (await db.execute(
        select(QueryExecution.status, func.count()).where(
            QueryExecution.connection_id == str(connection.id),
            QueryExecution.organization_id == str(organization.id),
            *([QueryExecution.policy_version.is_not(None)] if policy_only else []),
            *_live_filter(now),
        ).group_by(QueryExecution.status)
    )).all()
    summary = {"running": 0, "queued": 0}
    for status, count in counts:
        summary[status] = int(count)
    summary.update(await _effective_limit(db, organization, connection))

    if scope == "running":
        # Only rows whose worker is alive; a dead worker's rows are history
        # (stale). Paged like history, so a burst larger than a page can still
        # be reached (and killed) from the tab.
        stmt = base.where(*_live_filter(now))
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
        rows = (await db.execute(
            stmt.order_by(QueryExecution.queued_at.asc(), QueryExecution.id.asc()).limit(limit).offset(offset)
        )).scalars().all()
        return {
            "summary": summary,
            "items": await serialize(rows),
            "total": int(total),
            "has_more": offset + len(rows) < int(total),
        }

    stmt = base
    wanted = [s for s in (statuses or []) if s and s != "all"]
    if not wanted:
        # History is what has ended; live queries are listed under "running".
        # A live row whose lease ran out (dead worker) is history: it is stale.
        stmt = stmt.where(~(
            QueryExecution.status.in_(query_executions.LIVE_STATUSES)
            & (QueryExecution.lease_expires_at > now)
        ))
    if wanted:
        conditions = []
        concrete = [s for s in wanted if s != "stale"]
        if concrete:
            conditions.append(QueryExecution.status.in_(concrete))
        if "stale" in wanted:
            conditions.append(QueryExecution.status == "stale")
            conditions.append(
                QueryExecution.status.in_(query_executions.LIVE_STATUSES)
                & (QueryExecution.lease_expires_at < now)
            )
        stmt = stmt.where(or_(*conditions))
    if user_id:
        stmt = stmt.where(QueryExecution.user_id == str(user_id))
    newest_first = (QueryExecution.queued_at.desc(), QueryExecution.id.desc())
    if search:
        term = search.strip()
        from app.ee.encryption.types import text_envelope_expr

        # Compare as plain TEXT: through the column's encrypting type the
        # search term (and the envelope prefix pattern) would themselves be
        # encrypted before the comparison. SQL is full of "_" and "%": match
        # them literally, not as wildcards.
        stored = type_coerce(QueryExecution.query_text, Text)
        encrypted = text_envelope_expr(stored)
        plain_match = ~encrypted & stored.icontains(term, autoescape=True)
        has_encrypted = (await db.execute(
            select(func.count()).select_from(stmt.where(encrypted).subquery())
        )).scalar()
        if has_encrypted:
            return await _search_with_encrypted_rows(
                db, stmt.where(or_(plain_match, encrypted)).order_by(*newest_first),
                term, limit, offset, summary, serialize,
            )
        stmt = stmt.where(plain_match)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    rows = (await db.execute(stmt.order_by(*newest_first).limit(limit).offset(offset))).scalars().all()
    return {
        "summary": summary,
        "items": await serialize(rows),
        "total": int(total),
        "has_more": offset + len(rows) < int(total),
    }


async def _search_with_encrypted_rows(db, stmt, term, limit, offset, summary, serialize) -> dict[str, Any]:
    """Search when query text is encrypted at rest (data_encryption enabled).

    Ciphertext cannot be matched in SQL. Plaintext rows are pre-filtered in
    SQL; encrypted ones are decrypted by the column type and matched here,
    newest first, scanning at most ``ENCRYPTED_SEARCH_SCAN_MAX`` rows.
    """
    needle = term.lower()
    matches: list[QueryExecution] = []
    scanned = 0
    exhausted = False
    while scanned < ENCRYPTED_SEARCH_SCAN_MAX and len(matches) <= offset + limit:
        batch = (await db.execute(stmt.limit(ENCRYPTED_SEARCH_BATCH).offset(scanned))).scalars().all()
        scanned += len(batch)
        matches.extend(r for r in batch if needle in (r.query_text or "").lower())
        if len(batch) < ENCRYPTED_SEARCH_BATCH:
            exhausted = True
            break
    page = matches[offset:offset + limit]
    return {
        "summary": summary,
        "items": await serialize(page),
        # Exact when the scan reached the end; otherwise what was found so far.
        "total": len(matches),
        "has_more": len(matches) > offset + limit or not exhausted,
    }


async def kill_query(
    db: AsyncSession,
    organization: Organization,
    connection_id: str,
    query_execution_id: str,
    user: User,
) -> tuple[dict[str, Any], bool]:
    """Ask the worker running this query to cancel it.

    Returns the row and whether THIS call requested the kill (the route audits
    only then: not for a repeat click, nor for closing a dead worker's row).
    """
    connection = await _connection(db, organization, connection_id)
    row = (await db.execute(
        select(QueryExecution).where(
            QueryExecution.id == str(query_execution_id),
            QueryExecution.connection_id == str(connection.id),
            QueryExecution.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()
    if row is None:
        raise AppError.not_found(ErrorCode.CONNECTION_QUERY_NOT_FOUND)
    now = await _db_now(db)
    status = effective_status(row, now)
    requested = False

    async def serialize():
        await db.refresh(row)
        return (await _serialize(db, [row], viewer=user, organization=organization))[0]

    if status == "stale" and row.status in query_executions.LIVE_STATUSES:
        # Its worker is gone: nothing to cancel, just close the row. Only if it
        # is still live with a lapsed lease — a slow worker may have finished it
        # (or renewed it) since the read.
        result = await db.execute(
            update(QueryExecution).where(
                QueryExecution.id == row.id,
                QueryExecution.status.in_(query_executions.LIVE_STATUSES),
                or_(QueryExecution.lease_expires_at.is_(None), QueryExecution.lease_expires_at < now),
            ).values(
                status="stale", ended_at=now, lease_expires_at=None, updated_at=now,
                error=row.error or query_executions.STALE_ERROR,
            )
        )
        await db.commit()
        if result.rowcount:
            return await serialize(), False
        await db.refresh(row)
        status = effective_status(row, now)
    if status not in query_executions.LIVE_STATUSES:
        raise AppError.conflict(ErrorCode.CONNECTION_QUERY_FINISHED, status=status)
    if not row.killable and row.status != "queued":
        # The client enforces its own deadline in line: once running, BOW
        # cannot interrupt it, so do not record (or audit) a kill that would
        # not happen. While it still waits for a slot it can be cancelled.
        raise AppError.conflict(ErrorCode.CONNECTION_QUERY_NOT_KILLABLE)
    if row.kill_requested_at is None:
        # Conditional on still being live (and, for a client BOW cannot
        # interrupt, still queued), so a query that ended or started a moment
        # ago is not stamped as killed.
        conditions = [
            QueryExecution.id == row.id,
            QueryExecution.status.in_(query_executions.LIVE_STATUSES),
            QueryExecution.kill_requested_at.is_(None),
        ]
        if not row.killable:
            conditions.append(QueryExecution.status == "queued")
        result = await db.execute(
            update(QueryExecution).where(*conditions)
            .values(kill_requested_at=now, killed_by_user_id=str(user.id), updated_at=now)
        )
        await db.commit()
        if result.rowcount:
            requested = True
        else:
            await db.refresh(row)
            status = effective_status(row, now)
            if status not in query_executions.LIVE_STATUSES:
                # Finished between the read and the update.
                raise AppError.conflict(ErrorCode.CONNECTION_QUERY_FINISHED, status=status)
            if row.kill_requested_at is None:
                # A client BOW cannot interrupt started meanwhile.
                raise AppError.conflict(ErrorCode.CONNECTION_QUERY_NOT_KILLABLE)
            # Someone else's kill landed first: theirs is the one on record.
    # Same-worker fast path; other workers pick it up on their next tick.
    query_executions.request_kill_local(str(row.id))
    return await serialize(), requested
