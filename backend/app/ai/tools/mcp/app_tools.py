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
from app.models.artifact import ArtifactVersion
from app.models.membership import Membership

logger = logging.getLogger(__name__)


async def _build_viewer_context(
    db: AsyncSession, user: Optional[User], organization: Optional[Organization]
) -> Optional[Dict[str, Any]]:
    """ARTIFACT_DATA.current_user for MCP app rendering — same shape as
    GET /users/me/viewer_context. None when there is no authenticated user."""
    if user is None:
        return None
    membership = None
    if organization is not None:
        result = await db.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization.id,
            )
        )
        membership = result.scalars().first()
    groups: List[str] = []
    if organization is not None:
        from app.routes.user_profile import _resolve_viewer_groups
        groups = await _resolve_viewer_groups(db, str(user.id), str(organization.id))
    return {
        "id": str(user.id),
        "name": user.name,
        "email": user.email,
        "image_url": getattr(user, "image_url", None),
        "role": membership.role if membership else None,
        "profile_attributes": (membership.profile_attributes if membership else None) or None,
        "groups": groups,
    }


async def _open_release(db: AsyncSession, ref: str, user: User, organization: Organization):
    """(dashboard, live release) when `user` may open it in this organization
    -- the /d rules (phase 4) -- else None."""
    from app.errors import AppError
    from app.services.dashboard_access import open_published

    try:
        artifact, release = await open_published(db, ref, user)
    except AppError:
        return None
    if str(artifact.organization_id) != str(organization.id):
        return None
    return artifact, release


async def _release_chart(db: AsyncSession, viz: Visualization, artifact, release, user: User) -> Dict[str, Any]:
    """One chart of a release, with the rows this reader may see."""
    from app.services.dashboard_data import default_step, resolve_release_step

    query = await db.get(Query, viz.query_id) if viz.query_id else None
    step = await default_step(db, query) if query is not None else None
    resolution = await resolve_release_step(db, step, artifact, release, user) if step is not None else None
    data = resolution.data if resolution is not None else {}
    withheld = resolution is not None and resolution.withheld
    # Code follows the reader's role, as on /d.
    from app.core.code_visibility import can_view_code
    from app.core.permission_resolver import resolve_permissions

    code_visible = can_view_code(await resolve_permissions(db, str(user.id), str(artifact.organization_id)))
    return {"step": step, "query": query, "rows": (data or {}).get("rows", []),
            "columns": (data or {}).get("columns", []), "hide_code": withheld or not code_visible}


class GetVisualizationMCPTool(MCPTool):
    """Fetch full visualization data including query results and code.

    App-only tool used by the MCP visualization UI to render charts/tables.
    """

    name = "get_visualization"
    read_only_hint = True
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
            .execution_options(populate_existing=True)
        )
        viz = result.scalar_one_or_none()

        if viz is not None and viz.artifact_release_id:
            # A published dashboard's chart: only its live release's, for who
            # may open that dashboard (phase 4).
            from app.models.artifact import Artifact
            artifact_id = (await db.execute(
                select(Artifact.id).where(Artifact.published_release_id == str(viz.artifact_release_id),
                                          Artifact.deleted_at.is_(None))
            )).scalar_one_or_none()
            opened = await _open_release(db, str(artifact_id), user, organization) if artifact_id else None
            if opened is None or viz.deleted_at is not None:
                return {"error": f"Visualization {visualization_id} not found"}
            chart = await _release_chart(db, viz, *opened, user)
            step = chart["step"]
            return {
                "id": str(viz.id),
                "title": viz.title or "",
                "view": viz.view or {},
                "code": "" if chart["hide_code"] or step is None else step.code,
                "data": {"rows": chart["rows"], "columns": chart["columns"]},
                "data_model": step.data_model if step else {},
                "step_status": step.status if step else None,
            }

        # Same not-found answer for a foreign id as for a missing one.
        if not viz or not await self._assert_can_view_report(
            db, viz.report_id, user, organization
        ):
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
    read_only_hint = True
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
                    "description": "The artifact (version) ID to fetch data for.",
                },
                "dashboard_id": {
                    "type": "string",
                    "description": "A published dashboard's id or name: its live release is fetched instead.",
                },
            },
        }

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        dashboard_id = args.get("dashboard_id")
        if dashboard_id:
            return await self._published(db, str(dashboard_id), user, organization)
        artifact_id = args.get("artifact_id")
        if not artifact_id:
            return {"error": "artifact_id is required"}

        # Fetch artifact with report relationship
        result = await db.execute(
            select(ArtifactVersion)
            .options(selectinload(ArtifactVersion.report))
            .where(ArtifactVersion.id == artifact_id)
        )
        artifact = result.scalar_one_or_none()

        if not artifact or not await self._assert_can_view_report(
            db, artifact.report_id, user, organization
        ):
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
            .execution_options(populate_existing=True)
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
            "runtime": {"version": int(content.get("runtime_version") or 0)},
            "code": code,
            "mode": artifact.mode or "page",
            "visualizations": visualizations,
            "current_user": await _build_viewer_context(db, user, organization),
        }

    async def _published(self, db: AsyncSession, ref: str, user: User, organization: Organization):
        """The live release of a published dashboard, as /d serves it."""
        opened = await _open_release(db, ref, user, organization)
        if opened is None:
            return {"error": f"Dashboard {ref} not found"}
        artifact, release = opened
        content = release.content or {}
        viz_ids = [str(v) for v in content.get("visualization_ids") or []]
        charts = (await db.execute(
            select(Visualization).where(Visualization.artifact_release_id == str(release.id),
                                        Visualization.deleted_at.is_(None))
        )).scalars().all()
        order = {vid: i for i, vid in enumerate(viz_ids)}
        charts = sorted((v for v in charts if not viz_ids or str(v.id) in order),
                        key=lambda v: order.get(str(v.id), len(order)))
        visualizations: List[Dict[str, Any]] = []
        for viz in charts:
            chart = await _release_chart(db, viz, artifact, release, user)
            step = chart["step"]
            visualizations.append({
                "id": str(viz.id),
                "title": viz.title or (chart["query"].title if chart["query"] else None) or "Untitled",
                "view": viz.view or {},
                "rows": chart["rows"],
                "columns": chart["columns"],
                "dataModel": step.data_model or {} if step else {},
                "stepStatus": step.status if step else None,
            })
        return {
            "report": {"id": str(artifact.id), "title": artifact.title or ""},
            "runtime": {"version": int(content.get("runtime_version") or 0)},
            "code": content.get("code", ""),
            "mode": artifact.mode or "page",
            "visualizations": visualizations,
            "current_user": await _build_viewer_context(db, user, organization),
        }
