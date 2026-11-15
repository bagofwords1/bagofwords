"""A page the tool writes itself (no code from the planner) must pass the page checks too.

The tool-generated path repairs in-tool, and used to save a page that still
failed a check with only a note. On the benchmark every Haiku page with a raw
line chart came that way, and the same path could put the viewer's name in a
shared title. Now unconverged check errors fail the call in both paths.
"""
import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations.create_artifact import CreateArtifactTool  # noqa: E402

DATA = {"runtime": {"version": 11}, "visualizations": []}
RAW_LINE = "function App() { return <EChart option={{ series: [{ type: 'line', data: [1, 2] }] }} />; }"
CLEAN = "function App() { return <div>Ready</div>; }"


def run(monkeypatch, code, fixed, strict):
    tool = CreateArtifactTool()

    async def screenshot(self, html):
        return None, []                       # renders without a thrown error

    async def fix(self, candidate, errors, mode, runtime_ctx):
        return fixed                          # what the in-tool repair produces

    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", screenshot)
    monkeypatch.setattr(CreateArtifactTool, "_fix_code", fix)
    monkeypatch.setattr(CreateArtifactTool, "_build_thumbnail_html", lambda self, data, code, mode="page": "<html></html>")

    async def go():
        out = None
        async for item in tool._validate_and_repair_stream(code, DATA, "page", {}, max_repairs=(0 if strict else None), strict=strict):
            if isinstance(item, dict):
                out = item
        return out
    return asyncio.run(go())


def test_a_generated_page_that_still_fails_a_check_is_not_saved_as_clean(monkeypatch):
    out = run(monkeypatch, RAW_LINE, RAW_LINE, strict=False)       # the repair can't fix it
    assert out["clean"] is False
    assert any(e.startswith("[charts]") for e in out["errors"])


def test_a_generated_page_the_repair_fixes_is_clean(monkeypatch):
    out = run(monkeypatch, RAW_LINE, CLEAN, strict=False)
    assert out["clean"] is True and out["code"] == CLEAN


def test_planner_authored_code_is_unchanged(monkeypatch):
    out = run(monkeypatch, RAW_LINE, RAW_LINE, strict=True)
    assert out["clean"] is False and any(e.startswith("[charts]") for e in out["errors"])
