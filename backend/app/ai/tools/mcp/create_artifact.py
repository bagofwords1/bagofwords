"""MCP Tool: create_artifact - Generate dashboards/slides from visualizations."""

import logging
from typing import Dict, Any, List

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.tools.mcp.base import MCPTool
from app.ai.tools.mcp.context import build_rich_context
from app.ai.llm import LLM
from app.models.user import User
from app.models.organization import Organization
from app.models.artifact import Artifact
from app.models.visualization import Visualization
from app.models.query import Query
from app.services.report_service import ReportService
from app.schemas.mcp import MCPCreateArtifactInput, MCPCreateArtifactOutput
from app.dependencies import async_session_maker

logger = logging.getLogger(__name__)

# Maximum visualizations to include in artifact
MAX_VISUALIZATIONS = 10


class CreateArtifactMCPTool(MCPTool):
    """Create a dashboard or slide presentation from report visualizations.

    Automatically selects all successful visualizations in the report (up to 10).
    Use this after creating visualizations with create_data to compose them into
    a polished layout with KPI cards, charts, and responsive grids.
    """

    name = "create_artifact"
    description = (
        "Create a dashboard or slide presentation from existing visualizations. "
        "Automatically uses all successful visualizations in the report (up to 10). "
        "Supports 'page' mode for interactive dashboards or 'slides' mode for presentations. "
        "Call create_data first to generate visualizations, then use this to compose them."
    )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPCreateArtifactInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute create_artifact with auto-selected visualizations."""

        input_data = MCPCreateArtifactInput(**args)

        # Validate mode
        if input_data.mode not in ("page", "slides"):
            return MCPCreateArtifactOutput(
                report_id=input_data.report_id,
                success=False,
                error_message=f"Invalid mode '{input_data.mode}'. Must be 'page' or 'slides'.",
            ).model_dump()

        report_service = ReportService()

        # Load report
        try:
            report = await report_service.get_report(db, input_data.report_id, user, organization)
        except Exception as e:
            return MCPCreateArtifactOutput(
                report_id=input_data.report_id,
                success=False,
                error_message=f"Report not found: {str(e)}",
            ).model_dump()

        # Create tracking context
        tracking = await self._create_tracking_context(
            db, user, organization, report, self.name, args
        )

        # Build rich context
        rich_ctx = await build_rich_context(
            db=db,
            user=user,
            organization=organization,
            report=report,
            prompt=input_data.prompt,
        )

        # Check if we have a model
        if not rich_ctx.model:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No default LLM model configured for this organization."
            )
            return MCPCreateArtifactOutput(
                report_id=str(report.id),
                success=False,
                error_message="No default LLM model configured for this organization.",
            ).model_dump()

        # Fetch all successful visualizations for this report
        visualizations, warnings = await self._fetch_successful_visualizations(
            db, str(report.id), rich_ctx.context_hub, limit=MAX_VISUALIZATIONS
        )

        if not visualizations:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No successful visualizations found in this report."
            )
            return MCPCreateArtifactOutput(
                report_id=str(report.id),
                success=False,
                error_message="No successful visualizations found. Create visualizations using create_data first.",
            ).model_dump()

        # Get organization settings for privacy check
        allow_llm_see_data = True
        try:
            allow_llm_see_data = rich_ctx.org_settings.get_config("allow_llm_see_data").value
        except Exception:
            allow_llm_see_data = True

        # Import the existing tool to reuse its methods
        from app.ai.tools.implementations.create_artifact import CreateArtifactTool
        artifact_tool = CreateArtifactTool()

        # Build visualization profiles
        viz_profiles = [artifact_tool._build_viz_profile(v, allow_llm_see_data) for v in visualizations]

        # Build instructions context
        instructions_context = rich_ctx.instructions_text or ""

        # Build the prompt with selection guidance
        prompt = self._build_prompt_with_selection(
            artifact_tool=artifact_tool,
            user_prompt=input_data.prompt,
            title=input_data.title,
            mode=input_data.mode,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            report_title=getattr(report, 'title', None),
            allow_llm_see_data=allow_llm_see_data,
        )

        # LLM inference (non-streaming for MCP)
        llm = LLM(rich_ctx.model, usage_session_maker=async_session_maker)
        try:
            response = await llm.inference(
                prompt,
                usage_scope="mcp_create_artifact",
                usage_scope_ref_id=str(report.id),
            )
        except Exception as e:
            logger.exception("LLM inference failed for create_artifact")
            await self._finish_tracking(
                db, tracking, success=False,
                summary=f"Code generation failed: {str(e)}"
            )
            return MCPCreateArtifactOutput(
                report_id=str(report.id),
                success=False,
                error_message=f"Code generation failed: {str(e)}",
            ).model_dump()

        # Extract code from response
        code = artifact_tool._extract_code(response, mode=input_data.mode)

        # Build visualization IDs list
        included_viz_ids = [v["id"] for v in visualizations]

        # Create Artifact record
        artifact = Artifact(
            report_id=str(report.id),
            user_id=str(user.id),
            organization_id=str(organization.id),
            title=input_data.title or "Dashboard",
            mode=input_data.mode,
            content={"code": code, "visualization_ids": included_viz_ids},
            generation_prompt=input_data.prompt,
            version=1,
            status="completed",
        )
        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)

        # Finish tracking
        await self._finish_tracking(
            db, tracking, success=True,
            summary=f"Created {input_data.mode} artifact '{input_data.title or 'Dashboard'}' with {len(visualizations)} visualizations",
            result_json={"artifact_id": str(artifact.id)},
            created_visualization_ids=included_viz_ids,
        )

        # Build URL
        from app.settings.config import settings
        base_url = settings.bow_config.base_url
        url = f"{base_url}/reports/{report.id}?artifact={artifact.id}"

        return MCPCreateArtifactOutput(
            report_id=str(report.id),
            artifact_id=str(artifact.id),
            success=True,
            visualization_count=len(visualizations),
            visualization_ids=included_viz_ids,
            mode=input_data.mode,
            url=url,
        ).model_dump()

    async def _fetch_successful_visualizations(
        self,
        db: AsyncSession,
        report_id: str,
        context_hub,
        limit: int = MAX_VISUALIZATIONS,
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """Fetch all successful visualizations for a report.

        Returns:
            Tuple of (visualizations list, warnings list)
        """
        visualizations: List[Dict[str, Any]] = []
        warnings: List[str] = []

        # Build query data lookup from context_hub for enrichment
        query_data_lookup: Dict[str, Dict[str, Any]] = {}
        try:
            if context_hub is not None:
                view = context_hub.get_view()
                qsec = getattr(getattr(view, 'warm', None), 'queries', None)
                items = getattr(qsec, 'items', []) if qsec else []
                for it in (items or []):
                    query_id = getattr(it, 'query_id', None)
                    if query_id:
                        query_data_lookup[str(query_id)] = {
                            "columns": list(getattr(it, 'column_names', []) or []),
                            "row_count": getattr(it, 'row_count', 0),
                            "rows": list(getattr(it, 'rows', []) or [])[:100],
                            "dataModel": getattr(it, 'data_model', None) or {},
                        }
        except Exception:
            pass

        # Fetch visualizations with eager loading of query and steps
        try:
            result = await db.execute(
                select(Visualization)
                .options(
                    selectinload(Visualization.query).selectinload(Query.default_step),
                    selectinload(Visualization.query).selectinload(Query.steps),
                )
                .where(Visualization.report_id == report_id)
                .order_by(desc(Visualization.created_at))
                .limit(limit * 2)  # Fetch more to account for filtering
            )
            all_vizs = result.scalars().all()
        except Exception as e:
            warnings.append(f"Failed to fetch visualizations: {str(e)}")
            return visualizations, warnings

        # Filter to successful visualizations and build entries
        for viz in all_vizs:
            if len(visualizations) >= limit:
                break

            # Check step status
            step_status = None
            if viz.query and viz.query.default_step:
                step_status = viz.query.default_step.status
            elif viz.query and viz.query.steps:
                step_status = viz.query.steps[-1].status if viz.query.steps else None

            if step_status != "success":
                continue

            # Build visualization entry
            view_dict = viz.view or {}
            query_id = str(viz.query_id) if viz.query_id else None
            query_data = query_data_lookup.get(query_id, {}) if query_id else {}

            ventry = {
                "id": str(viz.id),
                "title": viz.title,
                "query_id": query_id,
                "view": view_dict,
                "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                "columns": query_data.get("columns", []),
                "row_count": query_data.get("row_count", 0),
                "rows": query_data.get("rows", []),
                "dataModel": query_data.get("dataModel", {}),
            }
            visualizations.append(ventry)

        return visualizations, warnings

    def _build_prompt_with_selection(
        self,
        artifact_tool,
        user_prompt: str,
        title: str | None,
        mode: str,
        viz_profiles: List[Dict[str, Any]],
        instructions_context: str,
        report_title: str | None,
        allow_llm_see_data: bool,
    ) -> str:
        """Build prompt with selection guidance for auto-selected visualizations."""

        # Get the base prompt from the existing tool
        base_prompt = artifact_tool._build_prompt(
            user_prompt=user_prompt,
            title=title,
            mode=mode,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            report_title=report_title,
            allow_llm_see_data=allow_llm_see_data,
            messages_context="",
            previous_artifacts=None,
        )

        # Add selection guidance at the beginning of the design request section
        selection_guidance = f"""
**IMPORTANT - Visualization Selection:**
You have access to {len(viz_profiles)} visualizations. You do NOT need to include all of them.
Select and include ONLY those that are relevant to the user's request and create a cohesive narrative.
Focus on quality over quantity - a focused dashboard with 3-4 well-chosen visualizations is better
than a cluttered one with all {len(viz_profiles)}.
"""

        # Insert the guidance after "DESIGN REQUEST" section header
        marker = "═══════════════════════════════════════════════════════════════════════════════\nDESIGN REQUEST"
        if marker in base_prompt:
            base_prompt = base_prompt.replace(
                marker,
                marker + "\n" + selection_guidance
            )

        return base_prompt
