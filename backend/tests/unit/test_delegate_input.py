"""DelegateInput survives the tool runner's validate → dump → validate round trip.

The runner validates tool arguments with the input model and hands the dumped
dict to the tool, which validates it again. Optional fields the planner left
out (or sent blank) must come through that round trip, or every such delegate
call fails as "Invalid input".
"""
import pytest

from app.ai.tools.schemas.sub_agents import DelegateInput


@pytest.mark.parametrize("extra", [
    {},
    {"context": ""},
    {"context": "   "},
    {"model": ""},
    {"agent_ids": []},
    {"context": "Paid orders = status 'paid'.", "model": "Claude Opus 5.5", "max_steps": 10},
])
def test_round_trip_through_the_tool_runner(extra):
    args = {"goal": "Count paid orders per day for September.", "title": "Paid orders", **extra}

    once = DelegateInput(**args).model_dump()
    twice = DelegateInput(**once)

    assert twice.goal == args["goal"]
    assert twice.title == args["title"]
    assert (twice.context or "").strip() == (args.get("context") or "").strip()
