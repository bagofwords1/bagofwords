from typing import ClassVar, List, Optional, Dict, Any
from pydantic import BaseModel
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape


class ToolExecutionItem(BaseModel):
    execution_number: int
    tool_name: str
    tool_input: Dict[str, Any]
    timestamp: str
    observation: Dict[str, Any] = {}


class WidgetUpdateItem(BaseModel):
    widget_id: int
    timestamp: str
    data: Dict[str, Any]


class StepUpdateItem(BaseModel):
    step_id: int
    timestamp: str
    data: Dict[str, Any]


class ObservationsSection(ContextSection):
    tag_name: ClassVar[str] = "recent_tool_executions"

    execution_count: int = 0
    tool_observations: List[ToolExecutionItem] = []
    widget_updates: List[WidgetUpdateItem] = []
    step_updates: List[StepUpdateItem] = []
    artifacts: Dict[str, Any] = {}

    def render(self) -> str:
        lines: List[str] = []
        for obs in self.tool_observations[-5:]:
            obs_lines = []
            summary = obs.observation.get("summary") if obs.observation else None
            if summary:
                obs_lines.append(xml_escape(summary))
                error = obs.observation.get("error")
                if error:
                    if isinstance(error, dict) and error.get("message"):
                        obs_lines.append(f"Error: {xml_escape(error['message'])}")
                    elif isinstance(error, str):
                        obs_lines.append(f"Error: {xml_escape(error)}")
            content = "\n".join(obs_lines)
            lines.append(xml_tag("tool_execution", content, {"tool": obs.tool_name}))

        # Widgets summary (optional)
        if self.widget_updates:
            lines.append(xml_tag("widgets_created_or_updated", str(len(self.widget_updates))))

        return xml_tag(self.tag_name, "\n".join(lines))


