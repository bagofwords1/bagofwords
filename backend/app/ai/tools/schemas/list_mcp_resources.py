from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ListMcpResourcesInput(BaseModel):
    connection_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional ID (or name) of a specific MCP connection to list resources from. "
            "Omit to list resources across all MCP connections attached to the current data sources."
        ),
    )


class ListMcpResourcesOutput(BaseModel):
    resources: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Resources and URI templates. Each item has: uri (or uri_template), name, "
            "description, mime_type, is_template, connection_id, connection_name."
        ),
    )
    total_count: int = Field(default=0, description="Number of resources/templates returned.")
    truncated: bool = Field(default=False, description="True if the result was capped.")
    errors: List[str] = Field(default_factory=list, description="Per-connection errors, if any.")
