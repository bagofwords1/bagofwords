"""Tool observations must survive the completion boundary.

An observation is what the model SAW when a tool ran. It used to live only in
the per-run ObservationContextBuilder, so the next turn -- and any turn after an
interrupted run -- saw only a re-derived digest. These tests pin the three
properties that fix depends on.
"""
import json

import pytest

from app.ai.persisted_summary import (
    SUMMARIZED_TOOL_NAMES,
    OBSERVATION_TOTAL_MAX_CHARS,
    build_tool_context_summary,
    bounded_observation,
)


def test_observation_is_projected_for_a_non_row_heavy_tool():
    # inspect_data's `output` carries no summary at all; only the observation
    # does. Re-deriving history from output is what lost the detail.
    out = {"success": True, "code": "SELECT 1", "execution_log": "x" * 50}
    obs = {"summary": "Inspection finished", "details": "order_id | status", "success": True}
    summary = build_tool_context_summary("inspect_data", out, obs)
    assert summary["observation"]["summary"] == "Inspection finished"
    assert "order_id" in summary["observation"]["details"]


def test_observation_only_tool_still_projects_without_result_json():
    summary = build_tool_context_summary("search_mcps", None, {"summary": "found 3"})
    assert summary["observation"]["summary"] == "found 3"


def test_row_heavy_tools_keep_their_purpose_built_projection():
    # Report cards read these; swapping in the observation would both bloat the
    # column and break the UI contract.
    summary = build_tool_context_summary(
        "create_data",
        {"success": True, "data_preview": {"columns": [{"field": "a"}], "rows": [{"a": 1}], "row_count": 1}},
        {"summary": "ignored here"},
    )
    assert "data_preview" in summary
    assert "observation" not in summary


@pytest.mark.parametrize("tool", sorted(SUMMARIZED_TOOL_NAMES))
def test_row_heavy_set_is_unchanged_by_an_observation(tool):
    a = build_tool_context_summary(tool, {"success": True}, None)
    b = build_tool_context_summary(tool, {"success": True}, {"summary": "x", "details": "y" * 100})
    assert a == b


def test_images_are_elided_but_their_existence_is_recorded():
    # Vision payloads are re-sent as image blocks within their own run; replaying
    # base64 from history is both expensive and invalid. Silence would read as
    # "no image was produced", so leave a marker.
    obs = {"summary": "saw it", "images": [{"data": "AAAA" * 500}]}
    bounded = bounded_observation(obs)
    assert "images" not in bounded
    assert "images_elided" in bounded


def test_a_pathological_observation_is_bounded():
    obs = {"summary": "big", "payload": "z" * 400_000}
    bounded = bounded_observation(obs)
    assert len(json.dumps(bounded)) < OBSERVATION_TOTAL_MAX_CHARS + 2_000
    # The identifying fields survive the trim; only bulk goes.
    assert bounded["summary"] == "big"


def test_empty_observation_projects_nothing():
    assert bounded_observation({}) is None
    assert bounded_observation(None) is None
    assert build_tool_context_summary("inspect_data", None, None) is None
