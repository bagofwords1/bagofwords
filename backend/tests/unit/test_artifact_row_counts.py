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

from app.services.artifact_payload import (  # noqa: E402
    partial_data_used,
    partial_data_warning,
    partial_rows_note,
    row_count_fields,
)


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
    # A list can stay partial if it says so, and an unfiltered preview isn't a bug:
    # without these the agent abandoned a working lookup.
    assert "says how many" in note and "no values" in note


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


def test_the_warning_names_each_cut_dataset_and_says_what_to_do():
    used = [{"title": "Sales", "row_count": 2240, "loaded_row_count": 1000}, {"title": None, "row_count": 9, "loaded_row_count": 5}]
    warning = partial_data_warning(used)
    assert "1000 of 2240" in warning and "5 of 9" in warning and "Sales" in warning and "None" not in warning
    assert "aggregates in the database" in warning


def test_no_warning_when_the_page_reads_no_cut_data():
    assert partial_data_warning([]) == ""


def test_every_page_host_receives_the_counts():
    from app.ai.tools.mcp.app_tools import _ARTIFACT_VIZ_KEYS

    assert {"row_count", "loaded_row_count", "truncated"} <= set(_ARTIFACT_VIZ_KEYS)


def test_no_prompt_still_claims_the_page_gets_every_row():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool
    from app.ai.tools.implementations.edit_artifact_legacy import EditArtifactTool

    assert "FULL dataset" not in CreateArtifactTool()._build_page_system_prompt()
    edit_prompt = EditArtifactTool()._build_edit_system_prompt()
    assert "FULL dataset" not in edit_prompt
    assert "loaded_row_count" in edit_prompt
