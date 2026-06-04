"""
Query Context Builder - Similar to WidgetContextBuilder but for Query + Visualization.
"""
import json
from collections import defaultdict
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import bindparam, func, select, text

from app.models.query import Query
from app.models.step import Step
from app.models.visualization import Visualization
from app.ai.context.sections.queries_section import QueriesSection, QueryObservation, QueryVisualizationSummary

from app.settings.logging_config import get_logger


logger = get_logger(__name__)


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _column_names(columns: Any) -> List[str]:
    columns = _json_value(columns)
    if not isinstance(columns, list):
        return []
    names: List[str] = []
    for column in columns:
        if not isinstance(column, dict):
            continue
        name = column.get("field") or column.get("headerName")
        if name:
            names.append(str(name))
    return names


def _preview_table(column_names: List[str], rows: Any) -> Optional[str]:
    rows = _json_value(rows)
    if not column_names or not isinstance(rows, list) or not rows:
        return None
    try:
        lines = []
        header = " | ".join(column_names)
        lines.append(header)
        lines.append("-" * len(header))
        for row in rows[:5]:
            if isinstance(row, dict):
                lines.append(" | ".join(str(row.get(col, "N/A")) for col in column_names))
            else:
                lines.append(str(row))
        return "\n".join(lines)
    except Exception:
        return None


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
        queries = await self._get_report_queries(self.report.id, max_queries=max_queries)
        if not queries:
            return QueriesSection(items=items)

        query_ids = [q["id"] for q in queries]
        step_ids_by_query = {
            q["id"]: q.get("default_step_id")
            for q in queries
            if q.get("default_step_id")
        }
        missing_step_query_ids = [qid for qid in query_ids if qid not in step_ids_by_query]
        if missing_step_query_ids:
            step_ids_by_query.update(await self._get_latest_step_ids(missing_step_query_ids))

        step_ids = [sid for sid in step_ids_by_query.values() if sid]
        steps_by_id = await self._get_step_summaries(step_ids, include_data_preview=include_data_preview)
        visualizations_by_query = await self._get_visualizations(query_ids)

        for q in queries:
            obs = self._build_query_observation(
                q,
                steps_by_id.get(step_ids_by_query.get(q["id"])),
                visualizations_by_query.get(q["id"], []),
                include_data_preview=include_data_preview,
            )
            items.append(obs)
        return QueriesSection(items=items)

    async def _get_report_queries(self, report_id: str, max_queries: int) -> List[Dict[str, Any]]:
        try:
            res = await self.db.execute(
                select(Query.id, Query.title, Query.default_step_id)
                .where(Query.report_id == report_id)
                .order_by(Query.created_at.desc())
                .limit(max_queries)
            )
            rows = [
                {
                    "id": str(row.id),
                    "title": row.title or "",
                    "default_step_id": str(row.default_step_id) if row.default_step_id else None,
                }
                for row in res.all()
            ]
            rows.reverse()
            return rows
        except Exception as e:
            logger.error(f"Failed to load queries for report {report_id}: {e}")
            return []

    async def _get_latest_step_ids(self, query_ids: List[str]) -> Dict[str, str]:
        if not query_ids:
            return {}
        try:
            ranked_steps = (
                select(
                    Step.query_id.label("query_id"),
                    Step.id.label("step_id"),
                    func.row_number()
                    .over(partition_by=Step.query_id, order_by=Step.created_at.desc())
                    .label("rn"),
                )
                .where(Step.query_id.in_(query_ids))
                .subquery()
            )
            res = await self.db.execute(
                select(ranked_steps.c.query_id, ranked_steps.c.step_id)
                .where(ranked_steps.c.rn == 1)
            )
            return {str(row.query_id): str(row.step_id) for row in res.all() if row.step_id}
        except Exception as e:
            logger.error(f"Failed to load latest steps for queries: {e}")
            return {}

    def _is_postgres(self) -> bool:
        try:
            return self.db.get_bind().dialect.name == "postgresql"
        except Exception:
            return False

    async def _get_step_summaries(
        self,
        step_ids: List[str],
        include_data_preview: bool,
    ) -> Dict[str, Dict[str, Any]]:
        if not step_ids:
            return {}
        if self._is_postgres():
            return await self._get_step_summaries_postgres(step_ids, include_data_preview)
        return await self._get_step_summaries_fallback(step_ids, include_data_preview)

    async def _get_step_summaries_postgres(
        self,
        step_ids: List[str],
        include_data_preview: bool,
    ) -> Dict[str, Dict[str, Any]]:
        preview_sql = "NULL::json AS preview_rows"
        if include_data_preview:
            preview_sql = """
                (
                    SELECT json_agg(elem)
                    FROM (
                        SELECT elem
                        FROM json_array_elements(s.data->'rows') WITH ORDINALITY AS t(elem, ord)
                        WHERE ord <= :preview_limit
                    ) preview
                ) AS preview_rows
            """
        stmt = text(f"""
            SELECT
                s.id,
                s.title,
                s.query_id,
                s.data_model,
                s.view,
                s.data->'info' AS info,
                s.data->'columns' AS columns,
                {preview_sql}
            FROM steps s
            WHERE s.id IN :step_ids
        """).bindparams(bindparam("step_ids", expanding=True))
        try:
            res = await self.db.execute(stmt, {"step_ids": step_ids, "preview_limit": 5})
            return {
                str(row.id): {
                    "id": str(row.id),
                    "title": row.title or "",
                    "query_id": str(row.query_id) if row.query_id else None,
                    "data_model": _json_value(row.data_model),
                    "view": _json_value(row.view),
                    "info": _json_value(row.info) or {},
                    "columns": _json_value(row.columns) or [],
                    "preview_rows": _json_value(row.preview_rows) or [],
                }
                for row in res.all()
            }
        except Exception as e:
            logger.error(f"Failed to load projected step summaries: {e}")
            return await self._get_step_summaries_fallback(step_ids, include_data_preview)

    async def _get_step_summaries_fallback(
        self,
        step_ids: List[str],
        include_data_preview: bool,
    ) -> Dict[str, Dict[str, Any]]:
        try:
            res = await self.db.execute(select(Step).where(Step.id.in_(step_ids)))
            summaries: Dict[str, Dict[str, Any]] = {}
            for step in res.scalars().all():
                data = step.data if isinstance(step.data, dict) else {}
                rows = data.get("rows") if isinstance(data.get("rows"), list) else []
                summaries[str(step.id)] = {
                    "id": str(step.id),
                    "title": step.title or "",
                    "query_id": str(step.query_id) if step.query_id else None,
                    "data_model": step.data_model if isinstance(step.data_model, dict) else None,
                    "view": step.view if isinstance(step.view, dict) else None,
                    "info": data.get("info") if isinstance(data.get("info"), dict) else {},
                    "columns": data.get("columns") if isinstance(data.get("columns"), list) else [],
                    "preview_rows": rows[:5] if include_data_preview else [],
                }
            return summaries
        except Exception as e:
            logger.error(f"Failed to load step summaries: {e}")
            return {}

    async def _get_visualizations(self, query_ids: List[str]) -> Dict[str, List[QueryVisualizationSummary]]:
        visualizations: Dict[str, List[QueryVisualizationSummary]] = defaultdict(list)
        if not query_ids:
            return visualizations
        try:
            res = await self.db.execute(
                select(
                    Visualization.query_id,
                    Visualization.id,
                    Visualization.title,
                    Visualization.status,
                    Visualization.view,
                )
                .where(Visualization.query_id.in_(query_ids))
            )
            for row in res.all():
                visualizations[str(row.query_id)].append(QueryVisualizationSummary(
                    id=str(row.id),
                    title=row.title or "",
                    status=row.status,
                    view=_json_value(row.view) if isinstance(_json_value(row.view), dict) else None,
                ))
        except Exception as e:
            logger.error(f"Failed to load visualizations for queries: {e}")
        return visualizations

    def _build_query_observation(
        self,
        query: Dict[str, Any],
        default_step: Optional[Dict[str, Any]],
        visualizations: List[QueryVisualizationSummary],
        include_data_preview: bool,
    ) -> QueryObservation:
        obs = QueryObservation(
            query_id=query["id"],
            query_title=query.get("title") or "",
            default_step_id=default_step.get("id") if default_step else None,
            default_step_title=default_step.get("title") if default_step else None,
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
                data_model = default_step.get("data_model")
                if isinstance(data_model, dict):
                    obs.data_model = data_model
                info = default_step.get("info")
                if isinstance(info, dict):
                    obs.stats = info
                    try:
                        obs.row_count = int(info.get("total_rows") or 0)
                    except Exception:
                        obs.row_count = 0
                obs.column_names = _column_names(default_step.get("columns"))
                preview_rows = default_step.get("preview_rows") or []
                if not obs.row_count and isinstance(preview_rows, list):
                    obs.row_count = len(preview_rows)
                if include_data_preview:
                    obs.data_preview = _preview_table(obs.column_names, preview_rows)
            except Exception:
                pass

        return obs

