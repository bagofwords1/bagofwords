from typing import Any, List, Optional
from pydantic import BaseModel, Field


class ReadExcelRangeInput(BaseModel):
    sheet_name: str = Field(
        ...,
        description="Exact sheet tab name, e.g. 'Sheet1'.",
    )
    ranges: List[str] = Field(
        ...,
        description="A1-notation ranges to read, e.g. ['A1:C10', 'E1:F5'].",
        min_length=1,
        max_length=20,
    )
    include_formulas: bool = Field(
        default=False,
        description="When true, returns each cell's formula alongside its value.",
    )
    cell_limit: int = Field(
        default=2000,
        description="Hard cap on total cells returned across all ranges. Excess ranges are skipped and truncated=true is set.",
        ge=1,
        le=50000,
    )


class ReadExcelRangeItem(BaseModel):
    address: str
    row_count: int
    col_count: int
    values: List[List[Any]]
    formulas: Optional[List[List[Any]]] = None


class ReadExcelRangeOutput(BaseModel):
    success: bool
    ranges: List[ReadExcelRangeItem] = Field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None
