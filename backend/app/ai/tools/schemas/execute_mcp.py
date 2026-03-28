from typing import Optional, Dict, Any
from pydantic import Field, BaseModel


class ExecuteMCPInput(BaseModel):
    connection_id: str = Field(
        ...,
        description="The ID of the MCP or Custom API connection to use."
    )
    tool_name: str = Field(
        ...,
        description="The name of the tool to invoke."
    )
    arguments: Dict[str, Any] = Field(
        default={},
        description="Arguments to pass to the tool, matching the tool's input schema."
    )


class ExecuteMCPOutput(BaseModel):
    success: bool = Field(..., description="Whether the tool execution succeeded.")
    content_type: Optional[str] = Field(default=None, description="Type of data returned: tabular, text, json.")
    file_id: Optional[str] = Field(default=None, description="If tabular data was materialized to CSV, the File record ID.")
    file_name: Optional[str] = Field(default=None, description="Name of the materialized CSV file.")
    row_count: Optional[int] = Field(default=None, description="Number of rows if tabular data.")
    preview: Optional[Any] = Field(default=None, description="Preview of the result (first rows for tabular, truncated text, etc.).")
    error_message: Optional[str] = Field(default=None, description="Error message if execution failed.")
