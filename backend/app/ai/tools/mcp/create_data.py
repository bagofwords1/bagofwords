"""MCP Tool: create_data - Generate data visualizations with Query/Step/Visualization persistence."""

import re
import asyncio
from typing import Dict, Any, List, Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.ai.context import ContextHub
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenRequest
from app.ai.prompt_formatters import build_codegen_context
from app.ai.tools.implementations.create_data import build_view_from_data_model
from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.services.report_service import ReportService
from app.services.data_source_service import DataSourceService
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
    
    async def _discover_tables(
        self,
        schema_builder,
        prompt: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Auto-discover relevant tables from user prompt using keyword extraction."""
        tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]{3,}", prompt)]
        keywords = list(dict.fromkeys(tokens))[:5]
        name_patterns = [f"(?i){re.escape(k)}" for k in keywords] if keywords else None
        
        ctx = await schema_builder.build(
            with_stats=True,
            name_patterns=name_patterns,
            top_k=top_k,
        )
        
        tables_by_source = []
        for ds in ctx.data_sources:
            if ds.tables:
                tables_by_source.append({
                    "data_source_id": str(ds.info.id),
                    "tables": [t.name for t in ds.tables]
                })
        return tables_by_source
    
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
        ds_service = DataSourceService()
        project_manager = ProjectManager()
        
        # Get or create MCP platform first (for external_platform_id)
        platform = await self._get_or_create_mcp_platform(db, organization)
        
        # Load report (report_id is now required)
        report = await report_service.get_report(db, input_data.report_id, user, organization)
        data_sources = report.data_sources
        
        # Update report with external_platform_id if not set (direct DB update)
        if not report.external_platform:
            await db.execute(
                update(Report)
                .where(Report.id == str(report.id))
                .values(external_platform_id=str(platform.id))
            )
            await db.flush()
        
        # Create tracking context (ReportSchema has .id so this works)
        tracking = await self._create_tracking_context(
            db, user, organization, report, self.name, args
        )
        
        # Get organization settings
        org_settings = await organization.get_settings(db)
        
        # Get default model
        model = await organization.get_default_llm_model(db)
        if not model:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No default LLM model configured for this organization."
            )
            return MCPCreateDataOutput(
                report_id=str(report.id),
                success=False,
                error_message="No default LLM model configured for this organization.",
            ).model_dump()
        
        # Build data source clients
        ds_clients = {}
        for ds in data_sources:
            try:
                ds_clients[ds.name] = await ds_service.construct_client(db, ds, user)
            except Exception:
                pass
        
        if not ds_clients:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No data sources could be connected."
            )
            return MCPCreateDataOutput(
                report_id=str(report.id),
                success=False,
                error_message="No data sources could be connected.",
            ).model_dump()
        
        # Create ContextHub for schema building
        context_hub = ContextHub(
            db=db,
            organization=organization,
            report=report,
            data_sources=data_sources,
            user=user,
        )
        
        # Auto-discover tables if not provided
        tables_by_source = None
        if input_data.tables:
            tables_by_source = [
                {"data_source_id": t.data_source_id, "tables": t.tables}
                for t in input_data.tables
            ]
        else:
            tables_by_source = await self._discover_tables(
                context_hub.schema_builder,
                input_data.prompt,
                top_k=5,
            )
        
        # Build schemas excerpt
        schemas_excerpt = ""
        if tables_by_source:
            try:
                all_resolved_names = []
                ds_ids = []
                for group in tables_by_source:
                    if group.get("data_source_id"):
                        ds_ids.append(group["data_source_id"])
                    all_resolved_names.extend(group.get("tables", []))
                
                ds_scope = list(set(ds_ids)) if ds_ids else None
                name_patterns = [f"(?i)(?:^|\\.){re.escape(n)}$" for n in all_resolved_names] if all_resolved_names else None
                
                ctx = await context_hub.schema_builder.build(
                    with_stats=True,
                    data_source_ids=ds_scope,
                    name_patterns=name_patterns,
                )
                schemas_excerpt = ctx.render_combined(top_k_per_ds=10, index_limit=0, include_index=False)
            except Exception:
                schemas_excerpt = ""
        
        # Build codegen context
        runtime_ctx = {
            "settings": org_settings,
            "context_hub": context_hub,
            "ds_clients": ds_clients,
            "excel_files": [],
        }
        
        codegen_context = await build_codegen_context(
            runtime_ctx=runtime_ctx,
            user_prompt=input_data.prompt,
            interpreted_prompt=input_data.prompt,
            schemas_excerpt=schemas_excerpt,
            tables_by_source=tables_by_source,
        )
        
        # Setup Coder and Executor
        coder = Coder(
            model=model,
            organization_settings=org_settings,
            context_hub=context_hub,
            usage_session_maker=async_session_maker,
        )
        
        streamer = StreamingCodeExecutor(
            organization_settings=org_settings,
            logger=None,
            context_hub=context_hub,
        )
        
        # Validator function
        async def _validator_fn(code, data_model):
            return await coder.validate_code(code, data_model)
        
        # Execute code generation
        output_log = ""
        generated_code = ""
        exec_df = None
        code_errors = []
        
        sigkill_event = asyncio.Event()
        
        async for e in streamer.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=codegen_context, retries=2),
            ds_clients=ds_clients,
            excel_files=[],
            code_generator_fn=coder.generate_code,
            validator_fn=_validator_fn,
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
        
        # Determine visualization type
        viz_type = input_data.visualization_type or "table"
        
        # Build view from data model
        data_model = {"type": viz_type, "series": []}
        available_columns = [c.get("field") for c in formatted.get("columns", []) if c.get("field")]
        view_schema = build_view_from_data_model(
            data_model, 
            title=title, 
            palette_theme="default",
            available_columns=available_columns
        )
        view_payload = view_schema.model_dump(exclude_none=True) if view_schema else {"version": "v2", "view": {"type": viz_type}}
        
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
