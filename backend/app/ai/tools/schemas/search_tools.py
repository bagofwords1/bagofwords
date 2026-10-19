from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SearchToolsInput(BaseModel):
    query: Optional[str] = Field(
        default=None,
        description=(
            "What to look for in tool names and descriptions. Plain words are "
            "ranked by how many match ('sales report'); wildcards match a name "
            "pattern ('jira_*', '*invoice*'); a regex is detected by its special "
            "characters or written as /pattern/ ('^get_(sales|orders)'). Omit to "
            "list every searchable tool. Write it in English: tool names and "
            "descriptions usually are."
        ),
    )
    limit: int = Field(default=10, ge=1, le=50, description="Most tools to return, with full schemas.")
    title: Optional[str] = Field(
        default=None,
        description="A short, human-readable label for this action, e.g. 'Finding invoice tools'.",
    )


class FoundTool(BaseModel):
    name: str
    description: str = ""
    connection_name: str = ""
    input_schema: Optional[Dict[str, Any]] = None
    policy: str = "allow"


class SearchToolsOutput(BaseModel):
    tools: List[FoundTool] = Field(default_factory=list)
    total_count: int = 0
