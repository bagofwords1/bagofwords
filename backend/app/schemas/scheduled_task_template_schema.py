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
        # The reader enables this with one click and never tunes it, so every
        # judgement call — what counts as notable, what to compare against,
        # what to do when the data stops — is settled here, and the digest
        # keeps the same shape from week to week.
        prompt_content=(
            "Write the weekly data digest for the data available to this report.\n\n"
            "Preparation: inspect which data sources and tables are attached; if none are, "
            "search for relevant agents or data sources you can use. Work only from tables "
            "that have a date or timestamp column.\n\n"
            "Time window: the 7 days ending at the latest date present in the data (state that "
            "range in the title). If a source has no rows in the last 7 calendar days, use the "
            "last 7 days that do have data and say so plainly instead of reporting a gap. "
            "Compare every figure to the previous 7-day window and to the average of the 4 "
            "windows before it; never show a number without its comparison.\n\n"
            "What counts as notable: a change of more than 10% versus the previous window, or "
            "more than 2 standard deviations from the 4-window average. Mention nothing else.\n\n"
            "Always use this structure, in this order, with the section titles translated "
            "into the language you write the digest in:\n"
            "1. Key numbers — 3 to 5 core metrics (e.g. volume, revenue, new records, active "
            "entities) with the change versus the previous window.\n"
            "2. Up / Down — the 5 entities that rose most and the 5 that fell most, with their "
            "percentage change. Pick the dimension yourself: a categorical column with roughly "
            "5 to 50 distinct values (customer, product, region, category). Skip this section "
            "if no such dimension exists.\n"
            "3. Worth attention — 2 to 3 findings a reader should act on or look into. "
            "One finding per underlying event: if a single change explains several metrics "
            "(the same invoices moving revenue, count and average), report it once.\n"
            "4. Not covered — sources or tables you skipped and why, one line each.\n\n"
            "Quiet weeks: if nothing crosses the notable threshold, write a single short "
            "paragraph saying the week was stable, with the key numbers, and stop.\n\n"
            "Style: write in the language of the organization's instructions, or of the data "
            "if that is clearer; short sentences; one small table or chart per section at most, "
            "and only where it adds something the text cannot. Skip trends of one data point. "
            "Never fail the run over one broken source — note it under Not covered and go on."
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
