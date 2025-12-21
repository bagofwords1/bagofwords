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
from .builders.mention_context_builder import MentionContextBuilder
from .builders.entity_context_builder import EntityContextBuilder
from app.ai.utils.token_counter import count_tokens


# Default caps to keep planner prompt small and predictable
DEFAULT_CONTEXT_LIMITS = {
    "messages_max": 20,        # last N messages
    "observations_max": 8    # last N observations
}


def _truncate_list(items, max_items):
    if not isinstance(items, list) or not max_items:
        return items
    return items[-max_items:]


def _safe_setattr_list(section, attr, max_items):
    """Safely truncate a list attribute on a section object."""
    try:
        if section and hasattr(section, attr):
            value = getattr(section, attr)
            if isinstance(value, list):
                setattr(section, attr, _truncate_list(value, max_items))
    except Exception:
        # Best-effort truncation only; never fail the refresh/build
        pass


def _section_token_length(text: Optional[str]) -> int:
    """Measure a section's size using token counts with safe fallbacks."""
    if not text:
        return 0
    try:
        return count_tokens(text)
    except Exception:
        # As a last resort, approximate via character length
        return len(text)


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
        widget=None,
        organization_settings=None
    ):
        self.db = db
        self.organization = organization
        self.organization_settings = organization_settings
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
        self.instruction_builder = InstructionContextBuilder(
            self.db, 
            self.organization, 
            organization_settings=self.organization_settings
        )
        self.code_builder = CodeContextBuilder(self.db, self.organization)
        self.resource_builder = ResourceContextBuilder(self.db, self.data_sources, self.organization, self.prompt_content)
        self.files_builder = FilesContextBuilder(self.db, self.organization, self.report)
        
        # New builders (port from agent.py)
        self.schema_builder = SchemaContextBuilder(self.db, self.data_sources, self.organization, self.report, user=self.user)
        self.message_builder = MessageContextBuilder(self.db, self.organization, self.report, self.user)
        self.memory_builder = MemoryContextBuilder(self.db, self.organization, self.user, self.head_completion)
        self.widget_builder = WidgetContextBuilder(self.db, self.organization, self.report)
        self.query_builder = QueryContextBuilder(self.db, self.organization, self.report)
        self.mention_builder = MentionContextBuilder(self.db, self.organization, self.report, self.head_completion)
        self.entity_builder = EntityContextBuilder(self.db, self.organization, self.report)
        
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
                with_stats=schema_cfg.with_stats,
                top_k=schema_cfg.top_k,
                active_only=schema_cfg.active_only,
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
            section_sizes['schemas'] = _section_token_length(context.schemas_excerpt or '')
        
        if spec.include_messages:
            # Use new config or fallback to legacy parameters
            message_config = spec.message_config
            if not message_config:
                # Create config from legacy parameters
                message_config = MessageContextConfig(
                    max_messages=spec.max_messages or DEFAULT_CONTEXT_LIMITS["messages_max"],
                    role_filter=spec.message_role_filter
                )
            
            context.messages_context = await self.message_builder.build_context(
                max_messages=message_config.max_messages,
                role_filter=message_config.role_filter
            )
            self.metadata.messages_count = len(context.messages_context.split('\n'))
            section_sizes['messages'] = _section_token_length(context.messages_context or '')
        
        if spec.include_memories:
            # Use new config or fallback to legacy parameters
            memory_config = spec.memory_config
            if not memory_config:
                memory_config = MemoryContextConfig(max_memories=spec.max_memories or 10)
            
            context.memories_context = await self.memory_builder.build_context(
                max_memories=memory_config.max_memories
            )
            self.metadata.memories_count = len(context.memories_context.split('\n'))
            section_sizes['memories'] = _section_token_length(context.memories_context or '')
        
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
            section_sizes['widgets'] = _section_token_length(context.widgets_context or '')
        
        if spec.include_instructions:
            # Build object, then render for legacy ContextSnapshot
            instruction_config = spec.instruction_config or InstructionContextConfig()
            inst_section = await self.instruction_builder.build(
                category=instruction_config.category,
            )
            context.instructions_context = inst_section.render()
            section_sizes['instructions'] = _section_token_length(context.instructions_context or '')
        
        # Optional sections
        if spec.include_code:
            # CodeContextBuilder has complex interface, skip for now
            # TODO: Implement when code context is needed
            context.code_context = ""
            section_sizes['code'] = _section_token_length(context.code_context or '')
        
        if spec.include_resource:
            context.resource_context = await self.resource_builder.build()
            section_sizes['resources'] = _section_token_length(context.resource_context or '')

        # Files section (object cached, string rendered into legacy snapshot)
        if getattr(spec, 'include_files', True):
            files_section = await self.files_builder.build()
            # We do not attach to ContextSnapshot directly; kept for future

        # Entities section (delegated to builder; no inline heuristics)
        try:
            if getattr(spec, 'include_entities', False):
                # Get allow_llm_see_data setting
                allow_llm_see_data = True
                try:
                    org_settings = await self.organization.get_settings(self.db)
                    cfg = org_settings.get_config("allow_llm_see_data") if org_settings else None
                    allow_llm_see_data = bool(cfg.value) if cfg is not None else True
                except Exception:
                    pass
                
                ent_cfg = getattr(spec, 'entities_config', None)
                ent_section = await self.entity_builder.build_for_turn(
                    types=(getattr(ent_cfg, 'types', None) if ent_cfg else None),
                    top_k=(getattr(ent_cfg, 'top_k', 10) if ent_cfg else 10),
                    require_source_assoc=(getattr(ent_cfg, 'require_data_source_association', True) if ent_cfg else True),
                    keywords=(getattr(ent_cfg, 'keywords', None) if ent_cfg else None),
                    user_text=(self.prompt_content.get("content") if isinstance(self.prompt_content, dict) else str(self.prompt_content or "")),
                    allow_llm_see_data=allow_llm_see_data,
                )
                if ent_section:
                    context.entities_context = ent_section.render()
                    self._warm_cache["entities"] = ent_section
                    try:
                        self.metadata.entities_count = len(getattr(ent_section, 'items', []) or [])
                    except Exception:
                        pass
                    section_sizes['entities'] = _section_token_length(context.entities_context or '')
        except Exception:
            pass
        
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
            section_sizes['messages'] = _section_token_length(messages_text)
        
        widgets_section = self._warm_cache.get("widgets", None)
        if widgets_section and hasattr(widgets_section, 'items'):
            self.metadata.widgets_count = len(widgets_section.items)
            # Add widgets section size for total_tokens calculation
            widgets_text = widgets_section.render() if widgets_section else ""
            section_sizes['widgets'] = _section_token_length(widgets_text)
        
        queries_section = self._warm_cache.get("queries", None)
        if queries_section and hasattr(queries_section, 'items'):
            self.metadata.queries_count = len(queries_section.items)
            # Add queries section size for total_tokens calculation
            queries_text = queries_section.render() if queries_section else ""
            section_sizes['queries'] = _section_token_length(queries_text)
        
        # Mentions section counts (mirror pattern used above)
        mentions_section = self._warm_cache.get("mentions", None)
        if mentions_section is not None:
            try:
                files_len = len(getattr(mentions_section, 'files', []) or [])
                ds_len = len(getattr(mentions_section, 'data_sources', []) or [])
                tables_len = len(getattr(mentions_section, 'tables', []) or [])
                entities_len = len(getattr(mentions_section, 'entities', []) or [])
                # Expose a total mentions count in metadata for diagnostics
                self.metadata.__dict__["mentions_count"] = files_len + ds_len + tables_len + entities_len
            except Exception:
                pass
            # Add mentions section size for total_tokens calculation
            try:
                mentions_text = mentions_section.render()
                section_sizes['mentions'] = _section_token_length(mentions_text or '')
            except Exception:
                pass
        
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
                with_stats=schema_cfg.with_stats,
                top_k=schema_cfg.top_k,
                active_only=schema_cfg.active_only,
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
    async def prime_static(self, query: str | None = None) -> None:
        """Build and cache static sections once (schemas, instructions, code, resources).
        
        Runs all builders in parallel for faster startup.
        
        Parameters
        ----------
        query : str | None, optional
            The user's query/prompt. If provided, enables intelligent instruction
            search to find relevant instructions beyond just 'always' load mode.
        """
        import asyncio
        # Run all static builders in parallel
        schemas_task = asyncio.create_task(self.schema_builder.build())
        # Pass query to enable intelligent instruction search
        instructions_task = asyncio.create_task(self.instruction_builder.build(query))
        resources_task = asyncio.create_task(self.resource_builder.build())
        files_task = asyncio.create_task(self.files_builder.build())
        
        # Wait for all to complete
        schemas, instructions, resources, files = await asyncio.gather(
            schemas_task, instructions_task, resources_task, files_task,
            return_exceptions=True
        )
        
        # Store results (handle exceptions gracefully)
        self._static_cache["schemas"] = schemas if not isinstance(schemas, Exception) else None
        self._static_cache["instructions"] = instructions if not isinstance(instructions, Exception) else None
        self._static_cache["code"] = None
        self._static_cache["resources"] = resources if not isinstance(resources, Exception) else None
        self._static_cache["files"] = files if not isinstance(files, Exception) else None

    async def refresh_warm(self) -> None:
        """Rebuild warm sections each loop (messages, queries, observations, entities).
        
        Runs builders in parallel where possible for faster refresh.
        """
        import asyncio
        
        # Get org settings first (needed for queries and entities)
        allow_llm_see_data = True
        try:
            org_settings = await self.organization.get_settings(self.db)
            cfg = org_settings.get_config("allow_llm_see_data") if org_settings else None
            allow_llm_see_data = bool(cfg.value) if cfg is not None else True
        except Exception:
            allow_llm_see_data = True
        
        # Extract user text for entity keyword matching
        user_text = ""
        try:
            if isinstance(self.prompt_content, dict):
                user_text = self.prompt_content.get("content", "")
            else:
                user_text = str(self.prompt_content or "")
        except Exception:
            user_text = ""
        
        # Run all warm builders in parallel
        messages_task = asyncio.create_task(self.message_builder.build(max_messages=DEFAULT_CONTEXT_LIMITS["messages_max"]))
        queries_task = asyncio.create_task(self.query_builder.build(max_queries=5, include_data_preview=allow_llm_see_data))
        mentions_task = asyncio.create_task(self.mention_builder.build())
        entities_task = asyncio.create_task(self.entity_builder.build_for_turn(
            top_k=5,
            require_source_assoc=True,
            user_text=user_text,
            allow_llm_see_data=allow_llm_see_data,
        ))
        
        # Wait for all to complete
        messages, queries, mentions, entities = await asyncio.gather(
            messages_task, queries_task, mentions_task, entities_task,
            return_exceptions=True
        )
        
        # Build observations synchronously (it's fast, no DB calls)
        observations = self.observation_builder.build()
        _safe_setattr_list(observations, "items", DEFAULT_CONTEXT_LIMITS["observations_max"])
        
        self._warm_cache.update({
            "messages": messages if not isinstance(messages, Exception) else None,
            "widgets": None,  # Deprecated
            "queries": queries if not isinstance(queries, Exception) else None,
            "observations": observations,
            "mentions": mentions if not isinstance(mentions, Exception) else None,
            "entities": entities if not isinstance(entities, Exception) else None,
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
            mentions=self._warm_cache.get("mentions", None),
            entities=self._warm_cache.get("entities", None),
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
    
    def get_history_summary(self, research_context: Optional[Dict[str, Any]] = None) -> str:
        """Quick access to history summary from cached warm sections.
        
        This is a fast synchronous method that uses already-cached data from
        refresh_warm() instead of rebuilding context via build_context().
        """
        summary_parts = []
        
        # Use cached messages section
        messages_section = self._warm_cache.get("messages")
        if messages_section:
            try:
                items = getattr(messages_section, 'items', []) or []
                user_count = sum(1 for m in items if getattr(m, 'role', '') == 'user')
                summary_parts.append(f"Previous conversation: {user_count} exchanges")
            except Exception:
                pass
        
        # Use cached widgets section
        widgets_section = self._warm_cache.get("widgets")
        if widgets_section:
            try:
                items = getattr(widgets_section, 'items', []) or []
                if items:
                    summary_parts.append(f"Created widgets: {len(items)}")
            except Exception:
                pass
        
        # Use cached queries section
        queries_section = self._warm_cache.get("queries")
        if queries_section:
            try:
                items = getattr(queries_section, 'items', []) or []
                if items:
                    summary_parts.append(f"Queries executed: {len(items)}")
            except Exception:
                pass
        
        # Include research context if provided
        if research_context:
            research_tools = list(research_context.keys())
            if research_tools:
                summary_parts.append(f"Research completed: {', '.join(research_tools)}")
        
        return "; ".join(summary_parts) if summary_parts else "No previous context"
    
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