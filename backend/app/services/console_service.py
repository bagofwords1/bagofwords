from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text, literal
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
    TopUserData, TopUsersMetrics, RecentNegativeFeedbackData, RecentNegativeFeedbackMetrics,
    DiagnosisItemData, DiagnosisStepData, DiagnosisFeedbackData, DiagnosisMetrics,
    TraceData, TraceCompletionData, TraceStepData, TraceFeedbackData
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

    async def get_diagnosis_metrics(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams,
        page: int = 1,
        page_size: int = 50
    ) -> DiagnosisMetrics:
        """Get completions with failed steps or negative feedback for diagnosis"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        print(f"Debug: Fetching diagnosis data for org {organization.id} from {start_date} to {end_date}")
        
        # Separate query for failed steps
        failed_steps_query = (
            select(
                Completion.id.label('completion_id'),
                Completion.completion.label('completion_content'),
                Completion.created_at.label('completion_created_at'),
                Report.user_id,
                Report.id.label('report_id'),
                func.coalesce(User.name, 'Unknown User').label('user_name'),
                func.coalesce(User.email, '').label('user_email'),
                Step.id.label('step_id'),
                Step.title.label('step_title'),
                Step.status.label('step_status'),
                Step.code.label('step_code'),
                Step.data_model.label('step_data_model'),
                Step.created_at.label('step_created_at')
            )
            .select_from(Completion)
            .join(Report, Completion.report_id == Report.id)
            .outerjoin(User, Report.user_id == User.id)
            .join(Step, Completion.step_id == Step.id)
            .where(
                Report.organization_id == organization.id,
                Step.status == 'error',
                Completion.created_at >= start_date,
                Completion.created_at <= end_date
            )
            .order_by(Completion.created_at.desc())
        )
        
        # Separate query for negative feedback - Remove created_at filter temporarily for debugging
        negative_feedback_query = (
            select(
                Completion.id.label('completion_id'),
                Completion.completion.label('completion_content'),
                Completion.created_at.label('completion_created_at'),
                Report.user_id,
                Report.id.label('report_id'),
                func.coalesce(User.name, 'Unknown User').label('user_name'),
                func.coalesce(User.email, '').label('user_email'),
                CompletionFeedback.id.label('feedback_id'),
                CompletionFeedback.direction.label('feedback_direction'),
                CompletionFeedback.message.label('feedback_message'),
                CompletionFeedback.created_at.label('feedback_created_at')
            )
            .select_from(Completion)
            .join(Report, Completion.report_id == Report.id)
            .outerjoin(User, Report.user_id == User.id)
            .join(CompletionFeedback, Completion.id == CompletionFeedback.completion_id)
            .where(
                Report.organization_id == organization.id,
                CompletionFeedback.direction == -1
                # Temporarily remove date filters to see if we get data
                # CompletionFeedback.created_at >= start_date,
                # CompletionFeedback.created_at <= end_date
            )
            .order_by(CompletionFeedback.created_at.desc())
        )
        
        # Execute both queries
        failed_steps_result = await db.execute(failed_steps_query)
        failed_steps_rows = failed_steps_result.all()
        
        negative_feedback_result = await db.execute(negative_feedback_query)
        negative_feedback_rows = negative_feedback_result.all()
        
        print(f"Debug: Found {len(failed_steps_rows)} failed steps and {len(negative_feedback_rows)} negative feedback items")
        
        # Combine and process results
        all_items = []
        
        # Process failed steps
        for row in failed_steps_rows:
            item_data = {
                'completion_id': row.completion_id,
                'completion_content': row.completion_content,
                'completion_created_at': row.completion_created_at,
                'user_id': row.user_id,
                'report_id': row.report_id,
                'user_name': row.user_name,
                'user_email': row.user_email,
                'issue_type': 'failed_step',
                'step_id': row.step_id,
                'step_title': row.step_title,
                'step_status': row.step_status,
                'step_code': row.step_code,
                'step_data_model': row.step_data_model,
                'step_created_at': row.step_created_at,
                'feedback_id': None,
                'feedback_direction': None,
                'feedback_message': None,
                'feedback_created_at': None
            }
            all_items.append(item_data)
        
        # Process negative feedback
        for row in negative_feedback_rows:
            item_data = {
                'completion_id': row.completion_id,
                'completion_content': row.completion_content,
                'completion_created_at': row.completion_created_at,
                'user_id': row.user_id,
                'report_id': row.report_id,
                'user_name': row.user_name,
                'user_email': row.user_email,
                'issue_type': 'negative_feedback',
                'step_id': None,
                'step_title': None,
                'step_status': None,
                'step_code': None,
                'step_data_model': None,
                'step_created_at': None,
                'feedback_id': row.feedback_id,
                'feedback_direction': row.feedback_direction,
                'feedback_message': row.feedback_message,
                'feedback_created_at': row.feedback_created_at
            }
            all_items.append(item_data)
        
        # Sort by creation date and apply pagination
        all_items.sort(key=lambda x: x['completion_created_at'], reverse=True)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_items = all_items[start_idx:end_idx]
        
        # Get count metrics
        failed_steps_count = len(failed_steps_rows)
        negative_feedback_count = len(negative_feedback_rows)
        
        # Process the results and build diagnosis items
        diagnosis_items = []
        
        for item_data in paginated_items:
            # Build step info if available
            step_info = None
            if item_data['step_id']:
                step_info = DiagnosisStepData(
                    step_id=str(item_data['step_id']),
                    step_title=item_data['step_title'] or "",
                    step_status=item_data['step_status'] or "",
                    step_code=item_data['step_code'],
                    step_data_model=item_data['step_data_model'],
                    created_at=item_data['step_created_at'] or item_data['completion_created_at']
                )
            
            # Build feedback info if available
            feedback_info = None
            if item_data['feedback_id']:
                feedback_info = DiagnosisFeedbackData(
                    feedback_id=str(item_data['feedback_id']),
                    direction=item_data['feedback_direction'],
                    message=item_data['feedback_message'],
                    created_at=item_data['feedback_created_at'] or item_data['completion_created_at']
                )
            
            # Get head completion (first completion in the report)
            head_completion_query = (
                select(Completion)
                .where(
                    Completion.report_id == item_data['report_id'],
                    Completion.role == 'user'
                )
                .order_by(Completion.created_at.asc())
                .limit(1)
            )
            head_completion_result = await db.execute(head_completion_query)
            head_completion = head_completion_result.scalar_one_or_none()
            
            head_completion_prompt = ""
            if head_completion and head_completion.prompt:
                if isinstance(head_completion.prompt, dict):
                    head_completion_prompt = head_completion.prompt.get('content', '')
                else:
                    head_completion_prompt = str(head_completion.prompt)
            
            # Get completion content
            completion_content = ""
            if item_data['completion_content']:
                if isinstance(item_data['completion_content'], dict):
                    completion_content = item_data['completion_content'].get('content', '')
                else:
                    completion_content = str(item_data['completion_content'])
            
            diagnosis_item = DiagnosisItemData(
                id=str(item_data['completion_id']),
                head_completion_id=str(head_completion.id) if head_completion else "",
                head_completion_prompt=head_completion_prompt,
                problematic_completion_id=str(item_data['completion_id']),
                problematic_completion_content=completion_content,
                user_id=str(item_data['user_id']) if item_data['user_id'] else "",
                user_name=item_data['user_name'],
                user_email=item_data['user_email'],
                report_id=str(item_data['report_id']),
                issue_type=item_data['issue_type'],
                step_info=step_info,
                feedback_info=feedback_info,
                created_at=item_data['completion_created_at'],
                trace_url=f"/reports/{item_data['report_id']}"
            )
            
            diagnosis_items.append(diagnosis_item)
        
        return DiagnosisMetrics(
            diagnosis_items=diagnosis_items,
            total_items=len(all_items),
            failed_steps_count=failed_steps_count,
            negative_feedback_count=negative_feedback_count,
            date_range=DateRange(
                start=start_date.isoformat(),
                end=end_date.isoformat()
            )
        )

    async def get_trace_data(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        report_id: str,
        issue_completion_id: str
    ) -> TraceData:
        """Get detailed trace data for a specific report and issue"""
        
        # Get the report
        report_query = select(Report).where(
            Report.id == report_id,
            Report.organization_id == organization.id
        )
        report_result = await db.execute(report_query)
        report = report_result.scalar_one_or_none()
        
        if not report:
            raise ValueError(f"Report {report_id} not found")
        
        # Get user info
        user = None
        if report.user_id:
            user_query = select(User).where(User.id == report.user_id)
            user_result = await db.execute(user_query)
            user = user_result.scalar_one_or_none()
        
        # Get all completions for this report
        completions_query = (
            select(Completion)
            .where(Completion.report_id == report_id)
            .order_by(Completion.created_at.asc())
        )
        completions_result = await db.execute(completions_query)
        completions = completions_result.scalars().all()
        
        # Get all steps for this report
        steps_query = (
            select(Step)
            .join(Widget, Step.widget_id == Widget.id)
            .where(Widget.report_id == report_id)
            .order_by(Step.created_at.asc())
        )
        steps_result = await db.execute(steps_query)
        steps = steps_result.scalars().all()
        
        # Get all feedback for completions in this report
        completion_ids = [c.id for c in completions]
        feedbacks_query = (
            select(CompletionFeedback)
            .where(CompletionFeedback.completion_id.in_(completion_ids))
            .order_by(CompletionFeedback.created_at.asc())
        )
        feedbacks_result = await db.execute(feedbacks_query)
        feedbacks = feedbacks_result.scalars().all()
        
        # Determine issue type
        issue_type = "unknown"
        
        # Check if the issue completion has failed steps
        failed_steps = [s for s in steps if any(c.id == issue_completion_id and c.step_id == s.id for c in completions) and s.status == 'error']
        negative_feedbacks = [f for f in feedbacks if f.completion_id == issue_completion_id and f.direction == -1]
        
        if failed_steps and negative_feedbacks:
            issue_type = "both"
        elif failed_steps:
            issue_type = "failed_step"
        elif negative_feedbacks:
            issue_type = "negative_feedback"
        
        # Build completion data
        head_completion = None
        completion_data = []
        
        for completion in completions:
            # Get content
            content = ""
            if completion.completion and isinstance(completion.completion, dict):
                content = completion.completion.get('content', '')
            elif completion.completion:
                content = str(completion.completion)
            
            # Get prompt content for user completions
            if completion.role == 'user' and completion.prompt:
                if isinstance(completion.prompt, dict):
                    content = completion.prompt.get('content', '')
                else:
                    content = str(completion.prompt)
            
            # Get reasoning
            reasoning = ""
            if completion.completion and isinstance(completion.completion, dict):
                reasoning = completion.completion.get('reasoning', '')
            
            # Check if this completion has issues
            has_issue = completion.id == issue_completion_id
            completion_issue_type = None
            if has_issue:
                completion_issue_type = issue_type
            
            trace_completion = TraceCompletionData(
                completion_id=str(completion.id),
                role=completion.role,
                content=content,
                reasoning=reasoning,
                created_at=completion.created_at,
                status=completion.status,
                has_issue=has_issue,
                issue_type=completion_issue_type
            )
            
            completion_data.append(trace_completion)
            
            # Set head completion (first user completion)
            if completion.role == 'user' and head_completion is None:
                head_completion = trace_completion
        
        # Build step data
        step_data = []
        for step in steps:
            # Find completion that belongs to this step
            step_completion = next((c for c in completions if c.step_id == step.id), None)
            has_issue = step.status == 'error' and step_completion and step_completion.id == issue_completion_id
            
            trace_step = TraceStepData(
                step_id=str(step.id),
                title=step.title,
                status=step.status,
                code=step.code,
                data_model=step.data_model,
                data=step.data,
                created_at=step.created_at,
                completion_id=str(step_completion.id) if step_completion else "",
                has_issue=has_issue
            )
            step_data.append(trace_step)
        
        # Build feedback data
        feedback_data = []
        for feedback in feedbacks:
            trace_feedback = TraceFeedbackData(
                feedback_id=str(feedback.id),
                direction=feedback.direction,
                message=feedback.message,
                created_at=feedback.created_at,
                completion_id=str(feedback.completion_id)
            )
            feedback_data.append(trace_feedback)
        
        return TraceData(
            report_id=report_id,
            head_completion=head_completion or completion_data[0] if completion_data else None,
            completions=completion_data,
            steps=step_data,
            feedbacks=feedback_data,
            issue_completion_id=issue_completion_id,
            issue_type=issue_type,
            user_name=user.name if user else "Unknown User",
            user_email=user.email if user else None
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