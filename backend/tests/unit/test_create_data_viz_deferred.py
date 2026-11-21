"""create_data's background visualization inference produces the inline result.

In the agent loop create_data ends with the deterministic chart and refines it
in the background (``_refine_visualization``); elsewhere it awaits the same
inference inline. Both must end in the same data_model and view for the same
inference reply, and a failed background pass must leave the deterministic
chart standing rather than an empty one. The LLM is stubbed at
``_infer_visualization_model`` — the boundary — so this is deterministic.
"""

import pytest

from app.ai.tools.implementations.create_data import CreateDataTool


def _formatted():
    rows = [
        {"country": "USA", "year": 2023, "revenue": 523.1},
        {"country": "USA", "year": 2024, "revenue": 610.0},
        {"country": "Canada", "year": 2023, "revenue": 303.9},
        {"country": "Canada", "year": 2024, "revenue": 280.5},
        {"country": "France", "year": 2023, "revenue": 195.1},
        {"country": "France", "year": 2024, "revenue": 210.2},
    ]
    return {
        "columns": [
            {"field": "country", "headerName": "country"},
            {"field": "year", "headerName": "year"},
            {"field": "revenue", "headerName": "revenue"},
        ],
        "rows": rows,
        "info": {"total_rows": len(rows), "total_columns": 3},
    }


INFERRED = {
    "type": "bar_chart",
    "series": [{"name": "Revenue", "key": "country", "value": "revenue", "aggregation": "sum"}],
    "group_by": "year",
    "display": {"format": "currency", "currency": "USD"},
}


def _stub_inference(monkeypatch, tool, result=None, exc=None):
    async def fake(**kwargs):
        if exc:
            raise exc
        return result

    monkeypatch.setattr(tool, "_infer_visualization_model", fake)


async def _refine(tool, fallback_type="bar_chart", formatted=None):
    return await tool._refine_visualization(
        runtime_ctx={},
        user_prompt="revenue by country and year",
        messages_context="",
        formatted=formatted or _formatted(),
        allow_llm_see_data=True,
        fallback_type=fallback_type,
        title="Revenue by country",
    )


@pytest.mark.asyncio
async def test_background_refinement_equals_inline_finalization(monkeypatch):
    tool = CreateDataTool()
    _stub_inference(monkeypatch, tool, {"data_model": dict(INFERRED)})
    refined = await _refine(tool)
    inline_dm, inline_view, _ = CreateDataTool._finalize_visualization(
        "bar_chart", dict(INFERRED), _formatted(), title="Revenue by country", runtime_ctx={},
    )
    assert refined["data_model"] == inline_dm
    assert refined["view"] == inline_view
    # And the inference actually contributed (not just the deterministic spec).
    assert refined["data_model"].get("group_by") in ("year", ["year"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stub", [
    {"result": None},
    {"result": {"data_model": None}},
    {"exc": RuntimeError("provider down")},
])
async def test_failed_background_pass_returns_none(monkeypatch, stub):
    """None means "keep the deterministic chart the tool already returned"."""
    tool = CreateDataTool()
    _stub_inference(monkeypatch, tool, **stub)
    assert await _refine(tool) is None


def test_deterministic_chart_is_renderable_without_inference():
    """What the tool returns before the background pass lands must draw."""
    dm, view, meta = CreateDataTool._finalize_visualization(
        "bar_chart", None, _formatted(), title="Revenue by country", runtime_ctx={},
    )
    assert dm["type"] == "bar_chart"
    assert dm.get("series"), "deterministic spec must name a measure"
    columns = {"country", "year", "revenue"}
    for s in dm["series"]:
        assert s.get("value") in columns
    assert view and view.get("view", {}).get("type") == "bar_chart"


@pytest.mark.asyncio
async def test_refinement_names_only_real_columns(monkeypatch):
    """A hallucinated column in the inference costs that field, never the chart."""
    tool = CreateDataTool()
    bad = dict(INFERRED, group_by="not_a_column",
               series=[{"name": "x", "key": "country", "value": "ghost"}])
    _stub_inference(monkeypatch, tool, {"data_model": bad})
    refined = await _refine(tool)
    columns = {"country", "year", "revenue"}
    assert refined["data_model"].get("group_by") in (None, *columns)
    for s in refined["data_model"].get("series") or []:
        assert s.get("value") in columns
