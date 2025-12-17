"""MCP Tool: inspect_data - Quick data inspection with auto-discovery."""

import re
import asyncio
from typing import Dict, Any, List

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
from app.ai.context import ContextHub
from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenRequest
from app.ai.prompt_formatters import build_codegen_context
from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.services.report_service import ReportService
from app.services.data_source_service import DataSourceService
from app.schemas.mcp import MCPInspectDataInput, MCPInspectDataOutput
from app.dependencies import async_session_maker


class InspectDataMCPTool(MCPTool):
    """Quick, ephemeral data inspection for exploration and debugging.
    
    Use to understand data structure, check column values, sample rows, or validate 
    assumptions before calling create_data. Results are not persisted as visualizations.
    Tables are auto-discovered from the prompt if not explicitly provided.
    """
    
    name = "inspect_data"
    description = (
        "Quick data inspection for exploration and debugging. "
        "Use to preview data (head/tail), check column types, understand structure, "
        "or validate assumptions before creating a final visualization. "
        "Results are logged but not saved as persistent visualizations. "
        "Use create_data for results that should be tracked and shared. "
        "Tables are auto-discovered from prompt if not provided."
        "Returns only a sample of 3 rows of data for each table!"
    )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return MCPInspectDataInput.model_json_schema()
    
    async def _discover_tables(
        self,
        schema_builder: SchemaContextBuilder,
        prompt: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Auto-discover relevant tables from user prompt using keyword extraction."""
        # Extract keywords from prompt
        tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]{3,}", prompt)]
        keywords = list(dict.fromkeys(tokens))[:5]  # Dedupe, limit to 5
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
        """Execute data inspection with auto-discovery."""
        
        input_data = MCPInspectDataInput(**args)
        
        report_service = ReportService()
        ds_service = DataSourceService()
        
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
        
        # Check if LLM is allowed to see data
        allow_llm_see_data = True
        try:
            cfg = org_settings.get_config("allow_llm_see_data")
            allow_llm_see_data = bool(cfg.value) if cfg else True
        except Exception:
            pass
        
        if not allow_llm_see_data:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="Data inspection is disabled. The 'Allow LLM to see data' setting is turned off."
            )
            return MCPInspectDataOutput(
                report_id=str(report.id),
                success=False,
                execution_log="",
                error_message="Data inspection is disabled. The 'Allow LLM to see data' setting is turned off for this organization.",
            ).model_dump()
        
        # Get default model
        model = await organization.get_default_llm_model(db)
        if not model:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No default LLM model configured for this organization."
            )
            return MCPInspectDataOutput(
                report_id=str(report.id),
                success=False,
                execution_log="",
                error_message="No default LLM model configured for this organization.",
            ).model_dump()
        
        # Build data source clients
        ds_clients = {}
        for ds in data_sources:
            try:
                ds_clients[ds.name] = await ds_service.construct_client(db, ds, user)
            except Exception:
                # Skip data sources that fail to connect
                pass
        
        if not ds_clients:
            await self._finish_tracking(
                db, tracking, success=False,
                summary="No data sources could be connected."
            )
            return MCPInspectDataOutput(
                report_id=str(report.id),
                success=False,
                execution_log="",
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
        
        # Build schemas excerpt for the resolved tables
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
        
        # Wrap generate_inspection_code
        async def _inspection_generator_fn(**kwargs):
            return await coder.generate_inspection_code(**kwargs)
        
        # Loose validator for inspection
        async def _loose_validator(code, dm):
            return {"valid": True, "reasoning": "Inspection mode - strict validation skipped"}
        
        # Execute inspection
        output_log = ""
        generated_code = ""
        success = False
        execution_error = None
        
        sigkill_event = asyncio.Event()
        
        async for e in streamer.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=codegen_context, retries=0),
            ds_clients=ds_clients,
            excel_files=[],
            code_generator_fn=_inspection_generator_fn,
            validator_fn=_loose_validator,
            sigkill_event=sigkill_event,
        ):
            if e["type"] == "stdout":
                payload = e["payload"]
                if isinstance(payload, str):
                    output_log += payload + "\n"
                else:
                    output_log += (payload.get("message") or "") + "\n"
            elif e["type"] == "done":
                success = True
                generated_code = e["payload"].get("code") or ""
                if e["payload"].get("errors"):
                    success = False
                    execution_error = str(e["payload"]["errors"])
                full_log = e["payload"].get("execution_log")
                if full_log and len(full_log) > len(output_log):
                    output_log = full_log
        
        # Finish tracking
        summary = "Data inspection completed successfully" if success else f"Data inspection failed: {execution_error or 'Unknown error'}"
        await self._finish_tracking(
            db, tracking, success=success,
            summary=summary,
            result_json={"output_length": len(output_log)} if success else None,
        )
        
        from app.settings.config import settings
        base_url = settings.bow_config.base_url
        
        output = MCPInspectDataOutput(
            report_id=str(report.id),
            success=success,
            execution_log=output_log[:10000] if output_log else "No output produced.",
            error_message=execution_error,
            url=f"{base_url}/reports/{report.id}",
        )
        
        return output.model_dump()
