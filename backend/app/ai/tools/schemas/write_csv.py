from typing import Optional, List, Dict, Any
from pydantic import Field, BaseModel


class WriteCsvInput(BaseModel):
    user_prompt: str = Field(
        ...,
        description="Description of what data to generate or how to transform raw data. E.g. 'Create a table with columns A, B, C' or 'Parse logs, extract timestamp, level, message. Filter ERROR only.'"
    )
    title: Optional[str] = Field(
        default=None,
        description="Title for the generated data visualization. Also used to name the saved CSV file, so prefer a short, descriptive title (e.g. 'Software Houses Income Tax 2025'). If not provided, a default title and filename will be used."
    )
    tables_by_source: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional list of tables to resolve for context (same format as inspect_data)."
    )
    source_file_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "File IDs this code should read, e.g. the file_id returned by "
            "execute_mcp. Always pass it when transforming a tool result: it "
            "pins the generated code to those files and tells it the exact "
            "path, reader and column shape, instead of leaving it to guess "
            "which attached file you meant."
        ),
    )


class WriteCsvOutput(BaseModel):
    success: bool = Field(..., description="Whether CSV generation succeeded.")
    file_id: Optional[str] = Field(default=None, description="The File record ID for the saved CSV.")
    file_name: Optional[str] = Field(default=None, description="Name of the saved file.")
    row_count: Optional[int] = Field(default=None, description="Number of rows in the output.")
    columns: Optional[List[str]] = Field(default=None, description="Column names in the output.")
    error_message: Optional[str] = Field(default=None, description="Error message if failed.")
    code: Optional[str] = Field(default=None, description="The generated code.")
    execution_log: Optional[str] = Field(default=None, description="Execution output log.")
