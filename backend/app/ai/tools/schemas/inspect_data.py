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

class InspectDataOutput(BaseModel):
    execution_log: str = Field(..., description="The standard output (stdout) from the inspection code.")
    success: bool = Field(..., description="Whether the inspection code ran without fatal errors.")
    error_message: Optional[str] = Field(default=None, description="Error message if execution failed.")
