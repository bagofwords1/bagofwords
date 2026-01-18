"""MCP Tool: create_data - Generate data visualizations with Query/Step/Visualization persistence."""

import asyncio
from typing import Dict, Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.ai.tools.mcp.context import build_rich_context
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenRequest
from app.ai.prompt_formatters import build_codegen_context
from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.services.report_service import ReportService
from app.project_manager import ProjectManager
from app.schemas.mcp import MCPCreateDataInput, MCPCreateDataOutput
from app.dependencies import async_session_maker


class CreateDataMCPTool(MCPTool):
    """Generate data and create a tracked, reproducible visualization.
    
    Creates Query + Step + Visualization records that persist in the report.
    Use this for final results that should be saved and shared.
    Tables are auto-discovered from the prompt if not explicitly provided.
    """
    
    name = "create_data"
    description = (
        "Create a tracked, reproducible data query with visualization (chart or table). "
        "Results are persisted in the report and can be viewed, shared, and added to dashboards. "
        "Use this for final results you want to save. "
        "Tables are auto-discovered from prompt if not provided. "
        "Call create_report first if no report_id is available."
    )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPCreateDataInput.model_json_schema()
    
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Execute create_data with full artifact creation."""
        
        input_data = MCPCreateDataInput(**args)
        
        report_service = ReportService()
        project_manager = ProjectManager()
        
        # Get or create MCP platform first (for external_platform_id)
        platform = await self._get_or_create_mcp_platform(db, organization)
        
        # Load report (report_id is now required)
        report = await report_service.get_report(db, input_data.report_id, user, organization)
        
        # Update report with external_platform_id if not set (direct DB update)
        if not report.external_platform:
            await db.execute(
                update(Report)
                .where(Report.id == str(report.id))
                .values(external_platform_id=str(platform.id))
            )
            await db.flush()
        
        # Create tracking context
        tracking = await self._create_tracking_context(
            db, user, organization, report, self.name, args
        )
        
        # Build rich context (shared context preparation)
        rich_ctx = await build_rich_context(
            db=db,
            user=user,
            organization=organization,
            report=report,
            prompt=input_data.prompt,
            explicit_tables=input_data.tables,
        )
        
        # Check if we have a model
        if not rich_ctx.model:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No default LLM model configured for this organization."
            )
            return MCPCreateDataOutput(
                report_id=str(report.id),
                success=False,
                error_message="No default LLM model configured for this organization.",
            ).model_dump()
        
        # Check if we have connected data sources
        if not rich_ctx.ds_clients:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No data sources could be connected."
            )
            return MCPCreateDataOutput(
                report_id=str(report.id),
                success=False,
                error_message="No data sources could be connected.",
            ).model_dump()
        
        # Build codegen context using the rich context
        runtime_ctx = {
            "settings": rich_ctx.org_settings,
            "context_hub": rich_ctx.context_hub,
            "ds_clients": rich_ctx.ds_clients,
            "excel_files": [],
            "context_view": rich_ctx.context_hub.get_view(),
        }
        
        codegen_context = await build_codegen_context(
            runtime_ctx=runtime_ctx,
            user_prompt=input_data.prompt,
            interpreted_prompt=input_data.prompt,
            schemas_excerpt=rich_ctx.schemas_excerpt,
            tables_by_source=rich_ctx.tables_by_source,
        )
        
        # Setup Coder and Executor
        coder = Coder(
            model=rich_ctx.model,
            organization_settings=rich_ctx.org_settings,
            context_hub=rich_ctx.context_hub,
            usage_session_maker=async_session_maker,
        )
        
        streamer = StreamingCodeExecutor(
            organization_settings=rich_ctx.org_settings,
            logger=None,
            context_hub=rich_ctx.context_hub,
        )

        # Execute code generation
        output_log = ""
        generated_code = ""
        exec_df = None
        code_errors = []

        sigkill_event = asyncio.Event()

        async for e in streamer.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=codegen_context, retries=2),
            ds_clients=rich_ctx.ds_clients,
            excel_files=[],
            code_generator_fn=coder.generate_code,
            sigkill_event=sigkill_event,
        ):
            if e["type"] == "stdout":
                payload = e["payload"]
                if isinstance(payload, str):
                    output_log += payload + "\n"
                else:
                    output_log += (payload.get("message") or "") + "\n"
            elif e["type"] == "done":
                generated_code = e["payload"].get("code") or ""
                code_errors = e["payload"].get("errors") or []
                exec_df = e["payload"].get("df")
                full_log = e["payload"].get("execution_log")
                if full_log and len(full_log) > len(output_log):
                    output_log = full_log
        
        # Check for execution failure
        if generated_code is None or exec_df is None:
            error_msg = "Code execution failed"
            if code_errors:
                error_msg = str(code_errors[-1][1] if code_errors else "Unknown error")[:500]
            
            await self._finish_tracking(
                db, tracking, success=False,
                summary=f"Create data failed: {error_msg}"
            )
            return MCPCreateDataOutput(
                report_id=str(report.id),
                success=False,
                error_message=error_msg,
            ).model_dump()
        
        # Format data for widget
        formatted = streamer.format_df_for_widget(exec_df)
        
        # Determine title
        title = input_data.title or f"Query: {input_data.prompt[:50]}"
        
        # Always use table view - chart types require series data we don't have here
        from app.schemas.view_schema import TableView, ViewSchema as VS
        data_model = {"type": "table", "series": []}
        view = TableView(title=title)
        view_payload = VS(view=view).model_dump(exclude_none=True)
        
        # Create Query (pass org/user IDs since report is a schema, not ORM model)
        query = await project_manager.create_query_v2(
            db, report, title, 
            organization_id=str(organization.id), 
            user_id=str(user.id)
        )
        
        # Create Step
        step = await project_manager.create_step_for_query(
            db, query, title, "chart", data_model
        )
        await project_manager.set_query_default_step_if_empty(db, query, str(step.id))
        
        # Update step with code and data
        await project_manager.update_step_with_code(db, step, generated_code)
        await project_manager.update_step_with_data(db, step, formatted)
        await project_manager.update_step_with_data_model(db, step, data_model)
        await project_manager.update_step_status(db, step, "success")
        
        # Create Visualization
        visualization = await project_manager.create_visualization_v2(
            db, 
            str(report.id), 
            str(query.id), 
            title, 
            view=view_payload,
            status="success"
        )
        
        # Build data preview (limited rows)
        data_preview = {
            "columns": formatted.get("columns", []),
            "rows": formatted.get("rows", [])[:20],
            "total_rows": formatted.get("info", {}).get("total_rows", len(formatted.get("rows", []))),
        }
        
        # Finish tracking
        await self._finish_tracking(
            db, tracking, success=True,
            summary=f"Created visualization '{title}' with {data_preview['total_rows']} rows",
            result_json={"query_id": str(query.id), "visualization_id": str(visualization.id)},
            created_step_id=str(step.id),
            created_visualization_ids=[str(visualization.id)],
        )
        
        from app.settings.config import settings
        base_url = settings.bow_config.base_url
        
        output = MCPCreateDataOutput(
            report_id=str(report.id),
            query_id=str(query.id),
            visualization_id=str(visualization.id),
            success=True,
            data_preview=data_preview,
            url=f"{base_url}/reports/{report.id}",
        )
        
        return output.model_dump()
