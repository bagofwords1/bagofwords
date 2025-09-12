from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class CreateDashboardInput(BaseModel):
    """Input for create_dashboard tool.

    - prompt: user's goal/instruction for the dashboard
    - report_title: optional title for the dashboard/report
    - visualization_ids: if provided and use_all_visualizations is False, limit to these IDs
    - use_all_visualizations: when True, ignore visualization_ids and use all available visualizations from runtime context
    - steps_context: optional pre-summarized analysis steps if not passing Step objects in runtime
    - previous_messages: optional conversation history override; if not provided, tool will use runtime context
    """

    prompt: str
    report_title: Optional[str] = None
    visualization_ids: Optional[List[str]] = None
    use_all_visualizations: bool = True
    steps_context: Optional[str] = None
    previous_messages: Optional[str] = None
    create_text_widgets: bool = True


class CreateDashboardOutput(BaseModel):
    """Final structured dashboard layout returned by the tool.

    The layout contains only blocks:
    {"blocks": list[dict]}
    """

    layout: Dict[str, Any] = Field(..., description="Layout with a single key: blocks")
    report_title: Optional[str] = None


