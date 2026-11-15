"""How a scheduled task and the report it runs in get their names.

A task's name is what users tell automations apart by, so the report that
holds it should carry the same name instead of a generic placeholder such as
"Scheduled task" (what the Automations page's "New task" button creates the
host report with). The rules:

- A task's *display name* is its own title, else the host report's title if
  someone actually named that report, else a short name made from the prompt.
  It is never a generic placeholder.
- Saving a task names the host report after it when the report is still
  unnamed (a placeholder title) or still carries the task's previous name.
  A report the user renamed to something else keeps their name.
"""
from __future__ import annotations

import re
from typing import Optional

# Titles a report carries before anyone named it: the agent's placeholders
# (mirrors app.ai.agent_v2.PLACEHOLDER_REPORT_TITLES) plus the localized
# `scheduled.defaultTitle` the frontend creates a task's host report with, in
# every shipped locale. tests/unit/test_scheduled_task_naming.py keeps the
# latter in sync with locales/*.json.
SCHEDULED_TASK_DEFAULT_TITLES = (
    "scheduled task",
    "tarea programada",
    "משימה מתוזמנת",
    "tâche planifiée",
    "schemalagd uppgift",
    "مهمة مُجدوَلة",
    "задача по расписанию",
    "geplante aufgabe",
    "tarefa agendada",
    "attività pianificata",
)
PLACEHOLDER_TITLES = frozenset(("", "untitled report", "untitled", *SCHEDULED_TASK_DEFAULT_TITLES))

_MAX_NAME_CHARS = 60


def _norm(title: Optional[str]) -> str:
    return " ".join((title or "").split()).casefold()


def is_placeholder_title(title: Optional[str]) -> bool:
    return _norm(title) in PLACEHOLDER_TITLES


def name_from_prompt(prompt) -> Optional[str]:
    """A short name made from the prompt text: its first line, whitespace
    collapsed, cut at a word boundary to at most ~60 characters."""
    content = prompt.get("content") if isinstance(prompt, dict) else prompt
    if not isinstance(content, str):
        return None
    first_line = next((ln for ln in content.splitlines() if ln.strip()), "")
    text = " ".join(first_line.split()).rstrip(" .:;,")
    if not text:
        return None
    if len(text) <= _MAX_NAME_CHARS:
        return text
    cut = text[:_MAX_NAME_CHARS]
    space = cut.rfind(" ")
    if space >= _MAX_NAME_CHARS // 2:
        cut = cut[:space]
    return re.sub(r"[\s.,:;\-–—]+$", "", cut) + "…"


def task_display_name(title: Optional[str], prompt, report_title: Optional[str]) -> Optional[str]:
    """The name a task shows as: its title, else its report's real title,
    else a name from its prompt. None only when there is nothing to go on."""
    own = " ".join((title or "").split())
    if own:
        return own
    if not is_placeholder_title(report_title):
        return report_title.strip()
    return name_from_prompt(prompt)


def synced_report_title(report_title: Optional[str], previous_name: Optional[str],
                        new_name: Optional[str]) -> Optional[str]:
    """The title the host report should take after a task is saved, or None to
    leave it alone. It follows the task while it is unnamed or still carries the
    task's previous name; any other title is the user's and is kept."""
    if not new_name or (report_title or "").strip() == new_name:
        return None
    if is_placeholder_title(report_title):
        return new_name
    if previous_name and _norm(report_title) == _norm(previous_name):
        return new_name
    return None
