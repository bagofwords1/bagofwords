from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

from .create_data_model import DataModel


class CreateWidgetInput(BaseModel):
    """Input for end-to-end widget creation.

    The tool will generate a data_model, then code, then execute it to populate the widget.
    """

    widget_title: str = Field(..., description="Title for the widget to create")
    user_prompt: str = Field(..., description="Original user instruction")
    interpreted_prompt: str = Field(..., description="LLM-interpreted, clarified version of the user prompt")


class CreateWidgetOutput(BaseModel):
    """Output of end-to-end widget creation."""

    success: bool = Field(..., description="Whether the overall operation succeeded")
    data_model: Optional[DataModel] = Field(default=None, description="Final normalized data model")
    code: Optional[str] = Field(default=None, description="Final code used to compute widget data")
    widget_data: Optional[Dict[str, Any]] = Field(default=None, description="Rendered data structure for the widget")
    data_preview: Optional[Dict[str, Any]] = Field(default=None, description="Privacy-safe preview for UI/LLM")
    stats: Optional[Dict[str, Any]] = Field(default=None, description="Execution stats/metadata")
    execution_log: Optional[str] = Field(default=None, description="Execution log or trace output if available")


