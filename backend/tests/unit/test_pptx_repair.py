"""Unit tests for the slides execute-and-repair loop (parity with page-mode
render validation): _execute_and_repair_pptx converges on a repairable error,
returns the original code when repair fails, and reports success without
repairs for working code."""

import pytest

from pathlib import Path

from app.ai.tools.implementations.create_artifact import CreateArtifactTool


GOOD_CODE = """
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[6])
tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
tb.text_frame.text = report.get("title") or "Deck"
prs.save(_pptx_output_path)
"""

# Crashes the way the real-world failure did: a column dict passed where
# python-pptx expects a string.
BAD_CODE = """
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[6])
tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
tb.text_frame.text = visualizations[0]["columns"][0]
prs.save(_pptx_output_path)
"""

VIZ = [{"title": "V", "columns": [{"field": "a", "headerName": "A"}], "rows": [{"a": 1}]}]
REPORT = {"id": "r1", "title": "T", "theme": None}


async def _run_loop(tool, code, out_path, runtime_ctx):
    result = None
    events = []
    async for item in tool._execute_and_repair_pptx(
        code, VIZ, REPORT, out_path, {}, runtime_ctx
    ):
        if isinstance(item, dict):
            result = item
        else:
            events.append(item)
    return result, events


@pytest.fixture
def tool():
    return CreateArtifactTool()


@pytest.mark.asyncio
async def test_good_code_executes_without_repair(tool, tmp_path):
    out = tmp_path / "deck.pptx"
    result, _ = await _run_loop(tool, GOOD_CODE, out, {})
    assert result["ok"] is True
    assert result["repair_attempts"] == 0
    assert result["error"] is None
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.asyncio
async def test_bad_code_is_repaired_in_tool(tool, tmp_path, monkeypatch):
    out = tmp_path / "deck.pptx"
    seen = {}

    async def fake_fix(code, error, runtime_ctx):
        seen["error"] = error
        return GOOD_CODE

    monkeypatch.setattr(tool, "_fix_pptx_code", fake_fix)
    result, _ = await _run_loop(tool, BAD_CODE, out, {})
    assert result["ok"] is True
    assert result["repair_attempts"] == 1
    assert result["code"] == GOOD_CODE
    assert out.exists()
    # The repair prompt received the trimmed executor error
    assert "split" in seen["error"] or "AttributeError" in seen["error"]


@pytest.mark.asyncio
async def test_unrepairable_code_returns_original_and_no_deck(tool, tmp_path, monkeypatch):
    out = tmp_path / "deck.pptx"

    calls = {"n": 0}

    async def fake_fix(code, error, runtime_ctx):
        # Distinct on every call: returning the same code twice trips the
        # stalled-repair break before the budget is spent.
        calls["n"] += 1
        return BAD_CODE + f"\n# still broken {calls['n']}"

    monkeypatch.setattr(tool, "_fix_pptx_code", fake_fix)
    result, _ = await _run_loop(tool, BAD_CODE, out, {})
    assert result["ok"] is False
    assert result["repair_attempts"] == tool.MAX_RENDER_REPAIR_ATTEMPTS
    # Original verified-broken code is returned, not an unverified intermediate
    assert result["code"] == BAD_CODE
    assert result["error"] and "AttributeError" in result["error"]
    assert not out.exists()


@pytest.mark.asyncio
async def test_repair_stops_when_fix_returns_same_code(tool, tmp_path, monkeypatch):
    out = tmp_path / "deck.pptx"
    calls = {"n": 0}

    async def fake_fix(code, error, runtime_ctx):
        calls["n"] += 1
        return code  # no progress → loop must stop, not spin

    monkeypatch.setattr(tool, "_fix_pptx_code", fake_fix)
    result, _ = await _run_loop(tool, BAD_CODE, out, {})
    assert result["ok"] is False
    assert calls["n"] == 1
