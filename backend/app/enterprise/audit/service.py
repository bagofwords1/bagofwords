# Audit Log Service
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

import logging
from typing import Optional, List, Tuple
from datetime import datetime
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from fastapi import Request

from app.enterprise.audit.models import AuditLog
from app.enterprise.audit.schemas import AuditLogResponse, AuditLogFilters
from app.models.user import User

logger = logging.getLogger(__name__)


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
        await db.commit()

        logger.debug(f"Audit log created: {action} by user {user_id} in org {organization_id}")
        return audit_log

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
        # Build base query
        conditions = [AuditLog.organization_id == organization_id]

        if filters:
            if filters.action:
                # Support comma-separated action values for multiselect
                actions = [a.strip() for a in filters.action.split(',') if a.strip()]
                if len(actions) == 1:
                    conditions.append(AuditLog.action == actions[0])
                elif len(actions) > 1:
                    conditions.append(AuditLog.action.in_(actions))
            if filters.resource_type:
                conditions.append(AuditLog.resource_type == filters.resource_type)
            if filters.resource_id:
                conditions.append(AuditLog.resource_id == filters.resource_id)
            if filters.user_id:
                conditions.append(AuditLog.user_id == filters.user_id)
            if filters.start_date:
                conditions.append(AuditLog.created_at >= filters.start_date)
            if filters.end_date:
                conditions.append(AuditLog.created_at <= filters.end_date)
            if filters.search:
                search_term = f"%{filters.search}%"
                conditions.append(
                    or_(
                        AuditLog.action.ilike(search_term),
                        AuditLog.resource_type.ilike(search_term),
                    )
                )

        # Count total
        count_stmt = select(func.count(AuditLog.id)).where(and_(*conditions))
        total_result = await db.execute(count_stmt)
        total = total_result.scalar() or 0

        # Get paginated results
        offset = (page - 1) * page_size
        stmt = (
            select(AuditLog)
            .options(selectinload(AuditLog.user))
            .where(and_(*conditions))
            .order_by(AuditLog.created_at.desc())
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


# Singleton instance
audit_service = AuditService()
