from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class CallToolInput(BaseModel):
    name: str = Field(..., description="The exact tool name returned by search_tools.")
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments matching the input_schema search_tools returned for this tool.",
    )
    title: Optional[str] = Field(
        default=None,
        description="A short, human-readable label for this action, e.g. 'Fetching open invoices'.",
    )


class CallToolOutput(BaseModel):
    success: bool = False
    message: Optional[str] = None
