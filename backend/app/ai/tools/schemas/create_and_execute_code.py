from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field


class CreateAndExecuteCodeInput(BaseModel):
    """Input for generating code from the current step's data model and executing it.

    The tool reads the data_model from AgentV2 state (runtime_ctx). No persistence here.
    """
    prompt: str = Field(..., description="User prompt")



class CreateAndExecuteCodeOutput(BaseModel):
    """Execution results with generated code and formatted preview suitable for widgets."""

    success: bool = Field(..., description="Whether execution succeeded")
    code: str = Field(..., description="Generated code that was executed")
    data_preview: Dict[str, Any] = Field(default_factory=dict, description="Preview rows/columns")
    stats: Dict[str, Any] = Field(default_factory=dict, description="DF info, counts, memory, etc")
    execution_log: Optional[str] = Field(default=None, description="Captured stdout/logs during execution")


