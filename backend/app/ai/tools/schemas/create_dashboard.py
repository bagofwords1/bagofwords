from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


class CreateDashboardInput(BaseModel):
    """Input for create_dashboard tool.

    - prompt: user's goal/instruction for the dashboard
    - report_title: optional title for the dashboard/report
    - visualization_ids: if provided and use_all_visualizations is False, limit to these IDs
    - use_all_visualizations: when True, ignore visualization_ids and use all available visualizations from runtime context
    - steps_context: optional pre-summarized analysis steps if not passing Step objects in runtime
    - previous_messages: optional conversation history override; if not provided, tool will use runtime context
    """

    prompt: str
    report_title: Optional[str] = None
    visualization_ids: Optional[List[str]] = None
    use_all_visualizations: bool = True
    steps_context: Optional[str] = None
    previous_messages: Optional[str] = None
    create_text_widgets: bool = True


class SemanticColumnOutput(BaseModel):
    """A column within a column_layout block."""
    span: int = 6
    children: List["SemanticBlockOutput"] = Field(default_factory=list)


class SemanticBlockOutput(BaseModel):
    """A semantic block as output by the LLM (before layout computation)."""
    
    type: Literal["visualization", "text", "text_widget", "card", "section", "column_layout"]
    
    # For visualization blocks
    visualization_id: Optional[str] = None
    
    # For text blocks
    content: Optional[str] = None
    variant: Optional[Literal["title", "subtitle", "paragraph", "insight", "summary"]] = None
    
    # For card/section blocks
    title: Optional[str] = None
    subtitle: Optional[str] = None
    children: Optional[List["SemanticBlockOutput"]] = None
    
    # For column_layout blocks
    columns: Optional[List[SemanticColumnOutput]] = None
    
    # Semantic hints
    role: Optional[str] = None
    importance: Optional[Literal["primary", "secondary", "tertiary"]] = None
    size: Optional[Literal["xs", "small", "medium", "large", "xl", "full"]] = None
    section: Optional[str] = None
    order: Optional[int] = None
    
    # View overrides
    view_overrides: Optional[Dict[str, Any]] = None


# Enable forward refs for nested children
SemanticColumnOutput.model_rebuild()
SemanticBlockOutput.model_rebuild()


class CreateDashboardOutput(BaseModel):
    """Final structured dashboard layout returned by the tool.

    - semantic_blocks: the raw semantic blocks from LLM (without coordinates)
    - layout: computed layout with x/y/width/height for each block
    """

    semantic_blocks: List[SemanticBlockOutput] = Field(
        default_factory=list,
        description="Raw semantic blocks from LLM before layout computation"
    )
    layout: Dict[str, Any] = Field(..., description="Computed layout with blocks containing x/y/width/height")
    report_title: Optional[str] = None
