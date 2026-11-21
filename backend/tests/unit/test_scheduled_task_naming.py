import json
from pathlib import Path

import pytest

from app.services.scheduled_task_naming import (
    SCHEDULED_TASK_DEFAULT_TITLES, is_placeholder_title, name_from_prompt,
    synced_report_title, task_display_name,
)

_LOCALES = Path(__file__).resolve().parents[3] / "locales"


def test_every_locale_new_task_title_is_a_placeholder():
    """The New-task flow titles the host report with scheduled.defaultTitle; every
    locale's value must be recognised, or that locale's reports stay generic."""
    catalogs = sorted(_LOCALES.glob("*.json"))
    assert catalogs
    for path in catalogs:
        default = json.loads(path.read_text(encoding="utf-8"))["scheduled"]["defaultTitle"]
        assert is_placeholder_title(default), f"{path.name}: {default!r}"


@pytest.mark.parametrize("prompt", [
    "Check signups",
    "  Check signups\nand also churn  ",
    "word " * 40,
    "x" * 200,
])
def test_name_from_prompt_is_short_nonempty_and_from_the_prompt(prompt):
    name = name_from_prompt({"content": prompt})
    assert name and len(name) <= 61
    assert name.rstrip("…").split()[0] in prompt
    assert not is_placeholder_title(name)


@pytest.mark.parametrize("content", [None, "", "   \n  "])
def test_name_from_empty_prompt_is_none(content):
    assert name_from_prompt({"content": content}) is None


def test_display_name_prefers_title_then_real_report_title_then_prompt():
    prompt = {"content": "Count orders"}
    assert task_display_name("Orders", prompt, "My report") == "Orders"
    assert task_display_name(None, prompt, "My report") == "My report"
    assert task_display_name("", prompt, "Scheduled task") == "Count orders"
    assert task_display_name(None, prompt, "משימה מתוזמנת") == "Count orders"


def test_report_follows_task_only_while_unnamed_or_synced():
    assert synced_report_title("Scheduled task", None, "A") == "A"
    assert synced_report_title("A", "A", "B") == "B"
    assert synced_report_title("Mine", "A", "B") is None
    assert synced_report_title("A", "A", "A") is None
    assert synced_report_title("Scheduled task", None, None) is None
