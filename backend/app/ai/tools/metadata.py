from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel, Field


class ToolMetadata(BaseModel):
    """Comprehensive tool metadata for registry and discovery."""
    
    name: str = Field(..., description="Unique tool identifier")
    description: str = Field(..., description="Human-readable tool description")
    category: Literal["research", "action", "both"] = Field(default="action", description="Tool access category")
    version: str = Field(default="1.0.0", description="Tool version for compatibility")
    
    # Schema information
    input_schema: Optional[Dict[str, Any]] = Field(default=None, description="JSON schema for tool input")
    output_schema: Optional[Dict[str, Any]] = Field(default=None, description="JSON schema for tool output")
    
    # Access control
    enabled_for_orgs: Optional[List[str]] = Field(default=None, description="Org whitelist (None = all)")
    required_permissions: List[str] = Field(default_factory=list, description="Required user permissions")
    
    # Execution policies
    max_retries: int = Field(default=2, description="Default retry attempts")
    timeout_seconds: int = Field(default=30, description="Default execution timeout")
    idempotent: bool = Field(default=False, description="Safe to retry without side effects")
    is_active: bool = Field(default=True, description="If false, hide from catalog and disallow execution")
    observation_policy: Optional[Literal["never", "on_trigger", "always"]] = Field(
        default="on_trigger", description="History persistence policy"
    )
    
    # Mode access control
    allowed_modes: Optional[List[str]] = Field(
        default=None,
        description="Modes where tool is available (None = all modes). E.g., ['training'] for training-only tools."
    )

    # Discovery and UI
    tags: List[str] = Field(default_factory=list, description="Searchable tags")
    examples: List[Dict[str, Any]] = Field(default_factory=list, description="Usage examples")
    
    class Config:
        extra = "forbid"  # Strict schema validation