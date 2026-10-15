"""Guards around submit_<list> calls that a stuck agent can loop on.

Three failure paths seen live (a list-filling chat looped for 30+ minutes on
"records: [] should be non-empty"):

* a broken tool call (JSON cut off mid-stream that the parser cannot
  salvage) reached the list rewrite as the parser's ``_unparsable`` marker, and the
  rewrite turned it into an empty ``records`` list — hiding the real error;
* repeated failing saves to one list had no cap;
* (see test_agent_list_replay) past successful saves of new rows replayed as
  ``records: []``, which the model then imitated.
"""
from types import SimpleNamespace

import pytest

from app.ai.agent_v2 import AgentV2, list_save_failure_action
from app.ai.llm.toolcall_args import ERROR_KEY, RAW_KEY, UNPARSABLE_KEY, parse_tool_call_arguments

ROUTING = {"submit_contracts": {"list_id": "L1", "list_name": "Contracts", "data_source_id": "D1"}}


def _rewrite(tool_input):
    agent = SimpleNamespace(_list_tool_routing=ROUTING)
    return AgentV2._rewrite_list_action(agent, "submit_contracts", tool_input)


@pytest.mark.parametrize("raw", [
    '{"records": [{"row_id": null, "fields": {"name": {"value": "Acme',  # cut off mid-string
    '{"records": ',
])
def test_broken_call_keeps_its_parse_error_instead_of_becoming_empty(raw):
    parsed = parse_tool_call_arguments(raw, "submit_contracts")
    assert parsed.get(UNPARSABLE_KEY), "precondition: the parser flags the call as broken"

    name, args = _rewrite(parsed)

    assert name == "submit_list" and args["list_id"] == "L1"
    # The tool runner short-circuits on this marker with the real JSON error
    # and its own attempt cap; an empty records list hides both.
    assert args.get(UNPARSABLE_KEY) is True
    assert args.get(RAW_KEY) and args.get(ERROR_KEY)
    assert "records" not in args


def test_well_formed_call_is_rewritten_with_its_records():
    records = [{"row_id": None, "fields": {"name": {"value": "Acme", "status": "found"}}}]
    name, args = _rewrite({"records": records})
    assert (name, args) == ("submit_list", {"list_id": "L1", "records": records})


def test_unknown_tool_passes_through_untouched():
    agent = SimpleNamespace(_list_tool_routing=ROUTING)
    assert AgentV2._rewrite_list_action(agent, "create_data", {"x": 1}) == ("create_data", {"x": 1})


def _run(events):
    """Feed (list_id, failed) save outcomes; return the action after each."""
    streaks: dict = {}
    return [list_save_failure_action(streaks, lid, failed) for lid, failed in events]


def test_consecutive_failures_on_one_list_warn_then_stop():
    actions = _run([("L1", True)] * 6)
    assert actions[:2] == [None, None]          # normal fix-and-retry stays quiet
    assert actions[2] == "warn"                 # third failure in a row
    assert actions[3] is None
    assert actions[4] == "stop" and actions[5] == "stop"


def test_a_successful_save_resets_the_streak():
    actions = _run([("L1", True), ("L1", True), ("L1", False), ("L1", True), ("L1", True)])
    assert "warn" not in actions and "stop" not in actions


def test_lists_are_counted_independently():
    actions = _run([("L1", True), ("L2", True), ("L1", True), ("L2", True), ("L1", True)])
    assert actions == [None, None, None, None, "warn"]
