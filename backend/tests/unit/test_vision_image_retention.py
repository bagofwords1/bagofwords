"""How long an image stays visible to the planner.

Images used to live for exactly ONE planner iteration: the vision-extraction
path pulled them off the observation and deleted them, so only the turn
immediately after the read could see the picture. A task needing the image AND
something else — "does this screenshot match what the query returns?" — could
never hold both at once, since each fact expired before the other arrived, and
the agent's only escape was to re-read the image at a full step per look.

Contracts here:
1. A tool-supplied image stays attached until newer images push it past the
   cap — steps that carry no image do not age it out. Whole steps are kept or
   dropped, never part of one, and the newest step is always kept.
2. The base64 never stays ON the observation — it must not reach the
   JSON-serialized <past_observations> / <last_observation> prompt text.
3. That holds for batched decisions too, where the recorded per-action
   observation is a different dict from the aggregate.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.ai.agent_v2 import (
    AgentV2,
    _MAX_RETAINED_IMAGES,
)


def _image(tag="AAAA"):
    return {"data": tag, "media_type": "image/png", "source_type": "base64"}


def _agent():
    """Minimal stand-in: _collect_vision_images only touches this one field."""
    return SimpleNamespace(_recent_vision_images=[])


def _collect(agent, observation, loop_index):
    return AgentV2._collect_vision_images(agent, observation, loop_index)


def test_image_survives_steps_that_carry_no_images():
    """A page read once stays visible across any number of note/list/submit
    steps — only newer images can push it out."""
    agent = _agent()
    _collect(agent, {"summary": "read", "images": [_image("PAGE1")]}, 4)
    for step in range(5, 30):
        later = _collect(agent, {"summary": "edit_note"}, step)
        assert [i.data for i in later] == ["PAGE1"], f"lost the page at step {step}"


@pytest.mark.parametrize("pages_per_step", [1, 5, 8])
def test_newest_images_are_kept_up_to_the_cap_in_whole_steps(pages_per_step):
    agent = _agent()
    attached = []
    for loop_index in range(30):
        attached = _collect(agent, {"summary": "read", "images": [
            _image(f"L{loop_index}P{p}") for p in range(pages_per_step)]}, loop_index)
    assert len(attached) <= _MAX_RETAINED_IMAGES
    # Whole steps only, and they are the most recent ones with no gaps.
    steps = sorted({int(i.data.split("P")[0][1:]) for i in attached})
    assert steps == list(range(30 - len(steps), 30))
    assert len(attached) == len(steps) * pages_per_step
    assert len(attached) + pages_per_step > _MAX_RETAINED_IMAGES  # nothing more fit


def test_one_step_over_the_cap_is_kept_whole_until_the_next_read():
    agent = _agent()
    _collect(agent, {"summary": "small", "images": [_image("OLD")]}, 0)
    big = _collect(agent, {"summary": "big", "images": [
        _image(f"BIG{p}") for p in range(_MAX_RETAINED_IMAGES + 6)]}, 1)
    assert len(big) == _MAX_RETAINED_IMAGES + 6 and "OLD" not in {i.data for i in big}
    after = _collect(agent, {"summary": "next read", "images": [_image("NEW")]}, 2)
    assert [i.data for i in after] == ["NEW"]


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


class _Rows:
    def __init__(self, ids):
        self._ids = ids

    def fetchall(self):
        return [(i,) for i in self._ids]


class _UploadsDb:
    """Stands in for the report_file_association lookup: which file ids were
    uploaded with which completion."""

    def __init__(self, uploads_by_completion):
        self.uploads = uploads_by_completion
        self.current = None

    async def execute(self, _stmt):
        return _Rows(self.uploads.get(self.current, []))


def _uploads_agent(tmp_path, uploads_by_completion, current_cid):
    files = []
    for cid, ids in uploads_by_completion.items():
        for fid in ids:
            path = tmp_path / f"{fid}.png"
            path.write_bytes(fid.encode())
            files.append(SimpleNamespace(id=fid, path=str(path), content_type="image/png"))
    db = _UploadsDb(uploads_by_completion)
    db.current = current_cid
    # A real AgentV2 (no __init__) so every method the loader may call exists.
    agent = AgentV2.__new__(AgentV2)
    agent.image_files = files
    agent.head_completion = SimpleNamespace(id=current_cid)
    agent.db = db
    agent.report = SimpleNamespace(id="r1")
    agent.report_id = "r1"
    return agent


async def _attached(agent):
    import base64
    images = await agent._load_images_as_input()
    return sorted(base64.b64decode(i.data).decode() for i in images)


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_turns", [1, 3, 9])
async def test_earlier_uploads_are_not_reattached_to_a_turn_without_its_own(tmp_path, earlier_turns):
    """The user-image channel lands on the NEWEST message, so a picture from an
    earlier turn re-sent there reads as "the user just sent this". A turn that
    uploaded nothing attaches nothing, however recent the earlier upload was —
    history names the file and read_file shows it again on demand."""
    uploads = {f"c{i}": [f"img{i}"] for i in range(earlier_turns)}
    uploads["now"] = []
    agent = _uploads_agent(tmp_path, uploads, "now")
    assert await _attached(agent) == []


@pytest.mark.asyncio
async def test_a_turn_attaches_exactly_its_own_uploads(tmp_path):
    uploads = {"c0": ["old_a", "old_b"], "now": ["new_a", "new_b"]}
    agent = _uploads_agent(tmp_path, uploads, "now")
    assert await _attached(agent) == ["new_a", "new_b"]
