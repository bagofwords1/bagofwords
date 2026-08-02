"""How long an image stays visible to the planner.

Images used to live for exactly ONE planner iteration: the vision-extraction
path pulled them off the observation and deleted them, so only the turn
immediately after the read could see the picture. A task needing the image AND
something else — "does this screenshot match what the query returns?" — could
never hold both at once, since each fact expired before the other arrived, and
the agent's only escape was to re-read the image at a full step per look.

Contracts here:
1. A tool-supplied image stays attached for several iterations, then ages out.
2. The base64 never stays ON the observation — it must not reach the
   JSON-serialized <past_observations> / <last_observation> prompt text.
3. That holds for batched decisions too, where the recorded per-action
   observation is a different dict from the aggregate.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.ai.agent_v2 import (
    AgentV2,
    _FOLLOWUP_IMAGE_LIMIT,
    _VISION_IMAGE_RETENTION_LOOPS,
)


def _image(tag="AAAA"):
    return {"data": tag, "media_type": "image/png", "source_type": "base64"}


def _agent():
    """Minimal stand-in: _collect_vision_images only touches this one field."""
    return SimpleNamespace(_recent_vision_images=[])


def _collect(agent, observation, loop_index):
    return AgentV2._collect_vision_images(agent, observation, loop_index)


def test_image_stays_attached_across_iterations_then_ages_out():
    agent = _agent()
    read_at = 4
    images = _collect(agent, {"summary": "read", "images": [_image()]}, read_at)
    assert [i.data for i in images] == ["AAAA"]

    # Still available on the following iterations — long enough for the model
    # to fetch something else and compare the two.
    for step in range(1, _VISION_IMAGE_RETENTION_LOOPS):
        later = _collect(agent, {"summary": "other tool"}, read_at + step)
        assert [i.data for i in later] == ["AAAA"], f"lost the image after {step} step(s)"

    aged_out = _collect(agent, {"summary": "other tool"}, read_at + _VISION_IMAGE_RETENTION_LOOPS)
    assert aged_out == []


def test_retention_is_bounded_under_repeated_reads():
    """Every iteration supplying a new image must not accumulate without
    limit — only the retained window's worth is ever attached."""
    agent = _agent()
    attached = []
    for loop_index in range(10):
        attached = _collect(
            agent, {"summary": "read", "images": [_image(f"IMG{loop_index}")]}, loop_index
        )
    assert len(attached) == _VISION_IMAGE_RETENTION_LOOPS
    # And it is the most RECENT window that survives, not the oldest.
    assert [i.data for i in attached] == ["IMG7", "IMG8", "IMG9"]


def test_base64_is_moved_off_the_observation():
    agent = _agent()
    observation = {"summary": "read the screenshot", "images": [_image("SECRETBYTES")]}
    _collect(agent, observation, 0)
    # The observation is JSON-serialized into the prompt; the bytes must not be.
    assert "images" not in observation
    assert observation["images_provided_as_vision"] is True
    assert "SECRETBYTES" not in json.dumps(observation)


def test_batched_decision_leaves_no_base64_on_the_recorded_observation():
    """The vision path strips the AGGREGATE, but the per-action observation is
    what gets recorded in the observation history and re-serialized into
    <past_observations> on every later iteration."""
    read_obs = {"summary": "read image", "images": [_image("SECRETBYTES")]}
    other_obs = {"summary": "5 rows", "query_id": "q1"}

    agg = AgentV2._aggregate_batch_observation(
        [
            {"tool_name": "read_file", "observation": read_obs},
            {"tool_name": "read_query", "observation": other_obs},
        ],
        [],
    )
    # Hoisted for the vision path...
    assert [img["data"] for img in agg["images"]] == ["SECRETBYTES"]
    # ...and gone from the dict that lands in the history.
    assert "images" not in read_obs
    assert read_obs["images_provided_as_vision"] is True
    assert "SECRETBYTES" not in json.dumps(agg["parallel_actions"])


def test_followup_images_are_the_most_recent_and_bounded():
    """A turn that uploaded nothing still gets the recent pictures — newest
    first, capped — so "why?" about a screenshot from a turn ago doesn't start
    by spending a step to find it again."""
    files = [SimpleNamespace(id=f"f{i}", created_at=i) for i in range(5)]
    agent = SimpleNamespace(image_files=files)
    picked = AgentV2._followup_image_files(agent)
    assert [f.id for f in picked] == ["f4", "f3"]
    assert len(picked) == _FOLLOWUP_IMAGE_LIMIT
