from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class CreateDashboardInput(BaseModel):
    """Input for create_dashboard tool.

    - prompt: user's goal/instruction for the dashboard
    - report_title: optional title for the dashboard/report
    - widget_ids: if provided and use_all_widgets is False, limit to these widgets
    - use_all_widgets: when True, ignore widget_ids and use all available widgets from runtime context
    - steps_context: optional pre-summarized analysis steps if not passing Step objects in runtime
    - previous_messages: optional conversation history override; if not provided, tool will use runtime context
    """

    prompt: str
    report_title: Optional[str] = None
    widget_ids: Optional[List[str]] = None
    use_all_widgets: bool = True
    steps_context: Optional[str] = None
    previous_messages: Optional[str] = None


class CreateDashboardOutput(BaseModel):
    """Final structured dashboard layout returned by the tool.

    The layout follows the structure produced by DashboardDesigner:
    {"prefix": str, "blocks": list[dict], "end_message": str}
    """

    layout: Dict[str, Any] = Field(..., description="Layout with keys: prefix, blocks, end_message")
    report_title: Optional[str] = None


