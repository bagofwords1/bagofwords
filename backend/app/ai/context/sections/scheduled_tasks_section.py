from typing import List, Optional
from pydantic import BaseModel
from .base import ContextSection, xml_tag, xml_escape


class ScheduledTaskItem(BaseModel):
    id: str
    cron_schedule: str
    cron_label: Optional[str] = None
    prompt_snippet: Optional[str] = None
    last_run_at: Optional[str] = None


class ScheduledTasksSection(ContextSection):
    """Active recurring scheduled tasks for the current report.

    Surfaced so the agent knows what is already scheduled — to avoid creating
    duplicates with create_scheduled_task, and to have the task id available for
    edit_scheduled_task (change prompt/schedule/active state) and
    cancel_scheduled_task.
    """

    tag_name = "scheduled_tasks"

    items: List[ScheduledTaskItem] = []

    def render(self) -> str:
        if not self.items:
            return xml_tag(self.tag_name, "No scheduled tasks on this report")
        task_tags: List[str] = []
        for t in self.items:
            inner = [xml_tag("schedule", xml_escape(t.cron_label or t.cron_schedule))]
            if t.prompt_snippet:
                inner.append(xml_tag("prompt", xml_escape(t.prompt_snippet)))
            if t.last_run_at:
                inner.append(xml_tag("last_run_at", xml_escape(str(t.last_run_at))))
            task_tags.append(xml_tag("task", "\n".join(inner), {"id": t.id}))
        return xml_tag(self.tag_name, "\n".join(task_tags))
