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
        organization_id: str,
        action: str,
        user_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[dict] = None,
        request: Optional[Request] = None,
        commit: bool = True,
    ) -> Optional[AuditLog]:
        """
        Create an audit log entry.
        Always logs regardless of license - viewing is gated at API level.

        Args:
            db: Database session
            organization_id: Organization ID
            action: Action name (e.g., "report.created")
            user_id: User who performed the action
            resource_type: Type of resource affected
            resource_id: ID of resource affected
            details: Additional context as dict
            request: FastAPI request for IP/user agent extraction
        """
        ip_address = None
        user_agent = None

        if request:
            # Get client IP (handle proxies)
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                ip_address = forwarded.split(",")[0].strip()
            else:
                ip_address = request.client.host if request.client else None

            user_agent = request.headers.get("User-Agent", "")[:500]

        audit_log = AuditLog(
            organization_id=organization_id,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        db.add(audit_log)
        if commit:
            await db.commit()

        logger.debug(f"Audit log created: {action} by user {user_id} in org {organization_id}")
        return audit_log

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

    async def iter_logs_ascending(
        self,
        db: AsyncSession,
        organization_id: str,
        filters: Optional[AuditLogFilters],
        page_size: int = 1000,
    ):
        """Yield (AuditLog, user_email) pages oldest-first by keyset, never
        loading the whole result into memory."""
        cursor = (None, None)
        base = self.build_conditions(organization_id, filters)
        while True:
            stmt = select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id).where(and_(*base))
            if cursor[0] is not None:
                stmt = stmt.where(or_(
                    AuditLog.created_at > cursor[0],
                    and_(AuditLog.created_at == cursor[0], AuditLog.id > cursor[1]),
                ))
            stmt = stmt.order_by(AuditLog.created_at, AuditLog.id).limit(page_size)
            rows = (await db.execute(stmt)).all()
            if not rows:
                return
            yield rows
            last = rows[-1][0]
            cursor = (last.created_at, last.id)
            if len(rows) < page_size:
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
