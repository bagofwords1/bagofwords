# Audit Log Service
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import logging
from typing import Optional, List, Tuple
from datetime import datetime, timezone
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi import Request

from app.ee.audit import context as audit_context
from app.ee.audit.content_policy import sanitize_details
from app.ee.audit.contract import ACTOR_UNKNOWN, SCHEMA_VERSION, actor_type_for, normalize_outcome
from app.ee.audit.models import AuditLog
from app.ee.audit.schemas import AuditLogResponse, AuditLogFilters
from app.models.user import User

logger = logging.getLogger(__name__)


def _naive_utc(dt: datetime) -> datetime:
    """audit_logs.created_at is naive UTC; normalize aware filter values."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class AuditService:
    """Service for managing audit logs"""

    async def log(
        self,
        db: AsyncSession,
        organization_id: Optional[str],
        action: str,
        user_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[dict] = None,
        request: Optional[Request] = None,
        commit: bool = True,
        *,
        outcome: Optional[str] = None,
        failure_reason: Optional[str] = None,
        actor: Optional[User] = None,
        actor_snapshot: Optional[dict] = None,
        occurred_at: Optional[datetime] = None,
        service_account: bool = False,
    ) -> Optional[AuditLog]:
        """
        Create an audit log entry under the event contract
        (docs/design/audit-event-contract.md). Always logs regardless of
        license - viewing is gated at API level.

        Source context (ip, user agent, request id, session id) comes from the
        request when given, else from the ambient audit context bound by the
        HTTP middleware or a job scope. Details go through the content policy
        before they reach the row. ``outcome`` defaults to the contract's
        inference for legacy call sites; new producers state it.

        ``commit=False`` ties the row to the caller's transaction: a rolled
        back business change takes its audit row with it, so a committed
        success is never recorded for work that did not happen. With
        ``commit=True`` a failed write falls back to the durable queue/spill
        (app.ee.audit.tool_audit) instead of being lost.
        """
        ctx = audit_context.current()
        if request is not None:
            req_ctx = audit_context.from_request(request)
            # Keep the request id of the ambient context (the middleware
            # minted it and echoed it to the client); fill the rest from the
            # request itself.
            req_ctx = req_ctx.with_(request_id=ctx.request_id if ctx and ctx.request_id else req_ctx.request_id)
            ctx = req_ctx
        ip_address = ctx.ip_address if ctx else None
        user_agent = ctx.user_agent if ctx else None

        details = sanitize_details(details)
        user_id = str(user_id) if user_id else None
        snapshot = actor_snapshot
        if snapshot is None and (actor is not None or user_id):
            snapshot = await self._actor_snapshot(db, actor, user_id)
        a_type = actor_type_for(user_id, details, service=service_account)
        if not user_id and ctx is not None and ctx.source == "http" and action.startswith("auth."):
            a_type = ACTOR_UNKNOWN

        audit_log = AuditLog(
            organization_id=str(organization_id) if organization_id else None,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
            occurred_at=occurred_at or datetime.utcnow(),
            outcome=normalize_outcome(outcome, action),
            failure_reason=(failure_reason or None) and str(failure_reason)[:255],
            request_id=ctx.request_id if ctx else None,
            session_id=ctx.session_id if ctx else None,
            actor_type=a_type,
            actor_snapshot=snapshot,
            schema_version=SCHEMA_VERSION,
        )

        db.add(audit_log)
        if commit:
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                from app.ee.audit import tool_audit
                tool_audit.note_recording_failure()
                logger.warning("Audit write failed for %s; handing the event to the durable queue", action, exc_info=True)
                await tool_audit.enqueue_row(audit_log)
                return audit_log

        logger.debug(f"Audit log created: {action} by user {user_id} in org {organization_id}")
        return audit_log

    @staticmethod
    async def _actor_snapshot(db: AsyncSession, actor: Optional[User], user_id: Optional[str]) -> Optional[dict]:
        """Identity at event time, so later renames or deletion of the user
        row never rewrite what the evidence says."""
        try:
            if actor is None and user_id:
                row = (await db.execute(select(User.email, User.name).where(User.id == user_id))).first()
                if row is None:
                    return None
                email, name = row
            elif actor is not None:
                email, name = getattr(actor, "email", None), getattr(actor, "name", None)
            else:
                return None
            snap = {"email": email, "name": name}
            return {k: v for k, v in snap.items() if v}
        except Exception:
            logger.debug("actor snapshot failed", exc_info=True)
            return None

    @staticmethod
    def build_conditions(organization_id: str, filters: Optional[AuditLogFilters]) -> list:
        """WHERE clauses shared by the list, export and count paths.

        ``search`` matches the action, resource type, the actor's email and
        ``details.title``; callers using it must outer-join ``User``.
        """
        conditions = [AuditLog.organization_id == organization_id]
        if not filters:
            return conditions
        if filters.action:
            # Support comma-separated action values for multiselect
            actions = [a.strip() for a in filters.action.split(',') if a.strip()]
            if len(actions) == 1:
                conditions.append(AuditLog.action == actions[0])
            elif len(actions) > 1:
                conditions.append(AuditLog.action.in_(actions))
        if filters.resource_type:
            types = [t.strip() for t in filters.resource_type.split(',') if t.strip()]
            if len(types) == 1:
                conditions.append(AuditLog.resource_type == types[0])
            elif len(types) > 1:
                conditions.append(AuditLog.resource_type.in_(types))
        if filters.resource_id:
            conditions.append(AuditLog.resource_id == filters.resource_id)
        if filters.outcome:
            conditions.append(AuditLog.outcome == filters.outcome)
        if filters.request_id:
            conditions.append(AuditLog.request_id == filters.request_id)
        if filters.user_id:
            conditions.append(AuditLog.user_id == filters.user_id)
        if filters.start_date:
            conditions.append(AuditLog.created_at >= _naive_utc(filters.start_date))
        if filters.end_date:
            conditions.append(AuditLog.created_at <= _naive_utc(filters.end_date))
        if filters.search:
            search_term = f"%{filters.search}%"
            conditions.append(
                or_(
                    AuditLog.action.ilike(search_term),
                    AuditLog.resource_type.ilike(search_term),
                    User.email.ilike(search_term),
                    AuditLog.details["title"].as_string().ilike(search_term),
                )
            )
        return conditions

    async def get_logs(
        self,
        db: AsyncSession,
        organization_id: str,
        filters: Optional[AuditLogFilters] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[AuditLogResponse], int]:
        """
        Get paginated audit logs for an organization.

        Returns:
            Tuple of (list of audit logs, total count)
        """
        conditions = self.build_conditions(organization_id, filters)
        needs_user_join = bool(filters and filters.search)

        # Count total
        count_stmt = select(func.count(AuditLog.id))
        if needs_user_join:
            count_stmt = count_stmt.outerjoin(User, User.id == AuditLog.user_id)
        count_stmt = count_stmt.where(and_(*conditions))
        total_result = await db.execute(count_stmt)
        total = total_result.scalar() or 0

        # Get paginated results
        offset = (page - 1) * page_size
        stmt = select(AuditLog).options(selectinload(AuditLog.user))
        if needs_user_join:
            stmt = stmt.outerjoin(User, User.id == AuditLog.user_id)
        stmt = (
            stmt
            .where(and_(*conditions))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset(offset)
            .limit(page_size)
        )

        result = await db.execute(stmt)
        logs = result.scalars().all()

        # Convert to response schema with user email
        responses = []
        for log in logs:
            response = AuditLogResponse(
                id=log.id,
                organization_id=log.organization_id,
                user_id=log.user_id,
                user_email=log.user.email if log.user else None,
                action=log.action,
                resource_type=log.resource_type,
                resource_id=log.resource_id,
                details=log.details,
                ip_address=log.ip_address,
                user_agent=log.user_agent,
                created_at=log.created_at,
                occurred_at=log.occurred_at,
                outcome=log.outcome,
                failure_reason=log.failure_reason,
                request_id=log.request_id,
                session_id=log.session_id,
                actor_type=log.actor_type,
                actor_snapshot=log.actor_snapshot,
                schema_version=log.schema_version,
            )
            responses.append(response)

        return responses, total

    async def get_log_by_id(
        self,
        db: AsyncSession,
        organization_id: str,
        log_id: str,
    ) -> Optional[AuditLogResponse]:
        """Get a single audit log entry by ID"""
        stmt = (
            select(AuditLog)
            .options(selectinload(AuditLog.user))
            .where(
                AuditLog.id == log_id,
                AuditLog.organization_id == organization_id
            )
        )

        result = await db.execute(stmt)
        log = result.scalar_one_or_none()

        if not log:
            return None

        return AuditLogResponse(
            id=log.id,
            organization_id=log.organization_id,
            user_id=log.user_id,
            user_email=log.user.email if log.user else None,
            action=log.action,
            resource_type=log.resource_type,
            resource_id=log.resource_id,
            details=log.details,
            ip_address=log.ip_address,
            user_agent=log.user_agent,
            created_at=log.created_at,
            occurred_at=log.occurred_at,
            outcome=log.outcome,
            failure_reason=log.failure_reason,
            request_id=log.request_id,
            session_id=log.session_id,
            actor_type=log.actor_type,
            actor_snapshot=log.actor_snapshot,
            schema_version=log.schema_version,
        )

    async def get_action_types(
        self,
        db: AsyncSession,
        organization_id: str,
    ) -> List[str]:
        """
        Get list of distinct action types for an organization.
        
        Returns:
            List of unique action values used in the organization's audit logs
        """
        stmt = (
            select(AuditLog.action)
            .where(AuditLog.organization_id == organization_id)
            .distinct()
            .order_by(AuditLog.action)
        )
        
        result = await db.execute(stmt)
        actions = result.scalars().all()
        
        return list(actions)


    async def count_logs(self, db: AsyncSession, organization_id: str, filters: Optional[AuditLogFilters]) -> int:
        stmt = select(func.count(AuditLog.id))
        if filters and filters.search:
            stmt = stmt.outerjoin(User, User.id == AuditLog.user_id)
        stmt = stmt.where(and_(*self.build_conditions(organization_id, filters)))
        return (await db.execute(stmt)).scalar() or 0

    async def export_snapshot(
        self, db: AsyncSession, organization_id: str, filters: Optional[AuditLogFilters]
    ) -> Tuple[int, Optional[Tuple[datetime, str]]]:
        """Row count and the last (created_at, id) key matching the filters,
        taken before an export starts so it can stop at that point."""
        total = await self.count_logs(db, organization_id, filters)
        if not total:
            return 0, None
        stmt = select(AuditLog.created_at, AuditLog.id)
        if filters and filters.search:
            stmt = stmt.outerjoin(User, User.id == AuditLog.user_id)
        stmt = (
            stmt.where(and_(*self.build_conditions(organization_id, filters)))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
        last = (await db.execute(stmt)).first()
        return total, ((last[0], last[1]) if last else None)

    async def iter_logs_ascending(
        self,
        db: AsyncSession,
        organization_id: str,
        filters: Optional[AuditLogFilters],
        page_size: int = 1000,
        upper: Optional[Tuple[datetime, str]] = None,
        limit: Optional[int] = None,
    ):
        """Yield (AuditLog, user_email) pages oldest-first by keyset, never
        loading the whole result into memory. ``upper`` (inclusive) and
        ``limit`` bound the scan to a snapshot taken by export_snapshot, so
        rows written while the export streams are not included."""
        if limit is not None and limit <= 0:
            return
        cursor = (None, None)
        emitted = 0
        base = self.build_conditions(organization_id, filters)
        if upper is not None:
            base.append(or_(
                AuditLog.created_at < upper[0],
                and_(AuditLog.created_at == upper[0], AuditLog.id <= upper[1]),
            ))
        while True:
            stmt = select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id).where(and_(*base))
            if cursor[0] is not None:
                stmt = stmt.where(or_(
                    AuditLog.created_at > cursor[0],
                    and_(AuditLog.created_at == cursor[0], AuditLog.id > cursor[1]),
                ))
            size = page_size if limit is None else min(page_size, limit - emitted)
            stmt = stmt.order_by(AuditLog.created_at, AuditLog.id).limit(size)
            rows = (await db.execute(stmt)).all()
            if not rows:
                return
            yield rows
            emitted += len(rows)
            last = rows[-1][0]
            cursor = (last.created_at, last.id)
            if len(rows) < size or (limit is not None and emitted >= limit):
                return

    async def get_resource_types(self, db: AsyncSession, organization_id: str) -> List[str]:
        """Distinct non-null resource types used in the organization's audit logs."""
        stmt = (
            select(AuditLog.resource_type)
            .where(AuditLog.organization_id == organization_id, AuditLog.resource_type.isnot(None))
            .distinct()
            .order_by(AuditLog.resource_type)
        )
        return list((await db.execute(stmt)).scalars().all())


# Singleton instance
audit_service = AuditService()
