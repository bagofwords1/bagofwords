"""MCP Tool: edit_artifact - Surgically edit an existing dashboard/artifact."""

import logging
from typing import Dict, Any, List, Optional

from sqlalchemy import select
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
from app.schemas.mcp import MCPEditArtifactInput, MCPEditArtifactOutput
from app.dependencies import async_session_maker

logger = logging.getLogger(__name__)


class EditArtifactMCPTool(MCPTool):
    """Surgically edit an existing dashboard or artifact.

    Loads the existing code and applies targeted search/replace diffs based on
    the user's instruction. Falls back to full rewrite if the diff cannot be applied.
    """

    name = "edit_artifact"
    description = (
        "Edit an existing dashboard or artifact by applying targeted changes. "
        "Preserves the existing design and only modifies what is requested. "
        "Use this instead of create_artifact when you want to modify an existing artifact. "
        "Requires artifact_id from a previous create_artifact result."
    )

    @property
    def meta(self) -> Optional[Dict[str, Any]]:
        return {"ui": {"resourceUri": "ui://bagofwords/artifact"}}

    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPEditArtifactInput.model_json_schema()

    async def execute(
        self,
        args: Dict[str, Any],
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute edit_artifact with diff-based editing."""

        input_data = MCPEditArtifactInput(**args)

        # Load report
        try:
            report = await self._load_report(db, input_data.report_id)
        except Exception as e:
            return MCPEditArtifactOutput(
                report_id=input_data.report_id,
                success=False,
                error_message=f"Report not found: {str(e)}",
            ).model_dump()

        # Load the existing artifact
        try:
            result = await db.execute(
                select(Artifact).where(
                    Artifact.id == input_data.artifact_id,
                    Artifact.organization_id == str(organization.id),
                )
            )
            artifact = result.scalar_one_or_none()
        except Exception as e:
            return MCPEditArtifactOutput(
                report_id=input_data.report_id,
                success=False,
                error_message=f"Failed to load artifact: {str(e)}",
            ).model_dump()

        if not artifact:
            return MCPEditArtifactOutput(
                report_id=input_data.report_id,
                success=False,
                error_message=f"Artifact {input_data.artifact_id} not found.",
            ).model_dump()

        # Extract existing code and viz IDs
        content = artifact.content or {}
        existing_code = content.get("code", "")
        existing_viz_ids = content.get("visualization_ids", [])

        if not existing_code:
            return MCPEditArtifactOutput(
                report_id=input_data.report_id,
                artifact_id=str(artifact.id),
                success=False,
                error_message="Artifact has no code to edit.",
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
            prompt=input_data.edit_instruction,
        )

        if not rich_ctx.model:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No default LLM model configured for this organization."
            )
            return MCPEditArtifactOutput(
                report_id=str(report.id),
                artifact_id=str(artifact.id),
                success=False,
                error_message="No default LLM model configured for this organization.",
            ).model_dump()

        # Merge viz IDs
        merged_viz_ids = list(existing_viz_ids)
        if input_data.visualization_ids:
            for vid in input_data.visualization_ids:
                if vid not in merged_viz_ids:
                    merged_viz_ids.append(vid)

        # Fetch visualizations
        visualizations: List[Dict[str, Any]] = []
        try:
            viz_result = await db.execute(
                select(Visualization)
                .options(
                    selectinload(Visualization.query).selectinload(Query.default_step),
                    selectinload(Visualization.query).selectinload(Query.steps),
                )
                .where(Visualization.id.in_(merged_viz_ids))
                .execution_options(populate_existing=True)
            )
            fetched_vizs = {str(v.id): v for v in viz_result.scalars().all()}
        except Exception as e:
            logger.exception("Failed to fetch visualizations for edit_artifact MCP")
            fetched_vizs = {}

        for viz_id in merged_viz_ids:
            viz = fetched_vizs.get(viz_id)
            if not viz:
                continue

            step = None
            if viz.query and viz.query.default_step:
                step = viz.query.default_step
            elif viz.query and viz.query.steps:
                step = viz.query.steps[-1] if viz.query.steps else None

            if not step or step.status != "success":
                continue

            step_data = step.data if step else {}
            rows = (step_data.get("rows") or [])[:100] if step_data else []
            raw_columns = step_data.get("columns") or [] if step_data else []
            data_model = step.data_model if step else {}

            view_dict = viz.view or {}
            ventry = {
                "id": str(viz.id),
                "title": viz.title,
                "query_id": str(viz.query_id) if viz.query_id else None,
                "view": view_dict,
                "data_model_type": (view_dict.get("view") or {}).get("type") or view_dict.get("type"),
                "columns": raw_columns,
                "row_count": len(rows),
                "rows": rows,
                "dataModel": data_model or {},
            }
            visualizations.append(ventry)

        # Import shared tools
        from app.ai.tools.implementations.create_artifact import CreateArtifactTool
        from app.ai.tools.implementations.edit_artifact import EditArtifactTool, apply_search_replace_diff

        artifact_tool = CreateArtifactTool()
        edit_tool = EditArtifactTool()

        # Get privacy setting
        allow_llm_see_data = True
        try:
            allow_llm_see_data = rich_ctx.org_settings.get_config("allow_llm_see_data").value
        except Exception:
            allow_llm_see_data = True

        # Build viz profiles (truncated for edit)
        viz_profiles = [artifact_tool._build_viz_profile(v, allow_llm_see_data) for v in visualizations]
        for profile in viz_profiles:
            if "sample_rows" in profile:
                profile["sample_rows"] = profile["sample_rows"][:3]

        # Build edit prompt
        instructions_context = rich_ctx.instructions_text or ""
        prompt = edit_tool._build_edit_prompt(
            existing_code=existing_code,
            edit_instruction=input_data.edit_instruction,
            mode=artifact.mode,
            viz_profiles=viz_profiles,
            instructions_context=instructions_context,
            report_title=getattr(report, 'title', None),
        )

        # LLM inference (non-streaming for MCP)
        llm = LLM(rich_ctx.model, usage_session_maker=async_session_maker)
        try:
            response = llm.inference(
                prompt,
                usage_scope="mcp_edit_artifact",
                usage_scope_ref_id=str(report.id),
            )
        except Exception as e:
            logger.exception("LLM inference failed for edit_artifact MCP")
            await self._finish_tracking(
                db, tracking, success=False,
                summary=f"Code edit generation failed: {str(e)}"
            )
            return MCPEditArtifactOutput(
                report_id=str(report.id),
                artifact_id=str(artifact.id),
                success=False,
                error_message=f"Code edit generation failed: {str(e)}",
            ).model_dump()

        # Apply the diff
        new_code, diff_applied, num_blocks = apply_search_replace_diff(existing_code, response)

        if num_blocks == 0:
            # No diff blocks — try full rewrite fallback
            extracted = artifact_tool._extract_code(response, mode=artifact.mode)
            if extracted and extracted != response.strip():
                new_code = extracted
                diff_applied = False
            else:
                new_code = existing_code
                diff_applied = False
        elif not diff_applied:
            extracted = artifact_tool._extract_code(response, mode=artifact.mode)
            if extracted and extracted != response.strip():
                new_code = extracted

        # Create a NEW artifact record (preserves version history for frontend dropdown)
        new_title = input_data.title or artifact.title
        included_viz_ids = [v["id"] for v in visualizations]
        new_version = artifact.version + 1

        new_artifact = Artifact(
            report_id=artifact.report_id,
            user_id=str(user.id),
            organization_id=str(organization.id),
            title=new_title,
            mode=artifact.mode,
            content={"code": new_code, "visualization_ids": included_viz_ids},
            generation_prompt=input_data.edit_instruction,
            version=new_version,
            status="completed",
        )
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)

        # Finish tracking
        summary = f"Edited artifact '{new_title}' (v{new_version})"
        if diff_applied:
            summary += f" — applied {num_blocks} surgical edit(s)"
        else:
            summary += " — fell back to full rewrite"

        await self._finish_tracking(
            db, tracking, success=True,
            summary=summary,
            result_json={"artifact_id": str(new_artifact.id), "version": new_version},
            created_visualization_ids=included_viz_ids,
        )

        # Build URL
        from app.settings.config import settings
        base_url = settings.bow_config.base_url
        url = f"{base_url}/reports/{report.id}?artifact={new_artifact.id}"

        return MCPEditArtifactOutput(
            report_id=str(report.id),
            artifact_id=str(new_artifact.id),
            success=True,
            version=new_version,
            diff_applied=diff_applied,
            url=url,
        ).model_dump()
