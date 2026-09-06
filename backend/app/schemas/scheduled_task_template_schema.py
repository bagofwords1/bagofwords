from typing import Dict, List, Optional

from pydantic import BaseModel


class ScheduledTaskTemplateDefinition(BaseModel):
    """A built-in scheduled-task template, defined in code (never stored).

    Enabling one creates a regular ScheduledPrompt stamped with `key`; the
    registry is the single source of truth for the prompt text and defaults.
    """
    key: str
    version: str = "1.0"
    title: str
    description: str
    prompt_content: str
    default_cron: str  # 5-field cron, fires in the org timezone
    spawn_new_report: bool = False
    tags: List[str] = []
    icon: Optional[str] = None  # heroicons name for the card


class ScheduledTaskTemplateEnableRequest(BaseModel):
    """Optional customizations applied when a template is first enabled.

    Omitted fields fall back to the template defaults (all usable data sources,
    the shipped cron). Ignored when re-enabling a paused instance — that resumes
    the existing task, which is fully editable like any other.
    """
    data_source_ids: Optional[List[str]] = None
    cron_schedule: Optional[str] = None


class ScheduledTaskTemplateState(BaseModel):
    """A registry entry annotated with the calling user's install state."""
    key: str
    title: str
    description: str
    # Shown in the details view so a user can read exactly what the task will
    # do before enabling it.
    prompt_content: str
    default_cron: str
    spawn_new_report: bool = False
    tags: List[str] = []
    icon: Optional[str] = None
    enabled: bool = False   # a row exists and is active
    paused: bool = False    # a row exists but is inactive (history kept)
    scheduled_prompt_id: Optional[str] = None
    report_id: Optional[str] = None
    # The live row's cron — may diverge from default_cron after the user
    # edits the schedule.
    cron_schedule: Optional[str] = None
    duplicate_count: int = 0


SCHEDULED_TASK_TEMPLATES: Dict[str, ScheduledTaskTemplateDefinition] = {
    "weekly-data-digest": ScheduledTaskTemplateDefinition(
        key="weekly-data-digest",
        title="Weekly data digest",
        description="A concise weekly summary of what changed across your connected data, every Monday morning.",
        prompt_content=(
            "Produce a concise weekly digest of the data available to this report. "
            "First inspect which data sources and tables are attached; if none are attached, "
            "search for relevant agents or data sources you can use. "
            "For each source with time-based data, summarize notable changes over the past 7 days — "
            "volumes, new records, top movers. If a source has no usable recent data, say so briefly "
            "and move on rather than failing. End with 2-3 highlights worth attention. "
            "Keep it skimmable: short sections, and small tables or charts only where they add signal."
        ),
        default_cron="0 9 * * 1",
        spawn_new_report=True,
        tags=["digest"],
        icon="heroicons:chart-bar",
    ),
    "monthly-trends-recap": ScheduledTaskTemplateDefinition(
        key="monthly-trends-recap",
        title="Monthly trends recap",
        description="Month-over-month trends from your data, with callbacks to previous months, on the 1st of each month.",
        prompt_content=(
            "Create a monthly trends recap from whatever data sources this report has access to. "
            "Compare the month that just ended to prior months where the data allows, and call back "
            "to observations from your previous runs when relevant. Gracefully skip sources without "
            "meaningful monthly granularity, noting which ones you skipped and why. "
            "Close with a short list of trends to watch next month."
        ),
        default_cron="0 9 1 * *",
        spawn_new_report=False,
        tags=["trends"],
        icon="heroicons:arrow-trending-up",
    ),
}


def get_scheduled_task_template(key: str) -> Optional[ScheduledTaskTemplateDefinition]:
    return SCHEDULED_TASK_TEMPLATES.get(key)


def list_scheduled_task_templates() -> List[ScheduledTaskTemplateDefinition]:
    return list(SCHEDULED_TASK_TEMPLATES.values())
