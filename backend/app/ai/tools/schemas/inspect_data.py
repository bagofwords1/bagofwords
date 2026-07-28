from typing import Optional, List, Any, Dict
from pydantic import Field, BaseModel

class InspectDataInput(BaseModel):
    user_prompt: str = Field(
        ...,
        description="Description of what to inspect. E.g. 'Check distinct values in status column', 'Preview the uploaded Excel file', 'Check for nulls in revenue'."
    )
    tables_by_source: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional list of tables to resolve and load for inspection. If checking uploaded files, this can be omitted."
    )
    source_file_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "File IDs to inspect, e.g. the file_id returned by execute_mcp. "
            "Pass it whenever you mean a specific file: it pins the generated "
            "code to those files and tells it the exact path, reader and "
            "column shape, instead of leaving it to guess which attachment you meant."
        ),
    )

class InspectDataOutput(BaseModel):
    execution_log: str = Field(..., description="The standard output (stdout) from the inspection code.")
    success: bool = Field(..., description="Whether the inspection code ran without fatal errors.")
    error_message: Optional[str] = Field(default=None, description="Error message if execution failed.")
