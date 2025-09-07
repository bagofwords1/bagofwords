"""
Database query optimization utilities
"""
from typing import List, Optional, Dict, Any
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload
from app.models.completion import Completion
from app.models.report import Report
from app.models.widget import Widget
from app.models.step import Step
from app.models.user import User
from app.models.organization import Organization
from app.services.cache_service import CacheService
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

class QueryOptimizer:
    """Service for optimizing database queries"""
    
    @staticmethod
    async def get_completions_with_relations(
        db: AsyncSession,
        report_id: str,
        limit: int = 10,
        before: Optional[str] = None,
        include_blocks: bool = True
    ) -> List[Completion]:
        """
        Optimized query to get completions with their relations
        Uses eager loading to avoid N+1 queries
        """
        # Build base query with eager loading
        query = select(Completion)
        
        if include_blocks:
            query = query.options(
                selectinload(Completion.completion_blocks),
                selectinload(Completion.agent_execution)
            )
        
        query = query.where(Completion.report_id == report_id)
        
        if before:
            try:
                from datetime import datetime
                before_dt = datetime.fromisoformat(before)
                query = query.where(Completion.created_at < before_dt)
            except Exception:
                pass
        
        query = query.order_by(Completion.created_at.desc()).limit(limit)
        
        result = await db.execute(query)
        completions = result.scalars().all()
        
        return list(reversed(completions))  # Return in chronological order
    
    @staticmethod
    async def get_reports_with_stats(
        db: AsyncSession,
        organization_id: str,
        user_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Get reports with completion and widget counts in a single query
        """
        # Cache key for organization reports
        cache_key = f"reports_stats:{organization_id}:{user_id}:{limit}:{offset}"
        cached_result = await CacheService.get_organization_settings(cache_key)
        if cached_result:
            return cached_result
        
        # Build query with subqueries for counts
        completion_count_subq = (
            select(func.count(Completion.id))
            .where(Completion.report_id == Report.id)
            .scalar_subquery()
        )
        
        widget_count_subq = (
            select(func.count(Widget.id))
            .where(Widget.report_id == Report.id)
            .scalar_subquery()
        )
        
        query = select(
            Report,
            completion_count_subq.label('completion_count'),
            widget_count_subq.label('widget_count')
        ).where(Report.organization_id == organization_id)
        
        if user_id:
            query = query.where(Report.user_id == user_id)
        
        query = query.order_by(Report.updated_at.desc()).limit(limit).offset(offset)
        
        result = await db.execute(query)
        rows = result.all()
        
        reports_with_stats = []
        for row in rows:
            report_dict = {
                'id': row.Report.id,
                'title': row.Report.title,
                'description': row.Report.description,
                'created_at': row.Report.created_at,
                'updated_at': row.Report.updated_at,
                'user_id': row.Report.user_id,
                'organization_id': row.Report.organization_id,
                'completion_count': row.completion_count,
                'widget_count': row.widget_count
            }
            reports_with_stats.append(report_dict)
        
        # Cache for 5 minutes
        await CacheService.set_organization_settings(cache_key, reports_with_stats, 300)
        
        return reports_with_stats
    
    @staticmethod
    async def get_user_permissions_optimized(
        db: AsyncSession,
        user_id: str,
        organization_id: str
    ) -> Dict[str, Any]:
        """
        Get user permissions with caching
        """
        # Check cache first
        cached_permissions = await CacheService.get_user_permissions(user_id, organization_id)
        if cached_permissions:
            return cached_permissions
        
        # Query with joins to get all needed data in one query
        from app.models.membership import Membership
        
        query = select(User, Membership).join(
            Membership, User.id == Membership.user_id
        ).where(
            and_(
                User.id == user_id,
                Membership.organization_id == organization_id
            )
        )
        
        result = await db.execute(query)
        row = result.first()
        
        if not row:
            return {}
        
        user, membership = row
        
        permissions = {
            'user_id': user.id,
            'organization_id': organization_id,
            'role': membership.role,
            'is_active': user.is_active,
            'is_verified': user.is_verified,
            'is_superuser': user.is_superuser,
            'permissions': membership.get_permissions() if hasattr(membership, 'get_permissions') else []
        }
        
        # Cache for 15 minutes
        await CacheService.set_user_permissions(user_id, organization_id, permissions, 900)
        
        return permissions
    
    @staticmethod
    async def bulk_update_completion_status(
        db: AsyncSession,
        completion_ids: List[str],
        status: str
    ) -> int:
        """
        Bulk update completion status for better performance
        """
        from sqlalchemy import update
        
        stmt = update(Completion).where(
            Completion.id.in_(completion_ids)
        ).values(status=status)
        
        result = await db.execute(stmt)
        await db.commit()
        
        return result.rowcount
    
    @staticmethod
    async def cleanup_old_completions(
        db: AsyncSession,
        days_old: int = 90,
        batch_size: int = 1000
    ) -> int:
        """
        Clean up old completions in batches
        """
        from datetime import datetime, timedelta
        
        cutoff_date = datetime.utcnow() - timedelta(days=days_old)
        
        # Get IDs of old completions in batches
        query = select(Completion.id).where(
            Completion.created_at < cutoff_date
        ).limit(batch_size)
        
        result = await db.execute(query)
        old_completion_ids = [row[0] for row in result.all()]
        
        if not old_completion_ids:
            return 0
        
        # Delete in batch
        from sqlalchemy import delete
        stmt = delete(Completion).where(Completion.id.in_(old_completion_ids))
        result = await db.execute(stmt)
        await db.commit()
        
        logger.info(f"Cleaned up {result.rowcount} old completions")
        return result.rowcount