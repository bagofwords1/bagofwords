"""
ContextHub - Main orchestrator for all agent context.
"""
import json
import time
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession

from .context_specs import (
    ContextMetadata, ContextSnapshot, ContextBuildSpec,
    ContextObjectsSnapshot,
    SchemaContextConfig, MessageContextConfig, MemoryContextConfig,
    WidgetContextConfig, InstructionContextConfig, CodeContextConfig,
    ResourceContextConfig
)
from .builders.schema_context_builder import SchemaContextBuilder
from .builders.files_context_builder import FilesContextBuilder
from .builders.message_context_builder import MessageContextBuilder
from .builders.memory_context_builder import MemoryContextBuilder
from .builders.widget_context_builder import WidgetContextBuilder
from .builders.query_context_builder import QueryContextBuilder
from .builders.instruction_context_builder import InstructionContextBuilder
from .builders.code_context_builder import CodeContextBuilder
from .builders.resource_context_builder import ResourceContextBuilder
from .builders.observation_context_builder import ObservationContextBuilder
from .context_view import ContextView, StaticSections, WarmSections
from .sections.messages_section import MessagesSection
from .sections.widgets_section import WidgetsSection
from .sections.observations_section import ObservationsSection
from .sections.resources_section import ResourcesSection
from .sections.code_section import CodeSection


class ContextHub:
    """
    Central hub for all agent context orchestration.
    
    Coordinates existing and new context builders to provide
    comprehensive context for agent execution.
    """
    
    def __init__(
        self,
        db: AsyncSession,
        organization,
        report,
        data_sources,
        user=None,
        head_completion=None,
        widget=None
    ):
        self.db = db
        self.organization = organization
        self.data_sources = data_sources
        self.report = report
        self.user = user
        self.head_completion = head_completion
        self.widget = widget
        self.prompt_content = head_completion.prompt if head_completion else ""
        
        # Initialize metadata
        self.metadata = ContextMetadata(
            organization_id=organization.id,
            user_id=user.id if user else None,
            report_id=report.id if report else None,
            completion_id=head_completion.id if head_completion else None,
            widget_id=widget.id if widget else None,
            external_platform=getattr(head_completion, 'external_platform', None) if head_completion else None,
            external_user_id=getattr(head_completion, 'external_user_id', None) if head_completion else None,
        )
        
        # Initialize builders
        self._init_builders()
        
        # Static context cache (will be added later)
        self._static_cache: Dict[str, Any] = {}
        self._warm_cache: Dict[str, Any] = {}
    
    def _init_builders(self):
        """Initialize all context builders."""
        
        # Existing builders (enhanced)
        self.instruction_builder = InstructionContextBuilder(self.db, self.organization)
        self.code_builder = CodeContextBuilder(self.db, self.organization)
        self.resource_builder = ResourceContextBuilder(self.db, self.data_sources, self.organization, self.prompt_content)
        self.files_builder = FilesContextBuilder(self.db, self.organization, self.report)
        
        # New builders (port from agent.py)
        self.schema_builder = SchemaContextBuilder(self.db, self.data_sources, self.organization, self.report)
        self.message_builder = MessageContextBuilder(self.db, self.organization, self.report, self.user)
        self.memory_builder = MemoryContextBuilder(self.db, self.organization, self.user, self.head_completion)
        self.widget_builder = WidgetContextBuilder(self.db, self.organization, self.report)
        self.query_builder = QueryContextBuilder(self.db, self.organization, self.report)
        
        # Observation context builder (tracks tool execution results)
        self.observation_builder = ObservationContextBuilder()
        
    async def build_context(
        self,
        spec: Optional[ContextBuildSpec] = None,
        research_context: Optional[Dict[str, Any]] = None,
        loop_index: int = 0
    ) -> ContextSnapshot:
        """
        Build comprehensive context snapshot for agent execution.
        
        Args:
            spec: What context sections to include
            research_context: Accumulated research findings
            loop_index: Current execution loop index
            
        Returns:
            Complete context snapshot with metadata
        """
        start_time = time.time()
        
        # Use default spec if not provided
        if spec is None:
            spec = ContextBuildSpec()
        
        # Update metadata
        self.metadata.loop_index = loop_index
        self.metadata.research_step_count = len(research_context or {})
        
        # Build context sections
        context = ContextSnapshot(metadata=self.metadata)
        section_sizes: Dict[str, int] = {}
        
        # Core sections
        if spec.include_schemas:
            # Build object using config params
            schema_cfg = spec.schema_config or SchemaContextConfig()
            schemas_section = await self.schema_builder.build(
                include_inactive=schema_cfg.include_inactive,
                with_stats=schema_cfg.with_stats,
                top_k=schema_cfg.top_k,
            )
            context.schemas_excerpt = schemas_section.render()
            # Prefer object-based count of tables if available; fallback to rendered lines
            try:
                data_sources = getattr(schemas_section, 'data_sources', []) or []
                table_count = 0
                for ds in data_sources:
                    tables = getattr(ds, 'tables', None)
                    if tables is None and isinstance(ds, dict):
                        tables = (ds.get('tables') or [])
                    if tables is None:
                        tables = []
                    try:
                        table_count += len(list(tables))
                    except Exception:
                        pass
                # Only set if we found a meaningful count; else fallback
                self.metadata.schemas_count = table_count if table_count > 0 else len(context.schemas_excerpt.split('\n'))
            except Exception:
                self.metadata.schemas_count = len(context.schemas_excerpt.split('\n'))
            section_sizes['schemas'] = len(context.schemas_excerpt or '')
        
        if spec.include_messages:
            # Use new config or fallback to legacy parameters
            message_config = spec.message_config
            if not message_config:
                # Create config from legacy parameters
                message_config = MessageContextConfig(
                    max_messages=spec.max_messages or 20,
                    role_filter=spec.message_role_filter
                )
            
            context.messages_context = await self.message_builder.build_context(
                max_messages=message_config.max_messages,
                role_filter=message_config.role_filter
            )
            self.metadata.messages_count = len(context.messages_context.split('\n'))
            section_sizes['messages'] = len(context.messages_context or '')
        
        if spec.include_memories:
            # Use new config or fallback to legacy parameters
            memory_config = spec.memory_config
            if not memory_config:
                memory_config = MemoryContextConfig(max_memories=spec.max_memories or 10)
            
            context.memories_context = await self.memory_builder.build_context(
                max_memories=memory_config.max_memories
            )
            self.metadata.memories_count = len(context.memories_context.split('\n'))
            section_sizes['memories'] = len(context.memories_context or '')
        
        if spec.include_widgets:
            # Use new config or fallback to legacy parameters
            widget_config = spec.widget_config
            if not widget_config:
                widget_config = WidgetContextConfig(
                    max_widgets=spec.max_widgets or 5,
                    status_filter=spec.widget_status_filter
                )
            
            context.widgets_context = await self.widget_builder.build_context(
                max_widgets=widget_config.max_widgets,
                status_filter=widget_config.status_filter,
                include_data_preview=widget_config.include_data_preview
            )
            self.metadata.widgets_count = len(context.widgets_context.split('\n'))
            section_sizes['widgets'] = len(context.widgets_context or '')
        
        if spec.include_instructions:
            # Build object, then render for legacy ContextSnapshot
            instruction_config = spec.instruction_config or InstructionContextConfig()
            inst_section = await self.instruction_builder.build(
                status=instruction_config.status,
                category=instruction_config.category,
            )
            context.instructions_context = inst_section.render()
            section_sizes['instructions'] = len(context.instructions_context or '')
        
        # Optional sections
        if spec.include_code:
            # CodeContextBuilder has complex interface, skip for now
            # TODO: Implement when code context is needed
            context.code_context = ""
            section_sizes['code'] = len(context.code_context or '')
        
        if spec.include_resource:
            context.resource_context = await self.resource_builder.build()
            section_sizes['resources'] = len(context.resource_context or '')

        # Files section (object cached, string rendered into legacy snapshot)
        if getattr(spec, 'include_files', True):
            files_section = await self.files_builder.build()
            # We do not attach to ContextSnapshot directly; kept for future
        
        # Research context
        if spec.include_research_context and research_context:
            context.research_context = research_context
        
        # Build history summary (simplified for now)
        context.history_summary = await self._build_history_summary(context)
        
        # Update metadata  
        self.metadata.build_duration_ms = (time.time() - start_time) * 1000
        
        # Count warm section items from object-based cache (more accurate than text line counting)
        messages_section = self._warm_cache.get("messages", None)
        if messages_section and hasattr(messages_section, 'items'):
            self.metadata.messages_count = len(messages_section.items)
            # Add messages section size for total_tokens calculation
            messages_text = messages_section.render() if messages_section else ""
            section_sizes['messages'] = len(messages_text)
        
        widgets_section = self._warm_cache.get("widgets", None)
        if widgets_section and hasattr(widgets_section, 'items'):
            self.metadata.widgets_count = len(widgets_section.items)
            # Add widgets section size for total_tokens calculation
            widgets_text = widgets_section.render() if widgets_section else ""
            section_sizes['widgets'] = len(widgets_text)
        
        queries_section = self._warm_cache.get("queries", None)
        if queries_section and hasattr(queries_section, 'items'):
            self.metadata.queries_count = len(queries_section.items)
            # Add queries section size for total_tokens calculation
            queries_text = queries_section.render() if queries_section else ""
            section_sizes['queries'] = len(queries_text)
        
        # Expose section sizes for UI diagnostics and calculate total_tokens as sum
        try:
            self.metadata.section_sizes = section_sizes
            # Calculate total_tokens as sum of all section sizes
            self.metadata.total_tokens = sum(section_sizes.values())
        except Exception:
            pass
        context.metadata = self.metadata
        
        return context

    async def build(self, spec: Optional[ContextBuildSpec] = None, research_context: Optional[Dict[str, Any]] = None, loop_index: int = 0) -> ContextObjectsSnapshot:
        """Build and return object-based snapshot."""
        if spec is None:
            spec = ContextBuildSpec()
        self.metadata.loop_index = loop_index
        self.metadata.research_step_count = len(research_context or {})

        # Build sections as objects
        schemas_obj = None
        files_obj = None
        if spec.include_schemas:
            schema_cfg = spec.schema_config or SchemaContextConfig()
            schemas_obj = await self.schema_builder.build(
                include_inactive=schema_cfg.include_inactive,
                with_stats=schema_cfg.with_stats,
                top_k=schema_cfg.top_k,
            )

        # Files
        files_obj = await self.files_builder.build()

        snapshot = ContextObjectsSnapshot(
            schemas=schemas_obj,
            files=files_obj,
            metadata=self.metadata,
        )
        # Cache
        self._static_cache["schemas"] = schemas_obj or self._static_cache.get("schemas")
        self._static_cache["files"] = files_obj or self._static_cache.get("files")
        return snapshot

    # --------------------------------------------------------------
    # Simple lifecycle helpers to prime static and refresh warm
    # --------------------------------------------------------------
    async def prime_static(self) -> None:
        """Build and cache static sections once (schemas, instructions, code, resources)."""
        self._static_cache["schemas"] = await self.schema_builder.build()
        # Instructions as object
        self._static_cache["instructions"] = await self.instruction_builder.build()
        # Optional sections depending on flags (code left None unless provided per data model)
        self._static_cache["code"] = None
        self._static_cache["resources"] = await self.resource_builder.build()
        # Files object
        self._static_cache["files"] = await self.files_builder.build()

    async def refresh_warm(self) -> None:
        """Rebuild warm sections each loop (messages, queries, observations)."""
        messages = await self.message_builder.build(max_messages=20)
        # Deprecate widgets from warm context: keep for backward compatibility but do not rebuild aggressively
        widgets = None

        queries = await self.query_builder.build(max_queries=5)

        observations = self.observation_builder.build()
        self._warm_cache.update({
            "messages": messages,
            "widgets": widgets,
            "queries": queries,
            "observations": observations,
        })

    def get_view(self) -> ContextView:
        """Return a read-only grouped view over current static + warm context."""
        static = StaticSections(
            schemas=self._static_cache.get("schemas", None),
            instructions=self._static_cache.get("instructions", None),
            resources=self._static_cache.get("resources", None),
            code=self._static_cache.get("code", None),
            files=self._static_cache.get("files", None),
        )
        warm = WarmSections(
            messages=self._warm_cache.get("messages", None),
            observations=self._warm_cache.get("observations", None),
            widgets=self._warm_cache.get("widgets", None),
            queries=self._warm_cache.get("queries", None),
        )
        meta = self.metadata.model_dump()
        return ContextView(static=static, warm=warm, meta=meta)
    
    async def _build_history_summary(self, context: ContextSnapshot) -> str:
        """Build a summary of conversation history for planner context."""
        # Simplified implementation - can be enhanced later
        summary_parts = []
        
        if context.messages_context:
            summary_parts.append(f"Previous conversation: {len(context.messages_context.split('user:'))} exchanges")
        
        if context.widgets_context:
            summary_parts.append(f"Created widgets: {self.metadata.widgets_count}")
        
        if context.research_context:
            research_tools = list(context.research_context.keys())
            summary_parts.append(f"Research completed: {', '.join(research_tools)}")
        
        return "; ".join(summary_parts) if summary_parts else "No previous context"
    
    async def get_schemas(self) -> str:
        """Quick access to schemas context only."""
        section = await self.schema_builder.build()
        return section.render()
    
    async def get_messages_context(self, max_messages: int = 20) -> str:
        """Quick access to messages context only."""
        section = await self.message_builder.build(max_messages=max_messages)
        return section.render()
    
    async def get_resources_context(self) -> str:
        """Quick access to resources context from metadata resources."""
        section = await self.resource_builder.build()
        return section.render()
    
    async def get_history_summary(self, research_context: Optional[Dict[str, Any]] = None) -> str:
        """Quick access to history summary."""
        # Build minimal context for summary
        spec = ContextBuildSpec(
            include_schemas=False,
            include_code=False,
            include_resource=False,
            max_messages=10,
            max_widgets=3,
            max_memories=5
        )
        context = await self.build_context(spec, research_context)
        return context.history_summary
    
    async def render(self, format_for_prompt: bool = True, include_metadata: bool = False) -> str:
        """
        Render context for prompt inclusion or debugging.
        
        Args:
            format_for_prompt: If True, format for LLM prompt inclusion (compact).
                             If False, format for debugging/inspection (detailed).
            include_metadata: Whether to include metadata in the output
            
        Returns:
            Formatted string representation of all context
        """
        context = await self.build_context()
        
        if format_for_prompt:
            return self._render_for_prompt(context, include_metadata)
        else:
            return self._render_for_debug(context, include_metadata)
    
    def _render_for_prompt(self, context: ContextSnapshot, include_metadata: bool) -> str:
        """Render context optimized for LLM prompt inclusion (compact format)."""
        parts = []
        
        if include_metadata:
            parts.append(f"<context_meta org={self.metadata.organization_id} report={self.metadata.report_id} loop={self.metadata.loop_index}/>")
        
        if context.schemas_excerpt:
            parts.append(f"<schemas>\n{context.schemas_excerpt}\n</schemas>")
        
        if context.messages_context:
            # Compact message format for prompts
            parts.append(f"<conversation>\n{context.messages_context[:2000]}...\n</conversation>")
        
        if context.widgets_context:
            parts.append(f"<widgets>\n{context.widgets_context}\n</widgets>")
        
        if context.memories_context:
            parts.append(f"<memories>\n{context.memories_context}\n</memories>")
        
        if context.instructions_context:
            parts.append(f"<instructions>\n{context.instructions_context}\n</instructions>")
        
        if context.research_context:
            research_summary = "; ".join([f"{k}: {v}" for k, v in context.research_context.items()])
            parts.append(f"<research>\n{research_summary}\n</research>")
        
        return "\n\n".join(parts)
    
    def _render_for_debug(self, context: ContextSnapshot, include_metadata: bool) -> str:
        """Render context for debugging/inspection (detailed format)."""
        parts = []
        
        if include_metadata:
            parts.append("=== CONTEXT METADATA ===")
            parts.append(f"Organization: {self.metadata.organization_id}")
            parts.append(f"Report: {self.metadata.report_id}")
            parts.append(f"User: {self.metadata.user_id}")
            parts.append(f"Loop: {self.metadata.loop_index}")
            parts.append(f"Research Steps: {self.metadata.research_step_count}")
            parts.append(f"Generated: {self.metadata.generation_time}")
            parts.append("")
        
        if context.schemas_excerpt:
            parts.append("=== SCHEMAS ===")
            parts.append(context.schemas_excerpt)
            parts.append("")
        
        if context.messages_context:
            parts.append("=== CONVERSATION HISTORY ===")
            parts.append(context.messages_context)
            parts.append("")
        
        if context.widgets_context:
            parts.append("=== WIDGETS ===")
            parts.append(context.widgets_context)
            parts.append("")
        
        if context.memories_context:
            parts.append("=== MEMORIES ===")
            parts.append(context.memories_context)
            parts.append("")
        
        if context.instructions_context:
            parts.append("=== INSTRUCTIONS ===")
            parts.append(context.instructions_context)
            parts.append("")
        
        # Add observation context if available
        observation_context = self.observation_builder.build_context(format_for_prompt=False)
        if observation_context:
            parts.append("=== OBSERVATION CONTEXT ===")
            parts.append(observation_context)
            parts.append("")
        
        if context.history_summary:
            parts.append("=== SUMMARY ===")
            parts.append(context.history_summary)
        
        return "\n".join(parts)