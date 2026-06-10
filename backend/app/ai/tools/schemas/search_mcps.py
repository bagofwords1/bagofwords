from typing import Optional, List, Dict, Any
from pydantic import Field, BaseModel


class SearchMCPsInput(BaseModel):
    query: Optional[str] = Field(
        default=None,
        description=(
            "Optional relevance hint to rank tools by name/description (not a hard "
            "filter). Plain text is matched fuzzily by word; wildcards are supported "
            "(e.g. 'search_*', '*contact*'). Omit to list all tools; if nothing "
            "matches, all tools are returned so you always get schemas."
        ),
    )
    connection_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional list of connection IDs to scope the search to specific MCP/API connections."
    )


class ToolPreview(BaseModel):
    name: str
    description: str
    connection_id: str
    connection_name: str
    connection_type: str
    input_schema: Optional[Dict[str, Any]] = None


class SearchMCPsOutput(BaseModel):
    tools: List[ToolPreview] = Field(default=[], description="List of matching tools with full schemas.")
    total_count: int = Field(default=0, description="Total number of matching tools.")
