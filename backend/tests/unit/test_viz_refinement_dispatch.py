"""A chart-reading action never reads a provisional chart — per action, not per batch.

Regression for the review of #1858: the settle used to run once before a tool
batch, before create_data had handed over its background inference, so in a
serial batch [create_data, read_query] the reader saw the provisional chart and
the refinement was written only at the end of the run. The barrier now runs
before each action inside AgentV2._dispatch_action_batch. These tests drive the
real dispatcher and the real tracker with stubbed tool bodies.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.ai.agent_v2 import AgentV2
from app.ai.viz_refinement import VizRefinement, VizRefinementTracker

REFINED = {"data_model": {"type": "bar_chart", "series": [{"key": "country", "value": "revenue"}]},
           "view": {"version": "v2", "view": {"type": "bar_chart"}}}


def _agent(applied: list, concurrency: int = 1) -> AgentV2:
    agent = object.__new__(AgentV2)
    agent.sigkill_event = asyncio.Event()

    async def announce(rec):
        return None

    async def apply(rec):
        applied.append(rec.step_id)

    agent._viz_refinements = VizRefinementTracker(announce=announce, apply=apply)
    agent._tool_concurrency = lambda: concurrency
    agent._new_invocation_state = lambda name: SimpleNamespace(tool=name)
    agent._adopt_invocation_outcomes = lambda outcomes: None
    return agent


async def _later(value, delay):
    await asyncio.sleep(delay)
    return value


def _action(name):
    return SimpleNamespace(name=name, arguments={})


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["read_query", "create_artifact", "create_doc"])
async def test_reader_after_create_data_in_the_same_batch_sees_the_refined_chart(reader):
    applied: list = []
    agent = _agent(applied)
    seen_by_reader: list = []

    async def run_one(ti, act, bid, inv):
        if act.name == "create_data":
            # What the agent does after create_data ends: take over its
            # background inference (still running when the next action starts).
            agent._viz_refinements.track(VizRefinement(task=asyncio.ensure_future(_later(REFINED, 0.05)), step_id="s1"))
        else:
            seen_by_reader.append(list(applied))
        return {"index": ti, "tool_name": act.name, "skipped": False}

    outcomes = await agent._dispatch_action_batch(
        [_action("create_data"), _action(reader)], [None, None], run_one,
    )
    assert [o["tool_name"] for o in outcomes] == ["create_data", reader]
    # The refinement was written before the reader ran, not after the run.
    assert seen_by_reader == [["s1"]]
    assert not agent._viz_refinements.pending


@pytest.mark.asyncio
async def test_actions_that_do_not_read_charts_do_not_wait():
    applied: list = []
    agent = _agent(applied)
    starts: list = []
    loop = asyncio.get_running_loop()

    async def run_one(ti, act, bid, inv):
        starts.append((act.name, loop.time()))
        if act.name == "create_data":
            agent._viz_refinements.track(VizRefinement(task=asyncio.ensure_future(_later(REFINED, 5.0)), step_id=f"s{ti}"))
        return {"index": ti, "tool_name": act.name, "skipped": False}

    t0 = loop.time()
    await agent._dispatch_action_batch(
        [_action("create_data"), _action("inspect_data"), _action("create_data")], [None] * 3, run_one,
    )
    assert loop.time() - t0 < 1.0          # nobody waited on the 5 s inference
    assert applied == []                    # nothing was forced to settle
    await agent._viz_refinements.cancel_all()


@pytest.mark.parametrize("platform,expected", [
    (None, True),        # web report: figures, background refinement
    ("", True),
    ("slack", False),    # channels send the chart image at tool end: inline
    ("teams", False),
    ("whatsapp", False),
])
def test_only_web_report_runs_defer_the_inference(monkeypatch, platform, expected):
    monkeypatch.delenv("BOW_VIZ_INFERENCE_INLINE", raising=False)
    agent = object.__new__(AgentV2)
    agent.platform = platform
    assert agent._defer_viz_inference_for_run() is expected


def test_the_kill_switch_keeps_every_run_inline(monkeypatch):
    monkeypatch.setenv("BOW_VIZ_INFERENCE_INLINE", "1")
    agent = object.__new__(AgentV2)
    agent.platform = None
    assert agent._defer_viz_inference_for_run() is False
