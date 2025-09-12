"""
Query Context Builder - Similar to WidgetContextBuilder but for Query + Visualization.
"""
import json
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization
from app.ai.context.sections.queries_section import QueriesSection, QueryObservation, QueryVisualizationSummary

from app.settings.logging_config import get_logger


logger = get_logger(__name__)


class QueryContextBuilder:
    def __init__(self, db: AsyncSession, organization, report):
        self.db = db
        self.organization = organization
        self.report = report

    async def build_context(
        self,
        max_queries: int = 5,
        status_filter: Optional[List[str]] = None,
        include_data_preview: bool = True
    ) -> str:
        section = await self.build(max_queries=max_queries, status_filter=status_filter, include_data_preview=include_data_preview)
        return section.render()

    async def build(
        self,
        max_queries: int = 5,
        status_filter: Optional[List[str]] = None,
        include_data_preview: bool = True
    ) -> QueriesSection:
        items: List[QueryObservation] = []
        queries = await self._get_report_queries(self.report.id)
        if len(queries) > max_queries:
            queries = queries[-max_queries:]
        for q in queries:
            obs = await self._build_query_observation(q, include_data_preview=include_data_preview)
            items.append(obs)
        return QueriesSection(items=items)

    async def _get_report_queries(self, report_id: str) -> List[Query]:
        try:
            res = await self.db.execute(select(Query).where(Query.report_id == report_id))
            return list(res.scalars().all())
        except Exception as e:
            logger.error(f"Failed to load queries for report {report_id}: {e}")
            return []

    async def _build_query_observation(self, query: Query, include_data_preview: bool) -> QueryObservation:
        default_step: Step | None = None
        try:
            if getattr(query, 'default_step_id', None):
                default_step = await self.db.get(Step, str(query.default_step_id))
            else:
                # Latest step if default not set
                res = await self.db.execute(
                    select(Step)
                    .where(Step.query_id == str(query.id))
                    .order_by(Step.created_at.desc())
                    .limit(1)
                )
                default_step = res.scalar_one_or_none()
        except Exception:
            default_step = None

        # Visualizations for this query
        visualizations: List[QueryVisualizationSummary] = []
        try:
            res = await self.db.execute(select(Visualization).where(Visualization.query_id == str(query.id)))
            for v in res.scalars().all():
                visualizations.append(QueryVisualizationSummary(
                    id=str(v.id),
                    title=v.title or "",
                    status=getattr(v, 'status', None),
                    view=getattr(v, 'view', None) if isinstance(getattr(v, 'view', None), dict) else None,
                ))
        except Exception:
            pass

        obs = QueryObservation(
            query_id=str(query.id),
            query_title=query.title or "",
            default_step_id=str(default_step.id) if default_step else None,
            default_step_title=getattr(default_step, 'title', None) if default_step else None,
            row_count=0,
            column_names=[],
            data_model=None,
            stats={},
            data_preview=None,
            visualizations=visualizations,
        )

        # Populate step-derived observation fields
        if default_step:
            try:
                if default_step.data_model and isinstance(default_step.data_model, dict):
                    obs.data_model = default_step.data_model
                if default_step.data and isinstance(default_step.data, dict):
                    if 'info' in default_step.data:
                        obs.stats = default_step.data['info']
                    if 'columns' in default_step.data and isinstance(default_step.data['columns'], list):
                        obs.column_names = [c.get('field', '?') for c in default_step.data['columns']]
                    if 'rows' in default_step.data and isinstance(default_step.data['rows'], list):
                        rows = default_step.data['rows']
                        obs.row_count = len(rows)
                        if include_data_preview and rows and obs.column_names:
                            try:
                                cols = obs.column_names
                                preview_rows = rows[:5]
                                lines = []
                                header = " | ".join(cols)
                                lines.append(header)
                                lines.append("-" * len(header))
                                for r in preview_rows:
                                    lines.append(" | ".join(str(r.get(c, "N/A")) for c in cols))
                                obs.data_preview = "\n".join(lines)
                            except Exception:
                                obs.data_preview = None
            except Exception:
                pass

        return obs


