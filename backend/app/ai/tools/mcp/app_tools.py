"""MCP App-only tools - Data fetching tools for MCP Apps UI rendering.

These tools are hidden from the LLM (visibility=["app"]) and are called
by the MCP App HTML bundles running in the client's iframe to fetch
visualization and artifact data from the server.
"""

import logging
from typing import Dict, Any, List, Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.tools.mcp.base import MCPTool
from app.models.user import User
from app.models.organization import Organization
from app.models.visualization import Visualization
from app.models.query import Query
from app.models.step import Step
from app.models.artifact import Artifact

logger = logging.getLogger(__name__)


class GetVisualizationMCPTool(MCPTool):
    """Fetch full visualization data including query results and code.

    App-only tool used by the MCP visualization UI to render charts/tables.
    """

    name = "get_visualization"
    description = "Fetch visualization data including chart config, rows, columns, and code."

    @property
    def visibility(self) -> List[str]:
        return ["app"]

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "visualization_id": {
                    "type": "string",
                    "description": "The visualization ID to fetch data for.",
                },
            },
            "required": ["visualization_id"],
        }

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        visualization_id = args.get("visualization_id")
        if not visualization_id:
            return {"error": "visualization_id is required"}

        # Fetch visualization with query and default step eagerly loaded
        result = await db.execute(
            select(Visualization)
            .options(
                selectinload(Visualization.query).selectinload(Query.default_step),
                selectinload(Visualization.query).selectinload(Query.steps),
            )
            .where(Visualization.id == visualization_id)
        )
        viz = result.scalar_one_or_none()

        if not viz:
            return {"error": f"Visualization {visualization_id} not found"}

        # Get the step (prefer default_step, fallback to latest)
        step = None
        if viz.query:
            step = viz.query.default_step
            if not step and viz.query.steps:
                step = viz.query.steps[-1]

        # Build response matching ToolWidgetPreview data shape
        return {
            "id": str(viz.id),
            "title": viz.title or "",
            "view": viz.view or {},
            "code": step.code if step else "",
            "data": {
                "rows": (step.data or {}).get("rows", []) if step else [],
                "columns": (step.data or {}).get("columns", []) if step else [],
            },
            "data_model": step.data_model if step else {},
            "step_status": step.status if step else None,
        }


class GetArtifactDataMCPTool(MCPTool):
    """Fetch artifact code and all visualization data for rendering.

    App-only tool used by the MCP artifact UI to render full dashboards/slides.
    Returns the same data shape that ArtifactFrame.vue sends via ARTIFACT_DATA.
    """

    name = "get_artifact_data"
    description = "Fetch artifact code and visualization data for rendering a dashboard or presentation."

    @property
    def visibility(self) -> List[str]:
        return ["app"]

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "The artifact ID to fetch data for.",
                },
            },
            "required": ["artifact_id"],
        }

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        artifact_id = args.get("artifact_id")
        if not artifact_id:
            return {"error": "artifact_id is required"}

        # Fetch artifact with report relationship
        result = await db.execute(
            select(Artifact)
            .options(selectinload(Artifact.report))
            .where(Artifact.id == artifact_id)
        )
        artifact = result.scalar_one_or_none()

        if not artifact:
            return {"error": f"Artifact {artifact_id} not found"}

        report = artifact.report
        content = artifact.content or {}
        code = content.get("code", "")
        viz_ids = content.get("visualization_ids", [])

        # Fetch queries for this report, filtered by artifact's visualization_ids
        queries_result = await db.execute(
            select(Query)
            .options(
                selectinload(Query.default_step),
                selectinload(Query.steps),
                selectinload(Query.visualizations),
            )
            .where(Query.report_id == str(artifact.report_id))
        )
        queries = queries_result.scalars().all()

        # Build visualization data array (same shape as ArtifactFrame.vue)
        visualizations: List[Dict[str, Any]] = []
        for query in queries:
            step = query.default_step
            if not step and query.steps:
                step = query.steps[-1]

            for viz in (query.visualizations or []):
                # If artifact has viz_ids, only include those
                if viz_ids and str(viz.id) not in viz_ids:
                    continue

                visualizations.append({
                    "id": str(viz.id),
                    "title": viz.title or query.title or "Untitled",
                    "view": viz.view or {},
                    "rows": (step.data or {}).get("rows", []) if step else [],
                    "columns": (step.data or {}).get("columns", []) if step else [],
                    "dataModel": step.data_model or {} if step else {},
                    "stepStatus": step.status if step else None,
                })

        return {
            "report": {
                "id": str(report.id) if report else str(artifact.report_id),
                "title": report.title if report else "",
            },
            "code": code,
            "mode": artifact.mode or "page",
            "visualizations": visualizations,
        }
