from typing import Optional, List, Dict, Any
from pydantic import Field, BaseModel


class WriteCsvInput(BaseModel):
    user_prompt: str = Field(
        ...,
        description="Description of what data to generate or how to transform raw data. E.g. 'Create a table with columns A, B, C' or 'Parse logs, extract timestamp, level, message. Filter ERROR only.'"
    )
    tables_by_source: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional list of tables to resolve for context (same format as inspect_data)."
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
