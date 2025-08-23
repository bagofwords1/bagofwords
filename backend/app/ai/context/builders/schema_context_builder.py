"""
Schema Context Builder - Ports proven logic from agent._build_schemas_context()
"""
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession


class SchemaContextBuilder:
    """
    Builds database schema context for agent execution.
    
    Ports the proven logic from agent._build_schemas_context() with
    data sources and file schemas.
    """
    
    def __init__(self, db: AsyncSession, report):
        self.db = db
        self.report = report
    
    async def build_context(self) -> str:
        """
        Build comprehensive schema context.
        
        Exact port from agent._build_schemas_context() - proven logic.
        
        Returns:
            Formatted schema context string with data sources and files
        """
        context = []
        
        # Get data sources and files from report
        data_sources = getattr(self.report, 'data_sources', []) or []
        files = getattr(self.report, 'files', []) or []
        
        # Build data source schemas
        for data_source in data_sources:
            context.append(
                f"<data_source>: {data_source.name}</data_source>\n"
                f"<data_source_type>: {data_source.type}</data_source_type>\n"
                f"<data_source_id>: {data_source.id}</data_source_id>\n\n"
                "<schema>:"
            )
            
            # Get schema with stats
            schema_content = await data_source.prompt_schema(
                self.db, 
                "", 
                with_stats=True
            )
            context.append(schema_content)
            context.append("</schema>\n")
            
            # Add business context if available
            if hasattr(data_source, 'context') and data_source.context:
                context.append(
                    f"<data_source_context>: \n"
                    f"Use this context as business context and rules for the data source\n"
                    f"{data_source.context}\n"
                    f"</data_source_context>"
                )
        
        # Build file schemas
        for file in files:
            file_schema = file.prompt_schema()
            context.append(file_schema)
        
        return "\n".join(context)
    
    async def get_data_source_count(self) -> int:
        """Get number of available data sources."""
        data_sources = getattr(self.report, 'data_sources', []) or []
        return len(data_sources)
    
    async def get_file_count(self) -> int:
        """Get number of available files."""
        files = getattr(self.report, 'files', []) or []
        return len(files)