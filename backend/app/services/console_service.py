from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text, literal, Integer
from app.models.organization import Organization
from app.models.user import User
from app.models.completion import Completion
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget
from app.models.completion_feedback import CompletionFeedback
from app.models.table_stats import TableStats
from app.schemas.console_schema import (
    SimpleMetrics, MetricsQueryParams, MetricsComparison, 
    TimeSeriesMetrics, ActivityMetrics, PerformanceMetrics,
    TimeSeriesPoint, TimeSeriesPointFloat, DateRange,
    TableUsageData, TableUsageMetrics, TableJoinsHeatmap, TableJoinData,
    TopUserData, TopUsersMetrics, RecentNegativeFeedbackData, RecentNegativeFeedbackMetrics,
    TraceData, TraceCompletionData, TraceStepData, TraceFeedbackData,
    CompactIssuesResponse, CompactIssueItem,
    AgentExecutionSummaryItem, AgentExecutionSummariesResponse
)
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
from app.settings.logging_config import get_logger
from collections import Counter, defaultdict
import json
import re
from pydantic import BaseModel
from app.models.membership import Membership
from app.models.tool_execution import ToolExecution
from app.models.completion_block import CompletionBlock
from app.schemas.agent_execution_trace_schema import AgentExecutionTraceResponse
from app.schemas.completion_v2_schema import CompletionBlockV2Schema
from app.serializers.completion_v2 import serialize_block_v2
from app.models.agent_execution import AgentExecution
from app.models.context_snapshot import ContextSnapshot
from app.models.tool_execution import ToolExecution
from app.models.plan_decision import PlanDecision
from app.models.completion import Completion
from app.models.completion_feedback import CompletionFeedback
from app.models.step import Step
from sqlalchemy.orm import aliased

logger = get_logger(__name__)

class ConsoleService:
    
    def _to_utc_naive(self, dt: Optional[datetime]) -> Optional[datetime]:
        """Convert aware datetimes to UTC and strip tzinfo; leave naive as-is.

        This ensures compatibility with TIMESTAMP WITHOUT TIME ZONE columns
        in PostgreSQL and works with SQLite as well.
        """
        if dt is None:
            return None
        # If dt is timezone-aware, convert to UTC and remove tzinfo
        if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        # Treat naive datetime as UTC-naive
        return dt

    def _normalize_date_range(self, start_date: Optional[datetime], end_date: Optional[datetime]) -> tuple[datetime, datetime]:
        """Normalize date range to ensure end_date includes the full day"""
        
        # Normalize timezone to UTC-naive if provided
        if end_date:
            end_date = self._to_utc_naive(end_date)
        if start_date:
            start_date = self._to_utc_naive(start_date)

        # Default to last 30 days if no dates provided (UTC-naive)
        if not end_date:
            end_date = datetime.utcnow()
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
        
        # Calculate judge metrics averages
        judge_metrics_query = (
            select(
                func.avg(Completion.instructions_effectiveness).label('avg_instructions_effectiveness'),
                func.avg(Completion.context_effectiveness).label('avg_context_effectiveness'), 
                func.avg(Completion.response_score).label('avg_response_score')
            )
            .join(Report)
            .where(
                report_filter,
                Completion.created_at >= start_date,
                Completion.created_at <= end_date,
                Completion.instructions_effectiveness.isnot(None)  # Only include completions with judge scores
            )
        )
        
        judge_result = await db.execute(judge_metrics_query)
        judge_data = judge_result.first()
        
        # Calculate accuracy rate from response scores
        # Count ALL completions
        total_completions_query = (
            select(func.count(Completion.id))
            .join(Report)
            .where(
                report_filter,
                Completion.created_at >= start_date,
                Completion.created_at <= end_date
            )
        )
        
        total_completions_result = await db.execute(total_completions_query)
        total_completions = total_completions_result.scalar() or 0
        
        # Sum response scores (only non-null ones)
        response_score_query = (
            select(func.sum(Completion.response_score))
            .join(Report)
            .where(
                report_filter,
                Completion.created_at >= start_date,
                Completion.created_at <= end_date,
                Completion.response_score.isnot(None)
            )
        )
        
        response_score_result = await db.execute(response_score_query)
        response_score_sum = response_score_result.scalar() or 0
        
        # Calculate accuracy: sum of scores / total completions * 20
        accuracy_rate = (response_score_sum / total_completions * 20) if total_completions > 0 else 0
        
        
        return SimpleMetrics(
            total_messages=total_messages,
            total_queries=total_queries,
            total_feedbacks=total_feedbacks,
            active_users=active_users,
            accuracy=f"{accuracy_rate:.1f}%",
            instructions_coverage="90%",  # Placeholder for instruction template coverage
            instructions_effectiveness=(judge_data.avg_instructions_effectiveness or 0.0) * 20,
            context_effectiveness=(judge_data.avg_context_effectiveness or 0.0) * 20,
            response_quality=(judge_data.avg_response_score or 0.0) * 20
        )

    async def get_metrics_with_comparison(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> MetricsComparison:
        """Get metrics with previous period comparison"""
        
        # Default to last 30 days if no dates provided, normalize to UTC-naive
        end_date = params.end_date or datetime.utcnow()
        start_date = params.start_date or (end_date - timedelta(days=30))

        end_date = self._to_utc_naive(end_date)
        start_date = self._to_utc_naive(start_date)
        
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
        numeric_fields = ["total_messages", "total_queries", "total_feedbacks", "active_users", 
                         "instructions_effectiveness", "context_effectiveness", "response_quality"]
        
        for field in numeric_fields:
            current_val = getattr(current, field)
            previous_val = getattr(previous, field)
            
            absolute_change = current_val - previous_val
            percentage_change = (absolute_change / previous_val * 100) if previous_val > 0 else 0
            
            changes[field] = {
                "absolute": round(absolute_change, 2),
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
        coverage_data = []
        instructions_effectiveness_data = []
        context_effectiveness_data = []
        response_quality_data = []
        feedback_data = []
        
        # For smoothing - keep track of last non-zero values
        last_instructions_effectiveness = 0.0
        last_context_effectiveness = 0.0
        last_response_quality = 0.0
        
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
            
            # Calculate accuracy rate from response scores for this day
            # Count ALL completions for this day
            total_completions_result = await db.execute(
                select(func.count(Completion.id))
                .join(Report)
                .where(
                    Report.organization_id == organization.id,
                    Completion.created_at >= interval_start,
                    Completion.created_at < interval_end
                )
            )
            total_completions = total_completions_result.scalar() or 0
            
            # Sum response scores (only non-null ones)
            response_score_result = await db.execute(
                select(func.sum(Completion.response_score))
                .join(Report)
                .where(
                    Report.organization_id == organization.id,
                    Completion.created_at >= interval_start,
                    Completion.created_at < interval_end,
                    Completion.response_score.isnot(None)
                )
            )
            response_score_sum = response_score_result.scalar() or 0
            
            # Calculate accuracy: sum of scores / total completions * 20
            accuracy_rate = (response_score_sum / total_completions * 20) if total_completions > 0 else 0
            
            # Positive feedback rate for this day (for feedback metric)
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
            positive_rate = (positive_feedbacks / total_feedbacks * 100) if total_feedbacks > 0 else 0
            
            # Calculate judge metrics for this day
            judge_metrics_result = await db.execute(
                select(
                    func.avg(Completion.instructions_effectiveness).label('avg_instructions_effectiveness'),
                    func.avg(Completion.context_effectiveness).label('avg_context_effectiveness'),
                    func.avg(Completion.response_score).label('avg_response_score')
                )
                .join(Report)
                .where(
                    Report.organization_id == organization.id,
                    Completion.created_at >= interval_start,
                    Completion.created_at < interval_end,
                    Completion.instructions_effectiveness.isnot(None)
                )
            )
            judge_data = judge_metrics_result.first()
            
            # Apply smoothing logic and convert to 1-100 scale
            current_instructions_effectiveness = (judge_data.avg_instructions_effectiveness or 0.0) * 20
            current_context_effectiveness = (judge_data.avg_context_effectiveness or 0.0) * 20
            current_response_quality = (judge_data.avg_response_score or 0.0) * 20
            
            # For smoothing: if no queries (scores are 0), keep last non-zero value
            if current_instructions_effectiveness > 0:
                last_instructions_effectiveness = current_instructions_effectiveness
            elif last_instructions_effectiveness > 0:
                current_instructions_effectiveness = last_instructions_effectiveness
                
            if current_context_effectiveness > 0:
                last_context_effectiveness = current_context_effectiveness
            elif last_context_effectiveness > 0:
                current_context_effectiveness = last_context_effectiveness
                
            if current_response_quality > 0:
                last_response_quality = current_response_quality
            elif last_response_quality > 0:
                current_response_quality = last_response_quality
            
            # Show all days with activity (messages or queries)
            has_activity = messages_count > 0 or queries_count > 0
            
            if has_activity:
                date_str = interval_start.strftime('%Y-%m-%d')
                
                # Create TimeSeriesPoint objects
                messages_data.append(TimeSeriesPoint(date=date_str, value=messages_count))
                queries_data.append(TimeSeriesPoint(date=date_str, value=queries_count))
                
                # Create TimeSeriesPointFloat objects for percentages
                accuracy_data.append(TimeSeriesPointFloat(date=date_str, value=accuracy_rate))
                coverage_data.append(TimeSeriesPointFloat(date=date_str, value=90.0))  # Placeholder for instruction coverage
                instructions_effectiveness_data.append(TimeSeriesPointFloat(date=date_str, value=current_instructions_effectiveness))
                context_effectiveness_data.append(TimeSeriesPointFloat(date=date_str, value=current_context_effectiveness))
                response_quality_data.append(TimeSeriesPointFloat(date=date_str, value=current_response_quality))
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
                instructions_coverage=coverage_data,
                instructions_effectiveness=instructions_effectiveness_data,
                context_effectiveness=context_effectiveness_data,
                response_quality=response_quality_data,
                positive_feedback_rate=feedback_data
            )
        )

    async def get_compact_issues(
        self,
        db: AsyncSession,
        organization: Organization,
        params: MetricsQueryParams,
        page: int = 1,
        page_size: int = 50,
        issue_filter: Optional[str] = None
    ) -> CompactIssuesResponse:
        """Return compact completion-anchored issues (tool errors or negative feedback)."""
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)

        # Base completions in org and date range
        base_query = (
            select(
                Completion.id.label('completion_id'),
                Completion.created_at.label('created_at'),
                Completion.completion.label('completion_content'),
                Completion.role.label('completion_role'),
                Report.id.label('report_id'),
                func.coalesce(User.name, 'Unknown User').label('user_name'),
                func.coalesce(User.email, '').label('user_email'),
                Step.id.label('step_id'),
                Step.status.label('step_status')
            )
            .select_from(Completion)
            .join(Report, Completion.report_id == Report.id)
            .outerjoin(User, Report.user_id == User.id)
            .outerjoin(Step, Completion.step_id == Step.id)
            .where(
                Report.organization_id == organization.id,
                Completion.created_at >= start_date,
                Completion.created_at <= end_date
            )
            .order_by(Completion.created_at.desc())
        )

        result = await db.execute(base_query)
        base_rows = result.all()

        if not base_rows:
            return CompactIssuesResponse(
                items=[],
                total_items=0,
                date_range=DateRange(start=start_date.isoformat(), end=end_date.isoformat())
            )

        completion_ids = [r.completion_id for r in base_rows]
        step_ids = [r.step_id for r in base_rows if r.step_id]

        # Failed tool executions per step
        te_rows = []
        if step_ids:
            te_query = (
                select(ToolExecution)
                .where(
                    ToolExecution.created_step_id.in_(step_ids),
                    ToolExecution.success == False
                )
                .order_by(
                    ToolExecution.created_step_id.asc(),
                    ToolExecution.attempt_number.desc(),
                    func.coalesce(ToolExecution.completed_at, ToolExecution.created_at).desc()
                )
            )
            te_result = await db.execute(te_query)
            te_rows = te_result.scalars().all()

        # Keep first failure per step
        step_id_to_te: Dict[str, ToolExecution] = {}
        for te in te_rows:
            if te.created_step_id and te.created_step_id not in step_id_to_te:
                step_id_to_te[te.created_step_id] = te

        # Negative feedback per completion
        cf_query = (
            select(
                CompletionFeedback.completion_id,
                CompletionFeedback.direction,
                CompletionFeedback.message,
                CompletionFeedback.created_at
            )
            .where(
                CompletionFeedback.completion_id.in_(completion_ids),
                CompletionFeedback.direction == -1
            )
        )
        cf_result = await db.execute(cf_query)
        cf_rows = cf_result.all()
        completion_id_to_cf = {r.completion_id: r for r in cf_rows}

        # Head prompt snippets for reports
        report_ids = list({r.report_id for r in base_rows})
        head_prompts: Dict[str, str] = {}
        if report_ids:
            hp_query = (
                select(Completion.report_id, Completion.prompt, Completion.created_at)
                .where(Completion.report_id.in_(report_ids), Completion.role == 'user')
                .order_by(Completion.report_id.asc(), Completion.created_at.asc())
            )
            hp_result = await db.execute(hp_query)
            for rep_id, prompt, _ in hp_result.all():
                if rep_id not in head_prompts:
                    if isinstance(prompt, dict):
                        head_prompts[rep_id] = str(prompt.get('content') or '')
                    else:
                        head_prompts[rep_id] = str(prompt or '')

        def classify_error(message: Optional[str]) -> str:
            return self._classify_error_type(message)

        include_all = issue_filter in ('all_queries',)

        items: List[CompactIssueItem] = []
        for r in base_rows:
            cf = completion_id_to_cf.get(r.completion_id)
            te = step_id_to_te.get(r.step_id) if r.step_id else None

            issue_type = None
            summary_text = None
            full_message = None
            tool_name = None
            tool_action = None

            if te is not None:
                issue_type = classify_error(te.error_message)
                full_message = te.error_message or ''
                summary_text = (str(full_message).split('\n', 1)[0]) if full_message else 'Error'
                tool_name = te.tool_name
                tool_action = te.tool_action
            elif cf is not None:
                issue_type = 'negative_feedback'
                full_message = cf.message or ''
                summary_text = (str(full_message).split('\n', 1)[0]) if full_message else 'Negative feedback'
            else:
                if not include_all:
                    continue
                issue_type = 'no_issue'
                # Derive a helpful summary from completion content
                try:
                    content_val = r.completion_content
                    if isinstance(content_val, dict):
                        content_text = str(content_val.get('content') or content_val.get('text') or '')
                    else:
                        content_text = str(content_val or '')
                    content_text = content_text.strip()
                    summary_text = content_text.split('\n', 1)[0][:140] if content_text else (r.completion_role.title() + ' Completion' if r.completion_role else 'Completion')
                except Exception:
                    summary_text = 'Completion'

            if issue_filter and issue_filter not in ('all', 'all_queries') and issue_type != issue_filter:
                continue

            items.append(CompactIssueItem(
                completion_id=str(r.completion_id),
                created_at=r.created_at,
                issue_type=issue_type,
                summary_text=summary_text,
                full_message=full_message,
                tool_name=tool_name,
                tool_action=tool_action,
                user_name=r.user_name,
                user_email=r.user_email,
                head_prompt_snippet=(head_prompts.get(r.report_id) or '')[:140],
                report_id=str(r.report_id),
                trace_url=f"/reports/{r.report_id}"
            ))

        total_items = len(items)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paged = items[start_idx:end_idx]

        return CompactIssuesResponse(
            items=paged,
            total_items=total_items,
            date_range=DateRange(start=start_date.isoformat(), end=end_date.isoformat())
        )

    async def get_table_usage_metrics(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        params: MetricsQueryParams
    ) -> TableUsageMetrics:
        """Get table usage statistics using precomputed TableStats within date range"""
        
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)
        
        # Aggregate usage by table within the date range using TableStats
        usage_sum = func.sum(TableStats.usage_count).label('usage_sum')
        table_fqn = TableStats.table_fqn.label('table_fqn')

        stats_query = (
            select(table_fqn, usage_sum)
            .where(
                TableStats.org_id == organization.id,
                TableStats.last_used_at.isnot(None),
                TableStats.last_used_at >= start_date,
                TableStats.last_used_at <= end_date,
                TableStats.usage_count > 0
            )
            .group_by(table_fqn)
            .order_by(func.sum(TableStats.usage_count).desc())
            .limit(10)
        )

        result = await db.execute(stats_query)
        rows = result.all()

        # Total usage across all tables within range (not limited to top 10)
        total_usage_query = (
            select(func.coalesce(func.sum(TableStats.usage_count), 0))
            .where(
                TableStats.org_id == organization.id,
                TableStats.last_used_at.isnot(None),
                TableStats.last_used_at >= start_date,
                TableStats.last_used_at <= end_date,
                TableStats.usage_count > 0
            )
        )
        total_usage_result = await db.execute(total_usage_query)
        total_usage_all = int(total_usage_result.scalar() or 0)

        top_tables = []
        total_usage = 0
        for row in rows:
            table_name = row.table_fqn
            usage_count = int(row.usage_sum or 0)
            total_usage += usage_count
            top_tables.append(
                TableUsageData(
                    table_name=table_name,
                    usage_count=usage_count,
                    database_name=self._extract_database_name(table_name)
                )
            )
        
        return TableUsageMetrics(
            top_tables=top_tables,
            total_queries_analyzed=total_usage_all,
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
                issue_type=completion_issue_type,
                instructions_effectiveness=completion.instructions_effectiveness,
                context_effectiveness=completion.context_effectiveness,
                response_score=completion.response_score
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

    async def get_agent_execution_trace(
        self,
        db: AsyncSession,
        organization: Organization,
        agent_execution_id: str
    ) -> AgentExecutionTraceResponse:
        """Return agent execution with its completion blocks (UI schema) and prompt snippet."""
        # Fetch AE scoped to org
        ae_query = (
            select(AgentExecution)
            .where(
                AgentExecution.id == agent_execution_id,
                AgentExecution.organization_id == organization.id
            )
        )
        ae_result = await db.execute(ae_query)
        agent_execution = ae_result.scalar_one_or_none()
        if not agent_execution:
            raise ValueError("Agent execution not found")

        # Fetch blocks associated to this AE ordered by (seq, block_index)
        blocks_query = (
            select(CompletionBlock)
            .where(CompletionBlock.agent_execution_id == agent_execution.id)
            .order_by(CompletionBlock.block_index.asc())
        )
        blocks_result = await db.execute(blocks_query)
        blocks = blocks_result.scalars().all()

        # Serialize blocks to UI schema
        block_schemas: List[CompletionBlockV2Schema] = []
        for b in blocks:
            block_schemas.append(await serialize_block_v2(db, b))

        # Head prompt snippet from the head completion (first user completion in report)
        head_prompt = None
        head_snapshot: Optional[ContextSnapshot] = None
        if agent_execution.report_id:
            hp_query = (
                select(Completion)
                .where(Completion.report_id == agent_execution.report_id, Completion.role == 'user')
                .order_by(Completion.created_at.asc())
                .limit(1)
            )
            hp_res = await db.execute(hp_query)
            head_c = hp_res.scalar_one_or_none()
            if head_c and head_c.prompt:
                if isinstance(head_c.prompt, dict):
                    head_prompt = str(head_c.prompt.get('content') or '')
                else:
                    head_prompt = str(head_c.prompt)

        # Fetch the earliest context snapshot for this agent execution (best-effort)
        cs_query = (
            select(ContextSnapshot)
            .where(ContextSnapshot.agent_execution_id == agent_execution.id)
            .order_by(ContextSnapshot.created_at.asc())
            .limit(1)
        )
        cs_res = await db.execute(cs_query)
        head_snapshot = cs_res.scalar_one_or_none()

        return AgentExecutionTraceResponse(
            agent_execution=agent_execution,
            completion_blocks=block_schemas,
            head_prompt_snippet=(head_prompt or '')[:160],
            head_context_snapshot=head_snapshot
        )

    async def get_tool_executions_diagnosis(self, db: AsyncSession, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None, page: int = 1, page_size: int = 20) -> dict:
        """Return tool executions joined with plan decisions, feedback (via completion), and related step if exists."""
        from sqlalchemy import select, and_, desc

        base = (
            select(
                ToolExecution.id,
                ToolExecution.created_at,
                ToolExecution.tool_name,
                ToolExecution.tool_action,
                ToolExecution.status,
                ToolExecution.duration_ms,
                PlanDecision.plan_type,
                PlanDecision.seq,
                PlanDecision.loop_index,
                CompletionFeedback.direction.label('feedback_direction'),
                CompletionFeedback.message.label('feedback_message'),
                Step.id.label('step_id'),
                Step.title.label('step_title'),
                Step.status.label('step_status'),
            )
            .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
            .join(Completion, Completion.id == AgentExecution.completion_id)
            .outerjoin(CompletionFeedback, CompletionFeedback.completion_id == Completion.id)
            .outerjoin(PlanDecision, PlanDecision.id == ToolExecution.plan_decision_id)
            .outerjoin(Step, Step.id == ToolExecution.created_step_id)
        )
        conditions = []
        if start_date:
            conditions.append(ToolExecution.created_at >= start_date)
        if end_date:
            conditions.append(ToolExecution.created_at <= end_date)
        if conditions:
            base = base.where(and_(*conditions))

        total_q = select(func.count()).select_from(base.subquery())
        total_res = await db.execute(total_q)
        total = total_res.scalar_one() or 0

        q = base.order_by(desc(ToolExecution.created_at)).limit(page_size).offset((page - 1) * page_size)
        res = await db.execute(q)
        rows = res.all()

        def map_row(r):
            d = {
                'id': str(r.id),
                'created_at': r.created_at,
                'tool_name': r.tool_name,
                'tool_action': r.tool_action,
                'status': r.status,
                'duration_ms': r.duration_ms,
                'plan_type': r.plan_type,
                'seq': r.seq,
                'loop_index': r.loop_index,
                'feedback_direction': r.feedback_direction,
                'feedback_message': r.feedback_message,
                'step_id': r.step_id,
                'step_title': r.step_title,
                'step_status': r.step_status,
            }
            return d

        items = [map_row(r) for r in rows]
        return {
            'items': items,
            'total_items': total,
            'date_range': {
                'start': start_date.isoformat() if start_date else '',
                'end': end_date.isoformat() if end_date else ''
            }
        }

    async def get_agent_execution_trace_by_completion(
        self,
        db: AsyncSession,
        organization: Organization,
        completion_id: str
    ) -> AgentExecutionTraceResponse:
        """Find latest agent execution for a completion and return its trace."""
        ae_query = (
            select(AgentExecution)
            .where(
                AgentExecution.completion_id == completion_id,
                AgentExecution.organization_id == organization.id
            )
            .order_by(AgentExecution.created_at.desc())
            .limit(1)
        )
        ae_res = await db.execute(ae_query)
        ae = ae_res.scalar_one_or_none()
        if not ae:
            raise ValueError("Agent execution not found for completion")
        return await self.get_agent_execution_trace(db, organization, ae.id)

    async def get_agent_execution_summaries(
        self,
        db: AsyncSession,
        organization: Organization,
        params: MetricsQueryParams,
        page: int = 1,
        page_size: int = 20,
        issue_filter: Optional[str] = None
    ) -> AgentExecutionSummariesResponse:
        """Aggregate agent executions joined with completion, feedback, tool counts, and report/user metadata."""
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)

        base_query = (
            select(
                AgentExecution.id.label('ae_id'),
                AgentExecution.created_at.label('created_at'),
                AgentExecution.status.label('ae_status'),
                AgentExecution.error_json.label('error_json'),
                AgentExecution.completion_id.label('completion_id'),
                AgentExecution.user_id.label('ae_user_id'),
                AgentExecution.report_id.label('report_id'),
                func.coalesce(User.name, 'Unknown User').label('user_name'),
                func.coalesce(User.email, '').label('user_email'),
                func.coalesce(Report.title, '').label('report_title')
            )
            .select_from(AgentExecution)
            .outerjoin(User, User.id == AgentExecution.user_id)
            .outerjoin(Report, Report.id == AgentExecution.report_id)
            .where(
                AgentExecution.organization_id == organization.id,
                AgentExecution.created_at >= start_date,
                AgentExecution.created_at <= end_date
            )
            .order_by(AgentExecution.created_at.desc())
        )

        total_q = select(func.count()).select_from(base_query.subquery())
        total_res = await db.execute(total_q)
        total_items = int(total_res.scalar() or 0)

        # Apply filters if specified
        if issue_filter == 'negative_feedback':
            # Filter to agent executions with negative feedback
            base_query = base_query.join(
                CompletionFeedback, CompletionFeedback.completion_id == AgentExecution.completion_id
            ).where(CompletionFeedback.direction == -1)
        elif issue_filter in ('code_errors', 'failed_queries'):
            # Filter to agent executions with failed create_and_execute_code tools
            failed_te_subquery = (
                select(ToolExecution.agent_execution_id)
                .where(
                    ToolExecution.tool_name == 'create_and_execute_code',
                    ToolExecution.success == False
                )
            )
            base_query = base_query.where(AgentExecution.id.in_(failed_te_subquery))

        # Recalculate total with filters
        total_q = select(func.count()).select_from(base_query.subquery())
        total_res = await db.execute(total_q)
        total_items = int(total_res.scalar() or 0)

        q = base_query.limit(page_size).offset((page - 1) * page_size)
        res = await db.execute(q)
        rows = res.all()

        if not rows:
            return AgentExecutionSummariesResponse(
                items=[],
                total_items=total_items,
                date_range=DateRange(start=start_date.isoformat(), end=end_date.isoformat())
            )

        ae_ids = [r.ae_id for r in rows]
        completion_ids = [r.completion_id for r in rows if r.completion_id]
        report_ids = [r.report_id for r in rows if r.report_id]

        # Prompts for completions
        prompts: dict[str, str] = {}
        if completion_ids:
            prompt_q = select(Completion.id, Completion.prompt).where(Completion.id.in_(completion_ids))
            pres = await db.execute(prompt_q)
            for cid, prompt in pres.all():
                try:
                    if isinstance(prompt, dict):
                        prompts[str(cid)] = str(prompt.get('content') or prompt.get('text') or '')
                    else:
                        prompts[str(cid)] = str(prompt or '')
                except Exception:
                    prompts[str(cid)] = ''

        # Tool execution counts per AE
        te_counts = {str(ae_id): {'total': 0, 'success': 0, 'failed': 0} for ae_id in ae_ids}
        if ae_ids:
            te_q = (
                select(
                    ToolExecution.agent_execution_id,
                    func.count(ToolExecution.id).label('cnt'),
                    func.sum(func.cast(ToolExecution.success, Integer)).label('success_cnt')
                )
                .where(ToolExecution.agent_execution_id.in_(ae_ids))
                .group_by(ToolExecution.agent_execution_id)
            )
            te_res = await db.execute(te_q)
            for ae_id, cnt, success_cnt in te_res.all():
                total = int(cnt or 0)
                successes = int(success_cnt or 0)
                failures = max(total - successes, 0)
                te_counts[str(ae_id)] = {'total': total, 'success': successes, 'failed': failures}

        # Feedback per completion (most recent)
        feedback_map: dict[str, dict] = {}
        if completion_ids:
            fb_q = (
                select(
                    CompletionFeedback.completion_id,
                    CompletionFeedback.direction,
                    CompletionFeedback.created_at,
                    CompletionFeedback.message
                )
                .where(CompletionFeedback.completion_id.in_(completion_ids))
                .order_by(CompletionFeedback.completion_id.asc(), CompletionFeedback.created_at.desc())
            )
            fb_res = await db.execute(fb_q)
            for cid, direction, _, message in fb_res.all():
                scid = str(cid)
                if scid not in feedback_map:
                    status = 'positive' if (direction or 0) > 0 else ('negative' if (direction or 0) < 0 else 'none')
                    feedback_map[scid] = {
                        'direction': int(direction or 0),
                        'status': status,
                        'message': message
                    }

        # Head user prompt per report (fallback)
        head_prompts: dict[str, str] = {}
        # Step titles per AE via ToolExecution.created_step_id
        ae_step_titles: dict[str, List[str]] = {str(ae_id): [] for ae_id in ae_ids}
        if ae_ids:
            te_step_q = (
                select(ToolExecution.agent_execution_id, Step.title)
                .join(Step, Step.id == ToolExecution.created_step_id)
                .where(ToolExecution.agent_execution_id.in_(ae_ids))
                .order_by(ToolExecution.agent_execution_id.asc(), Step.created_at.asc())
            )
            te_step_res = await db.execute(te_step_q)
            for ae_id, step_title in te_step_res.all():
                lst = ae_step_titles.get(str(ae_id))
                if lst is not None and step_title:
                    # collect unique titles preserving order
                    if step_title not in lst:
                        lst.append(step_title)
        if report_ids:
            hp_q = (
                select(Completion.report_id, Completion.prompt, Completion.created_at)
                .where(Completion.report_id.in_(report_ids), Completion.role == 'user')
                .order_by(Completion.report_id.asc(), Completion.created_at.asc())
            )
            hp_res = await db.execute(hp_q)
            for rep_id, prompt, _ in hp_res.all():
                if rep_id not in head_prompts:
                    try:
                        if isinstance(prompt, dict):
                            head_prompts[str(rep_id)] = str(prompt.get('content') or prompt.get('text') or '')
                        else:
                            head_prompts[str(rep_id)] = str(prompt or '')
                    except Exception:
                        head_prompts[str(rep_id)] = ''

        items: List[AgentExecutionSummaryItem] = []
        for r in rows:
            counts = te_counts.get(str(r.ae_id), {'total': 0, 'success': 0, 'failed': 0})
            fb = feedback_map.get(str(r.completion_id), {'direction': 0, 'status': 'none'})
            prompt_text = prompts.get(str(r.completion_id), '')
            if not prompt_text or not str(prompt_text).strip():
                prompt_text = head_prompts.get(str(r.report_id), '')
            report_link = f"/reports/{r.report_id}" if r.report_id else None

            items.append(AgentExecutionSummaryItem(
                agent_execution_id=str(r.ae_id),
                created_at=r.created_at,
                completion_id=str(r.completion_id) if r.completion_id else None,
                prompt=(prompt_text or '')[:200],
                agent_execution_status=r.ae_status,
                error_json=r.error_json,
                total_tools=counts['total'],
                total_failed_tools=counts['failed'],
                total_successful_tools=counts['success'],
                feedback_status=fb['status'],
                feedback_direction=fb['direction'],
                feedback_message=fb.get('message'),
                step_titles=ae_step_titles.get(str(r.ae_id), [])[:5],
                user_name=r.user_name,
                user_email=r.user_email,
                report_id=str(r.report_id) if r.report_id else '',
                report_name=r.report_title or '',
                report_link=report_link
            ))

        return AgentExecutionSummariesResponse(
            items=items,
            total_items=total_items,
            date_range=DateRange(start=start_date.isoformat(), end=end_date.isoformat())
        )

    async def get_diagnosis_dashboard_metrics(
        self,
        db: AsyncSession,
        organization: Organization,
        params: MetricsQueryParams
    ) -> Dict[str, int]:
        """Get dashboard metrics for diagnosis page."""
        start_date, end_date = self._normalize_date_range(params.start_date, params.end_date)

        # Count failed queries (create_and_execute_code tool failures)
        failed_queries_query = (
            select(func.count(func.distinct(ToolExecution.agent_execution_id)))
            .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
            .where(
                AgentExecution.organization_id == organization.id,
                AgentExecution.created_at >= start_date,
                AgentExecution.created_at <= end_date,
                ToolExecution.tool_name == 'create_and_execute_code',
                ToolExecution.success == False
            )
        )
        failed_queries_result = await db.execute(failed_queries_query)
        failed_queries = int(failed_queries_result.scalar() or 0)

        # Count negative feedback
        negative_feedback_query = (
            select(func.count(func.distinct(AgentExecution.id)))
            .join(CompletionFeedback, CompletionFeedback.completion_id == AgentExecution.completion_id)
            .where(
                AgentExecution.organization_id == organization.id,
                AgentExecution.created_at >= start_date,
                AgentExecution.created_at <= end_date,
                CompletionFeedback.direction == -1
            )
        )
        negative_feedback_result = await db.execute(negative_feedback_query)
        negative_feedback = int(negative_feedback_result.scalar() or 0)

        # Total agent executions
        total_query = (
            select(func.count(AgentExecution.id))
            .where(
                AgentExecution.organization_id == organization.id,
                AgentExecution.created_at >= start_date,
                AgentExecution.created_at <= end_date
            )
        )
        total_result = await db.execute(total_query)
        total_items = int(total_result.scalar() or 0)

        return {
            'failed_queries': failed_queries,
            'negative_feedback': negative_feedback,
            'code_errors': failed_queries,  # Same as failed queries (for backward compatibility)
            'total_items': total_items
        }