"""Planner routing for bulk scraping when the org's Sandbox settings are on.

Seen live before this rule existed: with browser and list helpers available
in code, the planner still looped its own browser tools page by page and
wrote every scraped row into submit_<list> itself, so the model read and
wrote all of it. The rule only appears for capabilities the run has.
"""
import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


def _system(**kw):
    return PromptBuilderV3._build_system(PlannerInput(user_message="x", **kw))


RULE = "Bulk scraping runs in code"


@pytest.mark.parametrize("flags", [
    {"sandbox_browser_enabled": True},
    {"sandbox_web_fetch_enabled": True},
    {"sandbox_browser_enabled": True, "sandbox_lists_enabled": True},
])
def test_bulk_work_is_routed_to_create_data_when_code_can_do_it(flags):
    text = _system(**flags)
    assert RULE in text and "ONE `create_data` call" in text
    assert ("`browser`" in text) is bool(flags.get("sandbox_browser_enabled"))
    assert ("never write scraped rows yourself" in text) is bool(flags.get("sandbox_lists_enabled"))


@pytest.mark.parametrize("flags", [{}, {"sandbox_lists_enabled": True}, {"web_fetch_enabled": True}])
def test_no_bulk_rule_without_a_way_for_code_to_reach_pages(flags):
    assert RULE not in _system(**flags)


def test_agent_web_fetch_alone_does_not_promise_http_in_code():
    off = _system(web_fetch_enabled=True)
    assert "injected `http` client" not in off and "cannot fetch URLs" in off
    on = _system(web_fetch_enabled=True, sandbox_web_fetch_enabled=True)
    assert "injected `http` client" in on
