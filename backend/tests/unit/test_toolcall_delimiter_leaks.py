"""Tool-call arguments split by a string closed without Gemma's <|"|> delimiter.

Gemma 4 served by vLLM (``--tool-call-parser gemma4``) delimits tool-call
strings with ``<|"|>``. When the model closes a string with a backtick (or
``</code>``) instead, the parser reads past it and returns valid JSON with the
next member's value glued onto the previous string and a garbage key such as
``page<|"|>,prompt``. Observed on create_artifact in 5 of 8 replays of a real
planner request: the call failed validation with "prompt: Field required" and
the dashboard was never built. These tests pin the contract at the ToolRunner:
such a call reaches the tool with every argument restored, and anything that
does not match the leak exactly is left for validation to report.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Optional

import pytest
from pydantic import BaseModel

from app.ai.runner.tool_runner import ToolRunner

D = '<|"|>'


class _ArtifactLikeInput(BaseModel):
    prompt: str
    code: Optional[str] = None
    mode: Literal["page", "slides"] = "page"
    title: Optional[str] = None
    visualization_ids: list[str] = []


class _RecordingTool:
    name = "artifact_like"
    spec = None
    metadata = None
    output_model = None

    def __init__(self) -> None:
        self.received: Optional[dict] = None

    @property
    def input_model(self) -> type[BaseModel]:
        return _ArtifactLikeInput

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        self.received = tool_input
        yield {
            "type": "tool.end",
            "payload": {"output": {"ok": True}, "observation": {"summary": "ok", "success": True}},
        }


async def _run(arguments: dict) -> tuple[dict, _RecordingTool]:
    tool = _RecordingTool()
    result = await ToolRunner().run(tool, arguments, {"mode": "chat"}, _noop_emit)
    return result, tool


async def _noop_emit(event):
    return None


CODE = "<script type=\"text/babel\">\nfunction App() { return <div>Hi</div>; }\nReactDOM.createRoot(document.getElementById('root')).render(<App />);\n</script>"
VIZ = ["0b6b9a7e-5d0c-4a52-9a43-1f0f3c2b7d11"]


def _assert_not_validation_failure(result: dict) -> None:
    observation = result.get("observation") or {}
    assert (observation.get("error") or {}).get("type") not in (
        "validation_error", "repeated_validation_error",
    ), observation


@pytest.mark.asyncio
@pytest.mark.parametrize("closer", ["`", "</code>"])
async def test_value_swallowed_after_code_is_restored(closer):
    # code closed with a backtick/</code>: mode's value lands in the next key.
    leaked = {
        "code": CODE + closer + ",mode:",
        "slides" + D + ",prompt": "Quarterly deck for the board.",
        "title": "Q3 review",
        "visualization_ids": VIZ,
    }
    result, tool = await _run(leaked)

    _assert_not_validation_failure(result)
    assert tool.received == {
        "prompt": "Quarterly deck for the board.",
        "code": CODE,
        "mode": "slides",
        "title": "Q3 review",
        "visualization_ids": VIZ,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Revenue by region", "דשבורד בתי חולים"])
async def test_key_lost_after_prompt_is_restored(title):
    # prompt closed with a backtick: the title key vanishes and its value is
    # glued onto the following key.
    leaked = {
        "code": CODE,
        "mode": "page",
        "prompt": "1. KPIs on top\n2. Ranking chart\n`,title:",
        title + D + ",visualization_ids": VIZ,
    }
    result, tool = await _run(leaked)

    _assert_not_validation_failure(result)
    assert tool.received["title"] == title
    assert tool.received["prompt"] == "1. KPIs on top\n2. Ranking chart"
    assert tool.received["visualization_ids"] == VIZ
    assert tool.received["code"] == CODE


@pytest.mark.asyncio
async def test_two_leaks_in_one_call_are_both_restored():
    leaked = {
        "code": CODE + "`,mode:",
        "page" + D + ",prompt": "Explore albums per artist.`,,title:",
        "Albums" + D + ",visualization_ids": VIZ,
    }
    result, tool = await _run(leaked)

    _assert_not_validation_failure(result)
    assert tool.received == {
        "prompt": "Explore albums per artist.",
        "code": CODE,
        "mode": "page",
        "title": "Albums",
        "visualization_ids": VIZ,
    }


@pytest.mark.asyncio
async def test_clean_arguments_reach_the_tool_unchanged():
    # A value may legitimately end in ",title:" — without the delimiter
    # signature in a key, nothing is touched.
    clean = {"prompt": "List fields as a, b, title:", "code": CODE, "mode": "page",
             "title": "x", "visualization_ids": VIZ}
    _, tool = await _run(dict(clean))
    assert tool.received == clean


@pytest.mark.asyncio
@pytest.mark.parametrize("leaked", [
    # garbage key, but the preceding string names no field of this tool
    {"code": CODE + "`,colour:", "page" + D + ",prompt": "p"},
    # garbage key with no preceding swallowed-key tail at all
    {"code": CODE, "page" + D + ",prompt": "p"},
])
async def test_unresolvable_leak_is_not_guessed(leaked):
    result, tool = await _run(leaked)

    assert tool.received is None
    assert result["observation"]["success"] is False
    assert result["observation"]["error"]["type"] == "validation_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("quoted", ["'slides'", '"slides"', "`slides`"])
async def test_quoted_choice_value_is_unwrapped(quoted):
    # Plain quotes instead of <|"|> survive into the value and fail a Literal.
    result, tool = await _run({"prompt": "Deck", "mode": quoted, "title": "Q3"})

    _assert_not_validation_failure(result)
    assert tool.received["mode"] == "slides"


@pytest.mark.asyncio
async def test_quotes_in_free_text_are_kept():
    args = {"prompt": "'Top 10' artists", "mode": "page", "title": "'Quoted title'"}
    _, tool = await _run(dict(args))
    assert tool.received["title"] == "'Quoted title'"
    assert tool.received["prompt"] == "'Top 10' artists"


@pytest.mark.asyncio
async def test_quoted_value_outside_the_choices_still_fails_validation():
    result, tool = await _run({"prompt": "Deck", "mode": "'poster'"})

    assert tool.received is None
    assert result["observation"]["error"]["type"] == "validation_error"
