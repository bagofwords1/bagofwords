from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.organization import Organization
from app.models.user import User
from app.models.completion import Completion
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget
from app.models.completion_feedback import CompletionFeedback
from app.schemas.console_schema import (
    SimpleMetrics, MetricsQueryParams, MetricsComparison, 
    TimeSeriesMetrics, ActivityMetrics, PerformanceMetrics,
    TimeSeriesPoint, TimeSeriesPointFloat, DateRange,
    TableUsageData, TableUsageMetrics, TableJoinsHeatmap, TableJoinData,
    TopUserData, TopUsersMetrics, RecentNegativeFeedbackData, RecentNegativeFeedbackMetrics
)
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from app.settings.logging_config import get_logger
from collections import Counter, defaultdict
import json
import re
from pydantic import BaseModel
from app.models.membership import Membership

logger = get_logger(__name__)

class ConsoleService:
    
    def _normalize_date_range(self, start_date: Optional[datetime], end_date: Optional[datetime]) -> tuple[datetime, datetime]:
        """Normalize date range to ensure end_date includes the full day"""
        
        # Default to last 30 days if no dates provided
        if not end_date:
            end_date = datetime.now()
        if not start_date:
            start_date = end_date - timedelta(days=30)
            
        # Ensure end_date includes the full day (set to end of day)
        end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Ensure start_date starts from beginning of day  
        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        
        return start_date, end_date
    
    async def get_organization_metrics(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> SimpleMetrics:
        """Get organization metrics with optional date filtering"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        # Base filters
        report_filter = Report.organization_id == organization.id
        
        # Count total messages (completions)
        messages_query = select(func.count(Completion.id)).join(Report).where(
            report_filter,
            Completion.created_at >= start_date,
            Completion.created_at <= end_date
        )
        
        messages_result = await db.execute(messages_query)
        total_messages = messages_result.scalar() or 0
        
        # Count total queries (steps)
        queries_query = (
            select(func.count(Step.id))
            .join(Widget).join(Report)
            .where(
                report_filter,
                Step.created_at >= start_date,
                Step.created_at <= end_date
            )
        )
        
        queries_result = await db.execute(queries_query)
        total_queries = queries_result.scalar() or 0
        
        # Count total feedbacks
        feedbacks_query = (
            select(func.count(CompletionFeedback.id))
            .join(Completion, CompletionFeedback.completion_id == Completion.id)
            .join(Report, Completion.report_id == Report.id)
            .where(
                report_filter,
                CompletionFeedback.created_at >= start_date,
                CompletionFeedback.created_at <= end_date
            )
        )
        
        feedbacks_result = await db.execute(feedbacks_query)
        total_feedbacks = feedbacks_result.scalar() or 0
        
        # Count active users
        users_query = select(func.count(func.distinct(Report.user_id))).where(
            report_filter,
            Report.created_at >= start_date,
            Report.created_at <= end_date
        )
        
        users_result = await db.execute(users_query)
        active_users = users_result.scalar() or 0
        
        return SimpleMetrics(
            total_messages=total_messages,
            total_queries=total_queries,
            total_feedbacks=total_feedbacks,
            active_users=active_users,
            accuracy="90%",  # Placeholder
            instructions_efficiency="90%",  # Placeholder
            feedback_efficiency="90%"  # Placeholder
        )

    async def get_metrics_with_comparison(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> MetricsComparison:
        """Get metrics with previous period comparison"""
        
        # Default to last 30 days if no dates provided
        end_date = params.end_date or datetime.now()
        start_date = params.start_date or (end_date - timedelta(days=30))
        
        # Calculate period length and previous period dates
        period_length = end_date - start_date
        prev_end_date = start_date
        prev_start_date = start_date - period_length
        
        # Get current and previous period metrics
        current_params = MetricsQueryParams(start_date=start_date, end_date=end_date)
        prev_params = MetricsQueryParams(start_date=prev_start_date, end_date=prev_end_date)
        
        current_metrics = await self.get_organization_metrics(db, organization, current_params)
        previous_metrics = await self.get_organization_metrics(db, organization, prev_params)
        
        # Calculate changes
        changes = self._calculate_changes(current_metrics, previous_metrics)
        
        return MetricsComparison(
            current=current_metrics,
            previous=previous_metrics,
            changes=changes,
            period_days=period_length.days
        )

    def _calculate_changes(self, current: SimpleMetrics, previous: SimpleMetrics) -> Dict[str, Dict[str, float]]:
        """Calculate percentage and absolute changes between periods"""
        
        changes = {}
        numeric_fields = ["total_messages", "total_queries", "total_feedbacks", "active_users"]
        
        for field in numeric_fields:
            current_val = getattr(current, field)
            previous_val = getattr(previous, field)
            
            absolute_change = current_val - previous_val
            percentage_change = (absolute_change / previous_val * 100) if previous_val > 0 else 0
            
            changes[field] = {
                "absolute": absolute_change,
                "percentage": round(percentage_change, 1)
            }
        
        return changes

    async def get_recent_widgets(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        current_user: User, 
        offset: int = 0, 
        limit: int = 10
    ) -> Dict:
        """Get recent widgets - keeping existing implementation for now"""
        # Your existing implementation here
        pass

    async def get_timeseries_metrics(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> TimeSeriesMetrics:
        """Get time-series metrics data for charts"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        # Generate daily intervals
        intervals = []
        current = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        while current <= end_date:
            next_day = current + timedelta(days=1)
            intervals.append((current, next_day))
            current = next_day
        
        # Get data for each day
        messages_data = []
        queries_data = []
        accuracy_data = []
        efficiency_data = []
        feedback_data = []
        
        for interval_start, interval_end in intervals:
            # Messages count for this day
            messages_result = await db.execute(
                select(func.count(Completion.id))
                .join(Report)
                .where(
                    Report.organization_id == organization.id,
                    Completion.created_at >= interval_start,
                    Completion.created_at < interval_end
                )
            )
            messages_count = messages_result.scalar() or 0
            
            # Queries count for this day
            queries_result = await db.execute(
                select(func.count(Step.id))
                .join(Widget).join(Report)
                .where(
                    Report.organization_id == organization.id,
                    Step.created_at >= interval_start,
                    Step.created_at < interval_end
                )
            )
            queries_count = queries_result.scalar() or 0
            
            # Positive feedback rate for this day
            total_feedbacks_result = await db.execute(
                select(func.count(CompletionFeedback.id))
                .join(Completion, CompletionFeedback.completion_id == Completion.id)
                .join(Report, Completion.report_id == Report.id)
                .where(
                    Report.organization_id == organization.id,
                    CompletionFeedback.created_at >= interval_start,
                    CompletionFeedback.created_at < interval_end
                )
            )
            total_feedbacks = total_feedbacks_result.scalar() or 0
            
            positive_feedbacks_result = await db.execute(
                select(func.count(CompletionFeedback.id))
                .join(Completion, CompletionFeedback.completion_id == Completion.id)
                .join(Report, Completion.report_id == Report.id)
                .where(
                    Report.organization_id == organization.id,
                    CompletionFeedback.created_at >= interval_start,
                    CompletionFeedback.created_at < interval_end,
                    CompletionFeedback.direction > 0
                )
            )
            positive_feedbacks = positive_feedbacks_result.scalar() or 0
            
            # Calculate positive feedback rate
            positive_rate = (positive_feedbacks / total_feedbacks * 100) if total_feedbacks > 0 else 0
            
            date_str = interval_start.strftime('%Y-%m-%d')
            
            # Create TimeSeriesPoint objects
            messages_data.append(TimeSeriesPoint(date=date_str, value=messages_count))
            queries_data.append(TimeSeriesPoint(date=date_str, value=queries_count))
            
            # Create TimeSeriesPointFloat objects for percentages
            accuracy_data.append(TimeSeriesPointFloat(date=date_str, value=90.0))  # Placeholder
            efficiency_data.append(TimeSeriesPointFloat(date=date_str, value=85.0))  # Placeholder
            feedback_data.append(TimeSeriesPointFloat(date=date_str, value=positive_rate))
        
        return TimeSeriesMetrics(
            date_range=DateRange(
                start=start_date.isoformat(),
                end=end_date.isoformat()
            ),
            activity_metrics=ActivityMetrics(
                messages=messages_data,
                queries=queries_data
            ),
            performance_metrics=PerformanceMetrics(
                accuracy=accuracy_data,
                instructions_efficiency=efficiency_data,
                positive_feedback_rate=feedback_data
            )
        )

    async def get_table_usage_metrics(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> TableUsageMetrics:
        """Get table usage statistics from step data models"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        # Get all steps within date range for this organization
        steps_query = (
            select(Step)
            .join(Widget).join(Report)
            .where(
                Report.organization_id == organization.id,
                Step.created_at >= start_date,
                Step.created_at <= end_date,
                Step.data_model.isnot(None)  # Only steps with data models
            )
        )
        
        result = await db.execute(steps_query)
        steps = result.scalars().all()
        
        # Extract table usage from data models
        table_usage = Counter()
        total_queries = 0
        
        for step in steps:
            if not step.data_model:
                continue
                
            try:
                data_model = step.data_model if isinstance(step.data_model, dict) else json.loads(step.data_model)
                tables_in_query = self._extract_tables_from_data_model(data_model)
                
                if tables_in_query:
                    total_queries += 1
                    for table in tables_in_query:
                        table_usage[table] += 1
                        
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Failed to parse data_model for step {step.id}: {e}")
                continue
        
        # Get top 10 tables
        top_tables = [
            TableUsageData(
                table_name=table,
                usage_count=count,
                database_name=self._extract_database_name(table)
            )
            for table, count in table_usage.most_common(10)
        ]
        
        return TableUsageMetrics(
            top_tables=top_tables,
            total_queries_analyzed=total_queries,
            date_range=DateRange(
                start=start_date.isoformat(),
                end=end_date.isoformat()
            )
        )

    async def get_table_joins_heatmap(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> TableJoinsHeatmap:
        """Get table joins heatmap showing which tables are used together"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        # Get all steps within date range for this organization
        steps_query = (
            select(Step)
            .join(Widget).join(Report)
            .where(
                Report.organization_id == organization.id,
                Step.created_at >= start_date,
                Step.created_at <= end_date,
                Step.data_model.isnot(None)
            )
        )
        
        result = await db.execute(steps_query)
        steps = result.scalars().all()
        
        # Track table co-occurrence
        table_pairs = Counter()
        all_tables = set()
        total_queries = 0
        
        for step in steps:
            if not step.data_model:
                continue
                
            try:
                data_model = step.data_model if isinstance(step.data_model, dict) else json.loads(step.data_model)
                tables_in_query = self._extract_tables_from_data_model(data_model)
                
                if len(tables_in_query) > 1:  # Only count queries with multiple tables
                    total_queries += 1
                    tables_list = list(tables_in_query)
                    all_tables.update(tables_list)
                    
                    # Count all pairs of tables in this query
                    for i, table1 in enumerate(tables_list):
                        for table2 in tables_list[i+1:]:
                            # Sort to ensure consistent pair ordering
                            pair = tuple(sorted([table1, table2]))
                            table_pairs[pair] += 1
                            
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Failed to parse data_model for step {step.id}: {e}")
                continue
        
        # Convert to list of TableJoinData
        join_data = [
            TableJoinData(
                table1=pair[0],
                table2=pair[1], 
                join_count=count
            )
            for pair, count in table_pairs.most_common(50)  # Top 50 pairs
        ]
        
        return TableJoinsHeatmap(
            table_pairs=join_data,
            unique_tables=sorted(list(all_tables)),
            total_queries_analyzed=total_queries,
            date_range=DateRange(
                start=start_date.isoformat(),
                end=end_date.isoformat()
            )
        )

    async def get_top_users_metrics(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> TopUsersMetrics:
        """Get top users by activity"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        # Get current period user metrics (simplified without trend calculation)
        current_users = await self._get_user_metrics_for_period(db, organization, start_date, end_date)
        
        top_users_data = []
        for user in current_users[:10]:  # Top 10 users
            top_users_data.append(TopUserData(
                user_id=user['user_id'],
                name=user['name'],
                email=user['email'],
                role=user['role'],
                messages_count=user['messages_count'],
                queries_count=user['queries_count']
                # Remove trend_percentage
            ))
        
        return TopUsersMetrics(
            top_users=top_users_data,
            total_users_analyzed=len(current_users),
            date_range=DateRange(
                start=start_date.isoformat(),
                end=end_date.isoformat()
            )
        )

    async def _get_user_metrics_for_period(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        start_date: datetime, 
        end_date: datetime
    ) -> List[Dict]:
        """Get user metrics for a specific period"""
        
        # Get user activity with messages and queries count, and role from membership
        result = await db.execute(
            select(
                User.id.label('user_id'),
                User.name,
                User.email,
                Membership.role.label('role'),  # Fix: Use Membership.role instead of User.type
                func.count(func.distinct(Completion.id)).label('messages_count'),
                func.count(func.distinct(Step.id)).label('queries_count')
            )
            .select_from(User)
            .join(Membership, User.id == Membership.user_id)  # Join with Membership to get role
            .join(Report, User.id == Report.user_id)
            .outerjoin(Completion, Report.id == Completion.report_id)
            .outerjoin(Widget, Report.id == Widget.report_id)
            .outerjoin(Step, Widget.id == Step.widget_id)
            .where(
                Membership.organization_id == organization.id,  # Filter by organization through membership
                Report.organization_id == organization.id,
                Report.created_at >= start_date,
                Report.created_at <= end_date
            )
            .group_by(User.id, User.name, User.email, Membership.role)
            .order_by(func.count(func.distinct(Completion.id)).desc())
        )
        
        users = result.all()
        return [
            {
                'user_id': user.user_id,
                'name': user.name,
                'email': user.email,
                'role': user.role,
                'messages_count': user.messages_count or 0,
                'queries_count': user.queries_count or 0
            }
            for user in users
        ]

    async def get_recent_negative_feedback_metrics(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> RecentNegativeFeedbackMetrics:
        """Get recent negative feedback with completion context"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        # Simplified query - just join with User
        result = await db.execute(
            select(
                CompletionFeedback.id,
                CompletionFeedback.message.label('description'),
                CompletionFeedback.created_at,
                CompletionFeedback.completion_id,
                func.coalesce(User.name, 'System').label('user_name'),
                func.coalesce(User.id, 'system').label('user_id')
            )
            .select_from(CompletionFeedback)
            .outerjoin(User, CompletionFeedback.user_id == User.id)  # Only join with User
            .where(
                CompletionFeedback.organization_id == organization.id,
                CompletionFeedback.direction == -1,  # Negative feedback only
                CompletionFeedback.created_at >= start_date,
                CompletionFeedback.created_at <= end_date,
                CompletionFeedback.message.isnot(None)  # Only feedbacks with messages
            )
            .order_by(CompletionFeedback.created_at.desc())
            .limit(20)  # Latest 20 negative feedbacks
        )
        
        feedbacks = result.all()
        
        print(f"Found {len(feedbacks)} negative feedbacks")
        print(f"Date range: {start_date} to {end_date}")
        print(f"Organization ID: {organization.id}")
        
        # Get total count for the period
        total_count_result = await db.execute(
            select(func.count(CompletionFeedback.id))
            .where(
                CompletionFeedback.organization_id == organization.id,
                CompletionFeedback.direction == -1,
                CompletionFeedback.created_at >= start_date
            )
        )
        total_negative_feedbacks = total_count_result.scalar() or 0
        
        feedback_data = [
            RecentNegativeFeedbackData(
                id=feedback.id,
                description=feedback.description or "No message provided",
                user_name=feedback.user_name,
                user_id=feedback.user_id,
                completion_id=feedback.completion_id,
                prompt=None,  # We don't have prompt anymore, set to None
                created_at=feedback.created_at,
                trace=f"/reports/{feedback.completion_id}"
            )
            for feedback in feedbacks
        ]
        
        return RecentNegativeFeedbackMetrics(
            recent_feedbacks=feedback_data,
            total_negative_feedbacks=total_negative_feedbacks,
            date_range=DateRange(
                start=start_date.isoformat(),
                end=end_date.isoformat()
            )
        )

    def _extract_tables_from_data_model(self, data_model: dict) -> set:
        """Extract unique table names from a data model"""
        tables = set()
        
        # Extract from columns
        columns = data_model.get('columns', [])
        for column in columns:
            source = column.get('source', '')
            table = self._parse_table_from_source(source)
            if table:
                tables.add(table)
        
        return tables

    def _parse_table_from_source(self, source: str) -> Optional[str]:
        """Parse table name from source string like 'dvdrental.customer.first_name' or 'customer.first_name'"""
        if not source:
            return None
            
        # Handle function calls like 'SUM(dvdrental.payment.amount)'
        # Extract table reference from within functions
        if '(' in source and ')' in source:
            # Extract content inside parentheses
            match = re.search(r'\((.*?)\)', source)
            if match:
                source = match.group(1)
        
        # Split by dots and extract table part
        parts = source.split('.')
        
        if len(parts) >= 3:  # database.table.column
            return f"{parts[0]}.{parts[1]}"
        elif len(parts) == 2:  # table.column  
            return parts[0]
        else:
            return None

    def _extract_database_name(self, table_name: str) -> Optional[str]:
        """Extract database name from table name like 'dvdrental.customer'"""
        if '.' in table_name:
            return table_name.split('.')[0]
        return None