"""Feedback loop — "the agent re-reads the same files because read_file
results come back as collapsed previews, not content".

The planner consumes ONLY the tool observation (agent_v2 passes
`last_observation` / `past_observations` into PlannerInput — the tool `output`
never reaches the model). So whatever a whole-file text/JSON read is supposed
to deliver to the model must be IN the observation. Today read_file's
observation is a one-line summary ("Read <id> — json — (truncated)") with no
content, so the model concludes the read returned nothing and re-issues it —
the reported loop.

Invariant under test: a successful read_file of a text/JSON file must surface
the file content (or a bounded excerpt of it) in the observation payload.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.tools.implementations.read_file import ReadFileTool

FILE_TEXT = (
    '{"rule": "orders with status=cancelled are excluded from revenue", '
    '"owner": "finance", "threshold": 12345}'
)


class _FakeFileClient:
    """Minimal file client: async read + no cheap version (skips the cache)."""

    def __init__(self, payload):
        self._payload = payload

    async def aread_file(self, file_id, sheet=None, **kwargs):
        if kwargs.get("offset") is not None:
            body = self._payload
            return {
                "content": body,
                "encoding": "text",
                "offset": kwargs["offset"],
                "length": len(body.encode("utf-8")),
                "next_cursor": None,
                "total_size": len(body.encode("utf-8")),
                "eof": True,
            }
        return self._payload

    async def afile_version(self, file_id):
        return None


async def _observation(tool_input):
    with patch(
        "app.ai.tools.implementations.read_file.resolve_file_client",
        new=AsyncMock(return_value=(_FakeFileClient(FILE_TEXT), None)),
    ):
        events = [e async for e in ReadFileTool().run_stream(tool_input, {})]
    payload = events[-1].payload
    assert payload["output"]["success"] is True, payload
    return payload["observation"]


@pytest.mark.asyncio
async def test_whole_file_read_surfaces_content_in_observation():
    obs = await _observation({"connection_id": "C1", "file_id": "rules.json"})
    obs_text = json.dumps(obs, ensure_ascii=False, default=str)
    # The model must be able to see what was read — a recognizable excerpt of
    # the file body, not just a summary line.
    assert "status=cancelled" in obs_text, (
        "read_file observation carries no file content — the planner only "
        f"sees: {obs_text[:300]}"
    )


@pytest.mark.asyncio
async def test_windowed_read_surfaces_window_text_in_observation():
    obs = await _observation({"connection_id": "C1", "file_id": "big.log", "offset": 0})
    obs_text = json.dumps(obs, ensure_ascii=False, default=str)
    assert "status=cancelled" in obs_text, (
        "windowed read_file observation carries no window content — paging is "
        f"useless to the model; it only sees: {obs_text[:300]}"
    )
