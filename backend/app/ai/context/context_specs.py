"""
Context management specifications and Pydantic models.
"""
from datetime import datetime
from typing import Dict, Optional, List, Any
from pydantic import BaseModel, Field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Only for type checking to avoid heavy imports at runtime
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.ai.context.sections.files_schema_section import FilesSchemaContext


class ContextMetadata(BaseModel):
    """Metadata for context generation and tracking."""
    
    # Core identifiers (string to support UUIDs)
    report_id: Optional[str] = None
    widget_id: Optional[str] = None  
    completion_id: Optional[str] = None
    user_id: Optional[str] = None
    organization_id: str
    
    # Execution context
    agent_type: str = "agent_v2"  # "agent_v1" vs "agent_v2"
    loop_index: int = 0
    research_step_count: int = 0
    
    # Temporal context
    generation_time: datetime = Field(default_factory=datetime.utcnow)
    context_window_start: Optional[datetime] = None
    context_window_end: Optional[datetime] = None
    
    # Performance metadata  
    total_tokens: int = 0
    section_sizes: Dict[str, int] = Field(default_factory=dict)
    build_duration_ms: float = 0
    
    # Content metadata
    schemas_count: int = 0
    messages_count: int = 0
    widgets_count: int = 0
    queries_count: int = 0
    memories_count: int = 0
    entities_count: int = 0
    metadata_resources_count: int = 0
    
    # External context
    external_platform: Optional[str] = None
    external_user_id: Optional[str] = None
    
    # Organization settings
    allow_llm_see_data: bool = True
    enable_code_context: bool = False


class ContextSnapshot(BaseModel):
    """Complete context snapshot for agent execution."""
    
    # Core context sections
    schemas_excerpt: str = ""
    messages_context: str = ""
    memories_context: str = ""
    widgets_context: str = ""
    queries_context: str = ""
    instructions_context: str = ""
    entities_context: str = ""
    code_context: str = ""
    resource_context: str = ""
    
    # Research context (accumulated during execution)
    research_context: Dict[str, Any] = Field(default_factory=dict)
    
    # Metadata
    metadata: ContextMetadata
    
    # Summary for history
    history_summary: str = ""
    
    # Lightweight usage tracking (what was actually sent to LLM)
    schemas_usage: Optional[Dict[str, Any]] = None  # SchemaUsageSnapshot.model_dump()
    instructions_usage: Optional[List[Dict[str, Any]]] = None  # List of InstructionItem summaries


class ContextObjectsSnapshot(BaseModel):
    """Object-based context snapshot for agent execution (section objects)."""
    
    # Core sections (objects)
    schemas: 'TablesSchemaContext | None' = None
    files: 'FilesSchemaContext | None' = None
    # Placeholders for future object sections
    messages: Any | None = None
    memories: Any | None = None
    widgets: Any | None = None
    queries: Any | None = None
    instructions: Any | None = None
    code: Any | None = None
    resources: Any | None = None
    observations: Any | None = None
    entities: Any | None = None
    
    # Metadata
    metadata: ContextMetadata


class SchemaContextConfig(BaseModel):
    """Configuration for schema context building."""
    active_only: bool = True
    with_stats: bool = True
    top_k: Optional[int] = None


class InstructionContextConfig(BaseModel):
    """Configuration for instruction context building."""
    
    # InstructionContextBuilder.load_instructions() parameters
    status: str = "published"
    category: Optional[str] = None


class MessageContextConfig(BaseModel):
    """Configuration for message context building."""
    
    # MessageContextBuilder.build_context() parameters  
    max_messages: int = 20
    role_filter: Optional[List[str]] = None


class MemoryContextConfig(BaseModel):
    """Configuration for memory context building."""
    
    # MemoryContextBuilder.build_context() parameters
    max_memories: int = 10


class WidgetContextConfig(BaseModel):
    """Configuration for widget context building."""
    
    # WidgetContextBuilder.build_context() parameters
    max_widgets: int = 5
    status_filter: Optional[List[str]] = None
    include_data_preview: bool = True


class ResourceContextConfig(BaseModel):
    """Configuration for resource context building."""
    
    # ResourceContextBuilder.build_context() parameters
    # Note: data_sources is passed from report, not configured here
    pass


class EntitiesContextConfig(BaseModel):
    """Configuration for entities context building."""

    keywords: List[str] = []
    types: Optional[List[str]] = None
    top_k: int = 10
    require_data_source_association: bool = True


class CodeContextConfig(BaseModel):
    """Configuration for code context building."""
    
    # CodeContextBuilder has complex methods, no simple build_context()
    # Keeping minimal config for future use
    pass

class ResearchContextConfig(BaseModel):
    """Configuration for research context building."""
    
    max_findings: int = 10
    include_sources: bool = True
    deduplicate: bool = True
    relevance_threshold: float = 0.5


class ContextBuildSpec(BaseModel):
    """Specification for what context to build."""
    
    # Core sections (what to include)
    include_schemas: bool = True
    include_messages: bool = True
    include_memories: bool = True
    include_widgets: bool = True
    include_instructions: bool = True
    include_entities: bool = True
    include_code: bool = False
    include_resource: bool = False
    include_research_context: bool = True
    
    # Builder-specific configurations
    schema_config: Optional[SchemaContextConfig] = None
    instruction_config: Optional[InstructionContextConfig] = None
    message_config: Optional[MessageContextConfig] = None
    memory_config: Optional[MemoryContextConfig] = None
    widget_config: Optional[WidgetContextConfig] = None
    resource_config: Optional[ResourceContextConfig] = None
    entities_config: Optional[EntitiesContextConfig] = None
    code_config: Optional[CodeContextConfig] = None
    research_config: Optional[ResearchContextConfig] = None
    
    # Global rendering preferences
    format_for_prompt: bool = True
    max_total_tokens: Optional[int] = None
    compress_content: bool = False
    
    # Research context data (passed from agent)
    research_context: Optional[Dict[str, Any]] = None
    
    # Backwards compatibility for legacy filters
    message_role_filter: Optional[List[str]] = None
    widget_status_filter: Optional[List[str]] = None
    max_messages: Optional[int] = None
    max_memories: Optional[int] = None
    max_widgets: Optional[int] = None
    
    def model_post_init(self, __context) -> None:
        """Handle backwards compatibility after model initialization."""
        # Migrate legacy filters to new config objects
        if self.message_role_filter and not self.message_config:
            self.message_config = MessageContextConfig(roles=self.message_role_filter)
        
        if self.widget_status_filter and not self.widget_config:
            self.widget_config = WidgetContextConfig(status_filter=self.widget_status_filter)
        
        # Migrate legacy limits
        if self.max_messages and self.message_config:
            self.message_config.max_messages = self.max_messages
        elif self.max_messages and not self.message_config:
            self.message_config = MessageContextConfig(max_messages=self.max_messages)
            
        if self.max_memories and self.memory_config:
            self.memory_config.max_memories = self.max_memories
        elif self.max_memories and not self.memory_config:
            self.memory_config = MemoryContextConfig(max_memories=self.max_memories)
            
        if self.max_widgets and self.widget_config:
            self.widget_config.max_widgets = self.max_widgets
        elif self.max_widgets and not self.widget_config:
            self.widget_config = WidgetContextConfig(max_widgets=self.max_widgets)


class ContextSection(BaseModel):
    """Individual context section with metadata."""
    
    name: str
    content: str
    token_count: int = 0
    build_time_ms: float = 0
    source_count: int = 0  # Number of items in this section
    cached: bool = False