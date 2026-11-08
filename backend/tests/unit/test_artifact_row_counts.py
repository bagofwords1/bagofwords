"""A cut query result is reported as cut, everywhere a page gets built from it.

Stored rows are limited by the org's row limit, so counting them says nothing
about the size of the result. If the page and the agent believe the stored rows
are everything, totals computed from them are silently wrong.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.services.artifact_payload import partial_data_used, partial_rows_note, row_count_fields  # noqa: E402


def _data(stored, total):
    return {"rows": [{"a": i} for i in range(stored)], "info": {"total_rows": total}}


@pytest.mark.parametrize("stored,total", [(1000, 2240), (10, 11), (500, 3503)])
def test_a_cut_result_reports_its_real_size(stored, total):
    assert row_count_fields(_data(stored, total)) == {
        "row_count": total, "loaded_row_count": stored, "truncated": True,
    }


@pytest.mark.parametrize("stored", [0, 1, 1000])
def test_a_complete_result_is_not_truncated(stored):
    assert row_count_fields(_data(stored, stored)) == {
        "row_count": stored, "loaded_row_count": stored, "truncated": False,
    }


@pytest.mark.parametrize("data", [
    {},                                                       # withheld from this viewer
    None,
    {"rows": []},
    {"rows": [{"a": 1}]},                                     # no info recorded
    {"rows": [{"a": 1}], "info": {"total_rows": None}},
    {"rows": [{"a": 1}], "info": {"total_rows": True}},
    {"rows": [{"a": 1}, {"a": 2}], "info": {"total_rows": 1}},  # total below what is present
])
def test_missing_or_nonsense_totals_never_claim_a_cut(data):
    counts = row_count_fields(data)
    assert counts["truncated"] is False
    assert counts["row_count"] == counts["loaded_row_count"]


def test_the_code_writer_sees_real_size_loaded_rows_and_the_cut():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    viz = {"id": "v1", "title": "Sales", "rows": [{"a": 1}] * 100, "columns": [{"field": "a"}],
           "sample_row_count": 100, **row_count_fields(_data(1000, 2240))}
    profile = CreateArtifactTool()._build_viz_profile(viz, allow_llm_see_data=True)
    assert profile["row_count"] == 2240
    assert profile["loaded_row_count"] == 1000
    assert profile["truncated"] is True
    assert profile["sample_row_count"] == 100


def test_the_code_writer_is_told_not_to_total_cut_rows():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    system = CreateArtifactTool()._build_page_system_prompt()
    assert "truncated" in system
    assert "never compute totals" in system


def test_the_planner_hears_about_a_cut_when_it_can_still_change_the_query():
    note = partial_rows_note(2240, 1000)
    assert "1000" in note and "aggregate" in note


@pytest.mark.parametrize("row_count,stored", [(1000, 1000), (3, 10), (None, 10)])
def test_no_note_when_nothing_was_cut(row_count, stored):
    assert partial_rows_note(row_count, stored) is None


def test_only_cut_datasets_the_page_reads_are_reported():
    vizs = [
        {"id": "used-cut", "title": "A", "truncated": True, "row_count": 2240, "loaded_row_count": 1000},
        {"id": "unused-cut", "title": "B", "truncated": True, "row_count": 9, "loaded_row_count": 5},
        {"id": "used-whole", "title": "C", "truncated": False, "row_count": 7, "loaded_row_count": 7},
    ]
    found = partial_data_used(vizs, 'const a = vizById("used-cut"); const c = vizById("used-whole");')
    assert [f["visualization_id"] for f in found] == ["used-cut"]
    assert found[0]["row_count"] == 2240 and found[0]["loaded_row_count"] == 1000
