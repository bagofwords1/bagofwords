"""
Agent Context Builder - Main orchestrator for all context builders.
"""
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession

from ..context_specs import ContextMetadata, ContextSnapshot, ContextBuildSpec
from .schema_context_builder import SchemaContextBuilder
from .message_context_builder import MessageContextBuilder
from .memory_context_builder import MemoryContextBuilder
from .widget_context_builder import WidgetContextBuilder


class AgentContextBuilder:
    """
    Main context builder that orchestrates all specialized builders.
    
    This is the central point for building comprehensive agent context,
    coordinating schema, message, memory, widget, and other context types.
    """
    
    def __init__(
        self,
        db: AsyncSession,
        organization,
        report,
        user=None,
        head_completion=None,
        widget=None
    ):
        self.db = db
        self.organization = organization
        self.report = report
        self.user = user
        self.head_completion = head_completion
        self.widget = widget
        
        # Initialize all specialized builders
        self.schema_builder = SchemaContextBuilder(self.db, self.report)
        self.message_builder = MessageContextBuilder(self.db, self.report)
        self.memory_builder = MemoryContextBuilder(self.db, self.head_completion)
        self.widget_builder = WidgetContextBuilder(self.db, self.report)
        
        # Initialize metadata
        self.metadata = ContextMetadata(
            organization_id=self.organization.id,
            user_id=self.user.id if self.user else None,
            report_id=self.report.id if self.report else None,
            completion_id=self.head_completion.id if self.head_completion else None,
            widget_id=self.widget.id if self.widget else None,
            external_platform=getattr(self.head_completion, 'external_platform', None) if self.head_completion else None,
            external_user_id=getattr(self.head_completion, 'external_user_id', None) if self.head_completion else None,
        )
    
    async def build_comprehensive_context(
        self,
        spec: Optional[ContextBuildSpec] = None,
        research_context: Optional[Dict[str, Any]] = None
    ) -> ContextSnapshot:
        """
        Build comprehensive context using all available builders.
        
        Args:
            spec: Specification for what context to build
            research_context: Accumulated research findings
            
        Returns:
            Complete context snapshot
        """
        if spec is None:
            spec = ContextBuildSpec()
        
        # Initialize context snapshot
        context = ContextSnapshot(metadata=self.metadata)
        
        # Build each section based on spec
        if spec.include_schemas:
            context.schemas_excerpt = await self.schema_builder.build_context()
            self.metadata.schemas_count = await self.schema_builder.get_data_source_count()
        
        if spec.include_messages:
            context.messages_context = await self.message_builder.build_context(
                max_messages=spec.max_messages,
                role_filter=spec.message_role_filter
            )
            self.metadata.messages_count = await self.message_builder.get_message_count(
                role_filter=spec.message_role_filter
            )
        
        if spec.include_memories:
            context.memories_context = await self.memory_builder.build_context(
                max_memories=spec.max_memories
            )
            self.metadata.memories_count = await self.memory_builder.get_memory_count()
        
        if spec.include_widgets:
            context.widgets_context = await self.widget_builder.build_context(
                max_widgets=spec.max_widgets,
                status_filter=spec.widget_status_filter
            )
            self.metadata.widgets_count = await self.widget_builder.get_widget_count()
        
        # Add research context if provided
        if spec.include_research_context and research_context:
            context.research_context = research_context
        
        # Update final metadata
        context.metadata = self.metadata
        
        return context
    
    async def build_schemas_excerpt(self) -> str:
        """Quick access to schemas context (for backward compatibility)."""
        return await self.schema_builder.build_context()
    
    async def build_history_summary(
        self, 
        research_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Build a concise history summary for planner context.
        
        Args:
            research_context: Accumulated research findings
            
        Returns:
            Formatted history summary string
        """
        summary_parts = []
        
        # Message summary
        message_count = await self.message_builder.get_message_count()
        if message_count > 0:
            summary_parts.append(f"Previous conversation: {message_count} messages")
        
        # Widget summary
        widget_count = await self.widget_builder.get_widget_count()
        if widget_count > 0:
            summary_parts.append(f"Created widgets: {widget_count}")
        
        # Memory summary
        memory_count = await self.memory_builder.get_memory_count()
        if memory_count > 0:
            summary_parts.append(f"Saved memories: {memory_count}")
        
        # Research summary
        if research_context:
            research_tools = list(research_context.keys())
            summary_parts.append(f"Research completed: {', '.join(research_tools)}")
        
        return "; ".join(summary_parts) if summary_parts else "No previous context"
    
    async def render(self, include_details: bool = False) -> str:
        """
        Render a human-readable view of all context from individual builders.
        
        Args:
            include_details: Whether to include detailed content from each builder
            
        Returns:
            Formatted string representation of all context sections
        """
        parts = [
            "=== AGENT CONTEXT OVERVIEW ===",
            f"Organization: {self.organization.id if self.organization else 'N/A'}",
            f"Report: {self.report.id if self.report else 'N/A'}",
            f"User: {self.user.id if self.user else 'N/A'}",
            ""
        ]
        
        if include_details:
            # Detailed render of each section
            parts.append(await self.schema_builder.render())
            parts.append("")
            parts.append(await self.message_builder.render())
            parts.append("")
            parts.append(await self.widget_builder.render())
            parts.append("")
            parts.append(await self.memory_builder.render())
        else:
            # Summary render
            schema_count = await self.schema_builder.get_data_source_count()
            message_count = await self.message_builder.get_message_count()
            widget_count = await self.widget_builder.get_widget_count()
            memory_count = await self.memory_builder.get_memory_count()
            
            parts.extend([
                "Context Summary:",
                f"  • Schemas: {schema_count} data sources",
                f"  • Messages: {message_count} in conversation",
                f"  • Widgets: {widget_count} created",
                f"  • Memories: {memory_count} referenced"
            ])
        
        return "\n".join(parts)