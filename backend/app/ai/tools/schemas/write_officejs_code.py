from typing import Optional, List, Any
from pydantic import BaseModel, Field


class WriteOfficeJsCodeInput(BaseModel):
    code: str = Field(
        ...,
        description=(
            "Office.js code body. Will be wrapped in Excel.run(async ctx => { ... }) in the taskpane. "
            "Use ctx.workbook, ctx.sync(), range.load(), etc. Return a small JSON-serializable value if "
            "useful to the planner (e.g. a computed sum). Do not return whole ranges."
        ),
    )
    description: Optional[str] = Field(
        default=None,
        description="One-line description of what the code does (surfaced in UI).",
    )


class WriteOfficeJsCodeOutput(BaseModel):
    success: bool = Field(..., description="Whether the code executed without error.")
    return_value: Optional[Any] = Field(default=None, description="Value returned from the async code body, if any.")
    error: Optional[str] = Field(default=None, description="Error message when success=false.")
    logs: Optional[List[str]] = Field(default=None, description="Captured console.log output.")
    ranges_touched: Optional[List[str]] = Field(default=None, description="Best-effort list of range addresses written.")
