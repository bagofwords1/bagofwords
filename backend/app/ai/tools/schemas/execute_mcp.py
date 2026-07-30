from typing import Optional, Dict, Any, List
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
    title: Optional[str] = Field(
        default=None,
        description=(
            "A short, human-readable label for this action in the active voice — "
            "3-6 words naming the connection/service and what you're doing, e.g. "
            "'Searching Notion for churned customers' or 'Fetching open Jira "
            "tickets'. Shown to the user as the live title while the tool runs, "
            "instead of the raw tool name. Do NOT include ids or the underlying "
            "tool_name; write it for a non-technical reader."
        ),
    )


class ExecuteMCPOutput(BaseModel):
    success: bool = Field(..., description="Whether the tool execution succeeded.")
    content_type: Optional[str] = Field(default=None, description="Type of data returned: tabular, text, json.")
    file_id: Optional[str] = Field(
        default=None,
        description=(
            "File record ID of the materialized result. Every successful call "
            "produces one. Pass it to create_data or write_csv via "
            "source_file_ids instead of working from the preview."
        ),
    )
    file_name: Optional[str] = Field(default=None, description="Name of the materialized file.")
    media: Optional[str] = Field(
        default=None,
        description=(
            "What the materialized file holds: 'csv' (a real table), 'json', "
            "'text', or 'none' when the result could not be saved. Picks the "
            "reader for whoever consumes the file."
        ),
    )
    materialization_error: Optional[str] = Field(
        default=None,
        description="Why the result could not be written to a file. When set, no file exists — do not assume one does.",
    )
    row_count: Optional[int] = Field(default=None, description="Number of rows if tabular data.")
    tabular_path: Optional[str] = Field(default=None, description="Key path the rows were unwrapped from when the table arrived inside an envelope, e.g. 'data'.")
    candidate_paths: Optional[List[str]] = Field(
        default=None,
        description="Other key paths in the payload that also held records. Re-run against one of these if the wrong table was picked.",
    )
    record_shape: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Column names and inferred types of the materialized rows, so a consumer can write its reader without guessing.",
    )
    parse_skipped: Optional[bool] = Field(
        default=None,
        description="True when the response was too large to parse in-process. The file holds every byte, but its structure was not inspected — no tabular_path or record_shape is available.",
    )
    size_chars: Optional[int] = Field(
        default=None,
        description="Character length of an unparsed response.",
    )
    parsed_from_text: Optional[str] = Field(
        default=None,
        description="Set when a result labelled 'text' turned out to be csv/ndjson/json and was parsed into rows.",
    )
    result_metadata: Optional[Dict[str, Any]] = Field(default=None, description="Non-table values that sat beside the rows in the envelope, such as pagination cursors and totals.")
    preview: Optional[Any] = Field(default=None, description="Preview of the result (first rows for tabular, truncated text, etc.).")
    error_message: Optional[str] = Field(default=None, description="Error message if execution failed.")
