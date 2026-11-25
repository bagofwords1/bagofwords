"""WhatsApp answer guidance when a chart is sent.

The chart image WhatsApp gets is one series, with value labels on bar charts
only. For a single-series bar chart the planner gives a short takeaway
instead of relisting the values; for any other chart it still states what the
picture can't show. The "include every value" rule is kept for small table
results, which get no chart and no link. Other channels' guidance is
untouched.
"""
from types import SimpleNamespace

import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3


def _directive(platform):
    return PromptBuilderV3._platform_system_directives(SimpleNamespace(external_platform=platform))


def _sentences_mentioning(text, needle):
    return [line for line in text.lower().split("\n") if needle in line]


def test_bar_chart_answer_is_a_takeaway_not_the_values_again():
    rules = _sentences_mentioning(_directive("whatsapp"), "chart image shows one series")
    assert len(rules) == 1
    rule = rules[0]
    assert "one series" in rule
    assert "bar_chart" in rule and "do not list" in rule


def test_other_charts_still_spell_out_what_the_image_cannot_show():
    """The image is one series with value labels on bars only: a line, a
    multi-series or a pie chart's numbers must still reach the user in text."""
    rule = _sentences_mentioning(_directive("whatsapp"), "chart image shows one series")[0]
    assert "any other chart" in rule
    assert "the other series" in rule


def test_include_every_value_applies_only_to_table_results_without_a_chart():
    rules = _sentences_mentioning(_directive("whatsapp"), "include every value")
    assert rules, "small table results must still be spelled out in text"
    assert all("no chart" in rule for rule in rules)


def test_whatsapp_planner_knows_a_dashboard_link_is_sent_for_it():
    rules = _sentences_mentioning(_directive("whatsapp"), "dashboard")
    assert any("link" in r and "automatically" in r for r in rules)
    # Scoped to the dashboard: source-document links elsewhere stay allowed.
    assert any("never write the dashboard's url" in r for r in rules)
    assert not any("never write a url" in r for r in rules)


@pytest.mark.parametrize("platform", ["slack", "teams", "google_chat"])
def test_other_channels_do_not_get_the_whatsapp_chart_rule(platform):
    assert not _sentences_mentioning(_directive(platform), "chart image shows one series")
