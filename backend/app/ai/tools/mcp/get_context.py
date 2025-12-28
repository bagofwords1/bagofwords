"""MCP Tool: get_context - Returns available schemas and metadata resources."""

import re
from typing import Dict, Any, List

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
from app.ai.context.builders.resource_context_builder import ResourceContextBuilder
from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.services.report_service import ReportService
from app.schemas.mcp import (
    GetContextInput,
    GetContextOutput,
    DataSourceInfo,
    TableInfo,
    ResourceInfo,
)


class GetContextTool(MCPTool):
    """Get available data sources, tables, and metadata resources.
    
    Use this tool to understand what data is available before running queries.
    Returns schemas ranked by usage and relevance.
    """
    
    name = "get_context"
    description = (
        "Get available data sources, tables, and metadata resources. Useful for searching or explaining metadata. "
        "Optionally filter by regex patterns."
    )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return GetContextInput.model_json_schema()
    
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Get context with schemas and resources."""
        
        input_data = GetContextInput(**args)
        
        report_service = ReportService()
        
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
        
        # Build schema context
        schema_builder = SchemaContextBuilder(db, data_sources, organization, report, user)
        schemas = await schema_builder.build(
            with_stats=True,
            top_k=20,
            name_patterns=input_data.patterns,
        )
        
        # Build resource context
        resource_builder = ResourceContextBuilder(db, data_sources, organization, {})
        resources_section = await resource_builder.build()
        
        # Convert schemas to output format
        data_sources_output: List[DataSourceInfo] = []
        for ds in schemas.data_sources:
            tables: List[TableInfo] = []
            for table in ds.tables:
                columns = [col.name for col in (table.columns or [])]
                description = None
                if table.metadata_json:
                    description = table.metadata_json.get("description")
                tables.append(TableInfo(
                    name=table.name,
                    columns=columns,
                    description=description,
                ))
            
            data_sources_output.append(DataSourceInfo(
                id=str(ds.info.id),
                name=ds.info.name,
                type=ds.info.type,
                tables=tables,
            ))
        
        # Convert resources to output format
        resources_output: List[ResourceInfo] = []
        if resources_section and resources_section.repositories:
            for repo in resources_section.repositories:
                for resource in (repo.resources or []):
                    # Filter by patterns if provided
                    if input_data.patterns:
                        match = False
                        searchable = f"{resource.get('name', '')} {resource.get('description', '')}"
                        for pattern in input_data.patterns:
                            try:
                                if re.search(pattern, searchable, re.IGNORECASE):
                                    match = True
                                    break
                            except re.error:
                                # Invalid regex, try literal match
                                if pattern.lower() in searchable.lower():
                                    match = True
                                    break
                        if not match:
                            continue
                    
                    resources_output.append(ResourceInfo(
                        name=resource.get("name", ""),
                        resource_type=resource.get("resource_type", ""),
                        description=resource.get("description"),
                    ))
        
        output = GetContextOutput(
            report_id=str(report.id),
            data_sources=data_sources_output,
            resources=resources_output,
        )
        
        # Finish tracking
        total_tables = sum(len(ds.tables) for ds in data_sources_output)
        await self._finish_tracking(
            db, tracking, success=True,
            summary=f"Retrieved context: {len(data_sources_output)} data sources, {total_tables} tables, {len(resources_output)} resources",
            result_json={
                "data_source_count": len(data_sources_output),
                "table_count": total_tables,
                "resource_count": len(resources_output),
            },
        )
        
        return output.model_dump()
