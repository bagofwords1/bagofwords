from typing import Optional
from pydantic import BaseModel, Field


class ReadExcelAsCsvInput(BaseModel):
    sheet_name: str = Field(
        ...,
        description="Exact sheet tab name, e.g. 'Sheet1'.",
    )
    range: str = Field(
        ...,
        description="A1-notation range to read as CSV, e.g. 'A1:F100'. Pass the full table range including header row if present.",
    )
    max_rows: int = Field(
        default=10000,
        description="Hard cap on rows returned. Extra rows are dropped and truncated=true is set.",
        ge=1,
        le=100000,
    )


class ReadExcelAsCsvOutput(BaseModel):
    success: bool
    csv: str = ""
    row_count: int = 0
    col_count: int = 0
    truncated: bool = False
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    error: Optional[str] = None
