"""MCP Tool: create_report - Creates a new analysis session (report)."""

from typing import Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.mcp.base import MCPTool
from app.models.user import User
from app.models.organization import Organization
from app.services.report_service import ReportService
from app.services.data_source_service import DataSourceService
from app.schemas.report_schema import ReportCreate
from app.settings.config import settings


class CreateReportTool(MCPTool):
    """Create a new analysis session (report).
    
    Returns a report_id that can be used in subsequent tool calls
    to maintain conversation context.
    """
    
    name = "create_report"
    description = (
        "Create a new analysis session (report). "
        "Call this once at the start of a conversation to get a report_id. "
        "Use that report_id in all subsequent create_data, inspect_data, and get_context calls."
    )
    
    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "A short, descriptive title summarizing what the user wants to analyze. "
                        "Examples: 'Customer Revenue Analysis', 'Q4 Sales Trends', 'Top Products by Region'. "
                        "Derive from the user's request - avoid generic titles like 'Data Analysis'."
                    )
                },
            },
            "required": ["title"],
        }
    
    async def execute(
        self, 
        args: Dict[str, Any], 
        db: AsyncSession,
        user: User,
        organization: Organization,
    ) -> Dict[str, Any]:
        """Create a new report with all active data sources attached."""
        
        report_service = ReportService()
        ds_service = DataSourceService()
        
        # Get or create MCP platform
        platform = await self._get_or_create_mcp_platform(db, organization)
        
        # Ensure user mapping exists for Members page visibility
        await self._ensure_mcp_user_mapping(db, user, organization, platform)
        
        # Get all active data sources for the organization
        data_sources = await ds_service.get_active_data_sources(db, organization)
        
        # Create the report with MCP platform
        report = await report_service.create_report(
            db=db,
            report_data=ReportCreate(
                title=args.get("title") or "MCP Session",
                data_sources=[ds.id for ds in data_sources],
                external_platform_id=str(platform.id),
            ),
            current_user=user,
            organization=organization,
        )
        
        base_url = settings.bow_config.base_url
        
        return {
            "report_id": str(report.id),
            "title": report.title,
            "url": f"{base_url}/reports/{report.id}",
            "data_sources": [
                {"id": str(ds.id), "name": ds.name, "type": ds.connections[0].type if ds.connections else None}
                for ds in data_sources
            ],
        }
