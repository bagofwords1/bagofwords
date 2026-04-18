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
    description: str = Field(
        ...,
        min_length=1,
        description=(
            "Short, past-tense-friendly label shown in the UI — e.g. "
            "\"Check headers at selected cell\" or \"Add a pie chart to Sheet1\". or \"Turn A1:C10 into a table\"."
            "Required. One sentence, no trailing period."
        ),
    )


class WriteOfficeJsCodeOutput(BaseModel):
    success: bool = Field(..., description="Whether the code executed without error.")
    return_value: Optional[Any] = Field(default=None, description="Value returned from the async code body, if any.")
    error: Optional[str] = Field(default=None, description="Error message when success=false.")
    logs: Optional[List[str]] = Field(default=None, description="Captured console.log output.")
    ranges_touched: Optional[List[str]] = Field(default=None, description="Best-effort list of range addresses written.")
