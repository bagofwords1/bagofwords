from typing import Optional
from pydantic import BaseModel, Field


class ReadMcpResourceInput(BaseModel):
    uri: str = Field(
        ...,
        description=(
            "The URI of the resource to read (e.g. 'pulse://rules'), taken from list_mcp_resources "
            "or built by filling in a URI template."
        ),
    )
    connection_id: Optional[str] = Field(
        default=None,
        description=(
            "ID (or name) of the MCP connection that exposes this resource. "
            "Optional when exactly one MCP connection is attached; required when there are several."
        ),
    )


class ReadMcpResourceOutput(BaseModel):
    success: bool = Field(..., description="Whether the resource was read successfully.")
    content: Optional[str] = Field(default=None, description="The resource content (text; binary rendered as a placeholder).")
    mime_type: Optional[str] = Field(default=None, description="MIME type of the resource, if reported.")
    uri: Optional[str] = Field(default=None, description="The URI that was read.")
    connection_name: Optional[str] = Field(default=None, description="Name of the connection the resource was read from.")
    truncated: bool = Field(default=False, description="True if the content was truncated to fit.")
    session_file_id: Optional[str] = Field(default=None, description="If the resource was a binary file (xlsx/pdf/exported sheet), its bytes are materialized into a session file with this id — pass it to inspect_data / create_data / read_excel_as_csv to analyze, exactly like an uploaded file.")
    error_message: Optional[str] = Field(default=None, description="Error message if the read failed.")
