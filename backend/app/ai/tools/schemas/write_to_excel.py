from typing import Optional, List, Dict, Any
from pydantic import Field, BaseModel


class WriteToExcelInput(BaseModel):
    columns: List[Dict[str, Any]] = Field(
        ...,
        description="Column definitions. Each dict must have 'field' (column key) and 'headerName' (display name)."
    )
    rows: List[Dict[str, Any]] = Field(
        ...,
        description="Row data. Each dict maps column 'field' keys to cell values."
    )
    title: Optional[str] = Field(
        default=None,
        description="Optional title for the data being written to Excel."
    )


class WriteToExcelOutput(BaseModel):
    success: bool = Field(..., description="Whether the taskpane confirmed the data was written to the spreadsheet.")
    row_count: Optional[int] = Field(default=None, description="Number of rows written.")
    column_count: Optional[int] = Field(default=None, description="Number of columns written.")
    wrote_to: Optional[str] = Field(default=None, description="Address of the range that was written (e.g. 'Sheet1!B5:C7').")
    error: Optional[str] = Field(default=None, description="Error message when success=false.")
