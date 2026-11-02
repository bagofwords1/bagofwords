"""With "Allow LLM to see data" off, no raw cell value reaches the model through
history, compaction, the previous-queries context, a code retry, or the
write_csv and visualization-profile observations; with it on, nothing changes.

Every leak is checked with a sentinel cell (an SSN) that must be absent with
the setting off, while the structure (columns, row counts, references,
parameters, date ranges, safe aggregates) stays.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.code_execution.code_execution import StreamingCodeExecutor, format_execution_failure
from app.ai.context.builders.message_context_builder import _compaction_observation_text
from app.ai.context.builders.query_context_builder import QueryContextBuilder
from app.ai.context.durable_transcript import _result_content
from app.ai.context.hidden_data import hide_data_for_replay
from app.ai.context.parts import Outcome
from app.ai.persisted_summary import TOOL_CONTEXT_KIND_FIELD, TOOL_CONTEXT_OBSERVATION_KIND
from app.ai.tools.implementations.create_artifact import CreateArtifactTool
from app.ai.code_execution import code_execution
from app.ai.tools.implementations import write_csv
from app.ai.tools.implementations.write_csv import _model_data_fields
from app.schemas.ai.planner import PlannerInput

SSN = "078-05-1120"

# describe()-style stats: top/min/max are verbatim cells; mean/count are not.
STATS = {
    "total_rows": 2,
    "column_info": {
        "ssn": {"dtype": "object", "count": 2, "unique": 2, "top": SSN},
        "amount": {"dtype": "float64", "count": 2, "mean": 5.0, "min": 1.0, "max": 9.0},
        "day": {"dtype": "datetime64[ns]", "count": 2, "min": "2026-01-01", "max": "2026-01-31"},
    },
}

# Stored summaries as each tool writes them (context_summary_json), with a
# sentinel captured while data was still visible.
SUMMARIES = {
    "read_file": {"version": 2, "success": True, "file_name": "people.csv", "row_count": 2,
                  "rows_shown": 2, "csv": f"name,ssn\nJane,{SSN}\n", "text": f"Jane {SSN}"},
    "write_csv": {"version": 2, "success": True, "file_id": "f1", "row_count": 2, "columns": ["name", "ssn"],
                  "data_preview": {"columns": [{"field": "name"}, {"field": "ssn"}], "row_count": 2,
                                   "rows": [{"name": "Jane", "ssn": SSN}]},
                  "stats": STATS},
    "inspect_data": {"summary": "Inspection finished for: who is Jane", "success": True,
                     "details": f"   name          ssn\n0  Jane  {SSN}", "code": "print(df.head())"},
    "describe_entity": {"summary": "Described entity.", "entity_id": "e1", "title": "People",
                        "data_preview": {"columns": [{"field": "ssn"}], "row_count": 2,
                                         "rows": [{"ssn": SSN}]},
                        "data_profile": {"head_rows": [{"ssn": SSN}]},
                        "stats": STATS, "parameters": [{"name": "region", "type": "string"}]},
    "read_artifact": {"summary": "Read artifact", "artifact_id": "a1",
                      "visualization_profiles": [{
                          "id": "v1", "query_id": "q1", "row_count": 2,
                          "columns": [{"field": "ssn", "dtype": "object", "min": SSN, "max": SSN},
                                      {"field": "day", "dtype": "datetime64[ns]",
                                       "min": "2026-01-01", "max": "2026-01-31"}],
                          "sample_rows": [{"ssn": SSN}],
                          "column_stats": {"amount": {"min": 1.0, "max": 9.0}},
                      }]},
    # An older record with no purpose-built shape: rows nested anywhere.
    "legacy_tool": {"success": True, "result": {"data": {"rows": [{"ssn": SSN}], "row_count": 2}}},
}


def _execution(tool_name, summary, *, envelope=False):
    body = ({"version": 2, TOOL_CONTEXT_KIND_FIELD: TOOL_CONTEXT_OBSERVATION_KIND, "observation": summary}
            if envelope else summary)
    return SimpleNamespace(tool_name=tool_name, context_summary_json=body, result_json=None,
                           result_summary=summary.get("summary"), error_message=None)


@pytest.mark.parametrize("tool_name", sorted(SUMMARIES))
def test_replay_hides_every_cell_and_keeps_the_structure(tool_name):
    summary = SUMMARIES[tool_name]
    before = copy.deepcopy(summary)
    hidden = hide_data_for_replay(summary, tool_name)
    text = json.dumps(hidden)
    assert SSN not in text
    assert summary == before, "the stored summary is never changed"
    for key in ("row_count", "file_id", "entity_id", "artifact_id", "query_id", "parameters"):
        if key in json.dumps(before):
            assert key in text, key


def test_replay_keeps_safe_aggregates_and_date_ranges():
    hidden = hide_data_for_replay(SUMMARIES["write_csv"], "write_csv")
    info = hidden["stats"]["column_info"]
    assert "top" not in info["ssn"] and "min" not in info["amount"] and "max" not in info["amount"]
    assert info["amount"]["sum"] == 10.0
    assert info["day"]["min"] == "2026-01-01"
    profile = hide_data_for_replay(SUMMARIES["read_artifact"], "read_artifact")["visualization_profiles"][0]
    assert profile["columns"][1]["min"] == "2026-01-01"
    assert "min" not in profile["columns"][0]
    assert "column_stats" not in profile and profile["data_hidden"] is True


def test_a_scalar_named_rows_is_structure_not_data():
    assert hide_data_for_replay({"rows": 12, "row_count": 12}, "x") == {"rows": 12, "row_count": 12}


@pytest.mark.parametrize("tool_name", sorted(SUMMARIES))
def test_transcript_replay_is_unchanged_with_data_visible_and_hidden_without(tool_name):
    execution = _execution(tool_name, SUMMARIES[tool_name])
    visible, _ = _result_content(execution, Outcome.SUCCESS, allow_llm_see_data=True)
    assert visible == json.dumps(SUMMARIES[tool_name], default=str, ensure_ascii=False, separators=(",", ":"))
    hidden, _ = _result_content(execution, Outcome.SUCCESS, allow_llm_see_data=False)
    assert SSN not in hidden


def test_transcript_replay_hides_inside_the_observation_envelope():
    execution = _execution("inspect_data", SUMMARIES["inspect_data"], envelope=True)
    hidden, digest = _result_content(execution, Outcome.SUCCESS, allow_llm_see_data=False)
    assert SSN not in hidden and SSN not in (digest or "")
    assert "Inspection finished" in hidden


def test_compaction_text_hides_what_it_captured_while_data_was_visible():
    execution = _execution("describe_entity", SUMMARIES["describe_entity"], envelope=True)
    assert SSN in _compaction_observation_text(execution)
    assert SSN in _compaction_observation_text(execution, True)
    assert SSN not in _compaction_observation_text(execution, False)


def _planner_input(**kw) -> PlannerInput:
    return PlannerInput(user_message="Totals per month", organization_name="Acme",
                        organization_ai_analyst_name="Analyst", **kw)


def test_the_transcript_path_says_data_is_hidden():
    assert "<data_visibility>" in PromptBuilderV3._build_static_context(_planner_input(allow_llm_see_data=False))
    assert "<data_visibility>" not in PromptBuilderV3._build_static_context(_planner_input())


def test_previous_queries_context_gates_stats_when_previews_are_off():
    builder = QueryContextBuilder.__new__(QueryContextBuilder)
    step = {"id": "s1", "title": "People", "info": STATS, "columns": [{"field": "ssn"}]}
    query = {"id": "q1", "title": "People"}
    hidden = builder._build_query_observation(query, step, [], include_data_preview=False)
    assert SSN not in json.dumps(hidden.stats) and hidden.row_count == 2
    shown = builder._build_query_observation(query, step, [], include_data_preview=True)
    assert SSN in json.dumps(shown.stats)


def test_a_code_retry_sees_no_printed_data_and_no_quoted_cell():
    exc = ValueError(f"could not convert string to float: '{SSN}'")
    exc.captured_stdout = f"   ssn\n0  {SSN}"
    message = f"Execution error: {exc}"
    shown = format_execution_failure(exc, message)
    assert SSN in shown and "<stdout_before_failure>" in shown
    hidden = format_execution_failure(exc, message, allow_llm_see_data=False)
    assert SSN not in hidden and "<hidden>" in hidden
    missing = KeyError("revenue")
    missing.captured_stdout = SSN
    assert format_execution_failure(missing, "Execution error: 'revenue'", False) == "Execution error: 'revenue'"


class _Settings:
    def __init__(self, allowed):
        self.allowed = allowed

    def get_config(self, key):
        assert key == "allow_llm_see_data"
        return SimpleNamespace(value=self.allowed)


def test_write_csv_observation_hides_the_file_head_and_raw_stats():
    head = f"   name          ssn\n0  Jane  {SSN}"
    assert _model_data_fields(head, STATS, _Settings(True)) == (head, STATS)
    preview, stats = _model_data_fields(head, STATS, _Settings(False))
    assert preview == "" and SSN not in json.dumps(stats) and stats["total_rows"] == 2


def test_visualization_profile_min_max_only_for_dates_when_hidden():
    viz = {"id": "v1", "columns": [{"field": "ssn"}, {"field": "day"}], "rows": [{"ssn": SSN, "day": "2026-01-01"}],
           "column_info": {"ssn": {"dtype": "object", "min": SSN, "max": SSN},
                           "day": {"dtype": "datetime64[ns]", "min": "2026-01-01", "max": "2026-01-31"}}}
    hidden = CreateArtifactTool._build_viz_profile(None, viz, False)
    assert SSN not in json.dumps(hidden)
    assert hidden["columns"][1]["min"] == "2026-01-01"
    shown = CreateArtifactTool._build_viz_profile(None, viz, True)
    assert shown["columns"][0]["min"] == SSN


def test_a_long_quoted_cell_is_hidden_too():
    cell = "x" * 500
    exc = ValueError(f"could not convert string to float: '{cell}'")
    assert cell not in format_execution_failure(exc, f"Execution error: {exc}", False)
    quoted = ValueError('invalid literal for int(): "O\'Brien"')
    assert "Brien" not in format_execution_failure(quoted, f"Execution error: {quoted}", False)


def test_the_executor_retry_follows_the_organization_setting():
    exc = ValueError(f"could not convert string to float: '{SSN}'")
    exc.captured_stdout = SSN
    executor = StreamingCodeExecutor.__new__(StreamingCodeExecutor)
    executor.organization_settings = _Settings(False)
    assert SSN not in executor._retry_feedback(exc, f"Execution error: {exc}")
    executor.organization_settings = _Settings(True)
    assert SSN in executor._retry_feedback(exc, f"Execution error: {exc}")
    # Both retry sites use it (no call bypasses the setting).
    source = Path(code_execution.__file__).read_text()
    assert source.count("self._retry_feedback(e, msg)") == 2
    assert source.count("format_execution_failure(") == 2  # the definition and _retry_feedback


def test_write_csv_emits_the_model_fields_in_its_observation():
    source = Path(write_csv.__file__).read_text()
    observation = source[source.index("        observation = {"):source.index("        if current_step_id:")]
    assert '"data_preview": model_preview' in observation and '"stats": model_stats' in observation
    assert "data_preview\": data_preview" not in observation


@pytest.mark.parametrize("cell", [SSN + '"', "O'Brien " + SSN, "a\\b " + SSN, 'both \' and " ' + SSN])
def test_a_real_conversion_error_hides_cells_with_quotes_and_escapes(cell):
    try:
        float(cell)
    except ValueError as exc:
        hidden = format_execution_failure(exc, f"Execution error: {exc}", False)
    assert SSN not in hidden and "<hidden>" in hidden


def _conversion_errors():
    import pandas as pd

    yield lambda: int(SSN + "x" * 210)                 # repr cut before its closing quote
    yield lambda: pd.to_numeric(["x\"" + SSN])         # raw quote inside the quoted cell
    yield lambda: pd.to_numeric(["x\n" + SSN])         # raw line break inside it


@pytest.mark.parametrize("raise_it", list(_conversion_errors()), ids=["int-truncated", "pandas-quote", "pandas-newline"])
def test_library_conversion_errors_never_show_the_cell(raise_it):
    try:
        raise_it()
    except (ValueError, TypeError) as exc:
        hidden = format_execution_failure(exc, f"Execution error: {exc}", False)
    assert SSN not in hidden and "hidden" in hidden


def test_type_names_in_a_type_error_survive_redaction():
    try:
        1 + "a"
    except TypeError as exc:
        text = format_execution_failure(exc, f"Execution error: {exc}", False)
    assert "unsupported operand type(s)" in text
