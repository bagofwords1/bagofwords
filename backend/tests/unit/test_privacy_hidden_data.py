"""With "Allow LLM to see data" off, no raw cell value reaches the model through
history, compaction, the previous-queries context, a code retry, or the
write_csv and visualization-profile observations; with it on, nothing changes.

Every leak is checked with a sentinel cell (an SSN) that must be absent with
the setting off, while the structure (columns, row counts, references,
parameters, date ranges, safe aggregates) stays.
"""
from __future__ import annotations

import asyncio
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
from app.ai.data_visibility import allow_llm_see_data, redact_error_text
from app.ai.context.parts import Outcome
from app.ai.persisted_summary import TOOL_CONTEXT_KIND_FIELD, TOOL_CONTEXT_OBSERVATION_KIND
from app.ai.tools.implementations.create_artifact import CreateArtifactTool
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
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
    assert SSN not in hidden and "hidden>" in hidden
    missing = AttributeError("'DataFrame' object has no attribute 'revenue'")
    missing.captured_stdout = SSN
    message = f"Execution error: {missing}"
    assert format_execution_failure(missing, message, False) == message


def _Settings(allowed):
    """Real organization settings with "Allow LLM to see data" on or off and
    every other setting at its default."""
    from app.models.organization_settings import OrganizationSettings

    return OrganizationSettings(config={"allow_llm_see_data": {
        "name": "Allow LLM to see data", "description": "", "value": allowed}})


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


def _retry_text(settings):
    """The error text the coder's second attempt receives, through the
    executor's public stream, for code that prints a cell and then fails
    converting it."""
    failing = (
        "def generate_df(ds_clients, excel_files):\n"
        "    import pandas as pd\n"
        f"    df = pd.DataFrame({{'ssn': ['{SSN}']}})\n"
        "    print(df)\n"
        "    float(df['ssn'][0])\n"
    )
    clean = "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame({'a': [1]})\n"
    calls = []

    async def _gen(**kwargs):
        calls.append(kwargs)
        return failing if len(calls) == 1 else clean

    async def go():
        async for _ in StreamingCodeExecutor(organization_settings=settings).generate_and_execute_stream_v2(
            request=CodeGenRequest(context=CodeGenContext(user_prompt="x", schemas_excerpt=""), retries=2),
            ds_clients={}, excel_files=[], code_generator_fn=_gen,
        ):
            pass

    asyncio.run(go())
    return calls[1]["code_and_error_messages"][-1][1]


def test_the_executor_retry_follows_the_organization_setting():
    assert SSN in _retry_text(_Settings(True))
    hidden = _retry_text(_Settings(False))
    assert SSN not in hidden and "could not convert string to float" in hidden


@pytest.mark.parametrize("cell", [SSN + '"', "O'Brien " + SSN, "a\\b " + SSN, 'both \' and " ' + SSN])
def test_a_real_conversion_error_hides_cells_with_quotes_and_escapes(cell):
    try:
        float(cell)
    except ValueError as exc:
        hidden = format_execution_failure(exc, f"Execution error: {exc}", False)
    assert SSN not in hidden and "hidden>" in hidden


def _conversion_errors():
    import pandas as pd

    yield lambda: int(SSN + "x" * 210)                 # repr cut before its closing quote
    yield lambda: pd.to_numeric(["x\"" + SSN])         # raw quote inside the quoted cell
    yield lambda: pd.to_numeric(["x\n" + SSN])         # raw line break inside it
    yield lambda: pd.to_numeric(['x"' + SSN + '"y'])  # balanced quotes inside it
    yield lambda: pd.to_datetime([SSN])                # no quotes: "unable to parse: <cell>"
    yield lambda: __import__("datetime").datetime.strptime("2024-01-01" + SSN, "%Y-%m-%d")
    yield lambda: pd.Series(["a"], dtype="category").__setitem__(0, SSN)  # "new category (<cell>)"


@pytest.mark.parametrize("raise_it", list(_conversion_errors()), ids=["int-truncated", "pandas-quote", "pandas-newline", "pandas-balanced", "pandas-datetime", "strptime", "categorical"])
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


def test_a_value_error_keeps_its_kind_of_problem():
    exc = ValueError(f"could not convert string to float: '{SSN}'")
    text = format_execution_failure(exc, f"Execution error: {exc}", False)
    assert text == "Execution error: could not convert string to float: <value hidden>"


def test_list_receipts_keep_their_ids_and_lose_their_keys():
    body = {"summary": "Saved 2 rows", "list_name": "Leads", "inserted": 1, "updated": 1,
            "rows": [{"row_id": "r1", "key": SSN, "action": "inserted"}, {"row_id": "r2", "key": None, "action": "updated"}]}
    hidden = hide_data_for_replay(body, "submit_list")
    assert hidden["rows"] == [{"row_id": "r1", "action": "inserted"}, {"row_id": "r2", "action": "updated"}]
    assert SSN not in json.dumps(hidden)


@pytest.mark.parametrize("type_name,message", [
    ("ValueError", f"could not convert string to float: '{SSN}'"),
    ("ParserError", f"Unknown string format: {SSN} present at position 0"),
    ("ConversionException", f"Conversion Error: Could not convert string '{SSN}' to INT32"),
    ("OutOfBoundsDatetime", f"Out of bounds nanosecond timestamp: {SSN}"),
])
def test_a_sandbox_error_is_judged_by_its_original_type(type_name, message):
    from app.ai.code_execution.sandbox.runner import SandboxExecutionError

    exc = SandboxExecutionError(type_name, message)
    hidden = format_execution_failure(exc, f"Execution error: {exc}", False)
    assert SSN not in hidden and hidden.startswith(f"Execution error: {type_name}: ")


def test_a_structural_sandbox_error_keeps_its_text():
    from app.ai.code_execution.sandbox.runner import SandboxExecutionError

    exc = SandboxExecutionError("NameError", "name 'revenue' is not defined")
    assert format_execution_failure(exc, f"Execution error: {exc}", False) == f"Execution error: {exc}"


def test_a_key_error_is_hidden_because_its_key_can_be_a_cell():
    from app.ai.code_execution.sandbox.runner import SandboxExecutionError

    for exc in (KeyError(SSN), SandboxExecutionError("KeyError", f"'{SSN}'")):  # df.loc[<cell>]
        hidden = format_execution_failure(exc, f"Execution error: {exc}", False)
        assert SSN not in hidden and "hidden>" in hidden


CELL = "alice@acme.com"


def _driver_errors():
    """Real driver errors that quote the cell, as a failed query raises them."""
    import psycopg2.errors
    import sqlalchemy.exc

    bad_text = psycopg2.errors.InvalidTextRepresentation(f'invalid input syntax for type integer: "{CELL}"')
    yield bad_text
    yield sqlalchemy.exc.DataError("SELECT email::int FROM people", {}, bad_text)
    snowflake = type("ProgrammingError", (Exception,), {})
    yield snowflake(f"100038 (22018): Numeric value '{CELL}' is not recognized")
    bad_request = type("BadRequest", (Exception,), {})
    yield bad_request(f"400 Bad int64 value: {CELL}")
    yield KeyError(CELL)


@pytest.mark.parametrize("exc", list(_driver_errors()), ids=["psycopg", "sqlalchemy-wrapped", "snowflake", "bigquery", "key-error"])
def test_a_driver_error_that_quotes_a_cell_is_cut(exc):
    message = f"Execution error: {exc}"
    assert CELL in format_execution_failure(exc, message)
    hidden = format_execution_failure(exc, message, False)
    assert CELL not in hidden and "hidden>" in hidden
    assert CELL not in redact_error_text(str(exc), type(exc).__name__)


def test_a_wrapped_driver_error_that_names_structure_keeps_its_text():
    import psycopg2.errors
    import sqlalchemy.exc

    missing = psycopg2.errors.UndefinedColumn('column "emaill" does not exist')
    exc = sqlalchemy.exc.ProgrammingError("SELECT emaill FROM people", {}, missing)
    message = f"Execution error: {exc}"
    assert format_execution_failure(exc, message, False) == message


class _FailingClient:
    """A connection whose query fails the way Postgres does on email::int."""

    def execute_query(self, query, *args, **kwargs):
        import psycopg2.errors
        import sqlalchemy.exc

        raise sqlalchemy.exc.DataError(query, {}, psycopg2.errors.InvalidTextRepresentation(
            f'invalid input syntax for type integer: "{CELL}"'))


def _failed_run(settings):
    """The final payload of a run whose every attempt fails on the query,
    through the executor's public stream: what create_data and inspect_data
    turn into the planner's observation (db_message, final_answer)."""
    code = "def generate_df(ds_clients, excel_files):\n    return ds_clients['main'].execute_query('SELECT email::int FROM people')\n"

    async def _gen(**kwargs):
        return code

    async def go():
        done = None
        async for event in StreamingCodeExecutor(organization_settings=settings).generate_and_execute_stream_v2(
            request=CodeGenRequest(context=CodeGenContext(user_prompt="x", schemas_excerpt=""), retries=2),
            ds_clients={"main": _FailingClient()}, excel_files=[], code_generator_fn=_gen,
        ):
            if event["type"] == "done":
                done = event["payload"]
        return done

    return asyncio.run(go())


@pytest.mark.parametrize("allowed", [True, False], ids=["visible", "hidden"])
def test_a_failed_query_records_its_error_without_the_cell_when_hidden(allowed):
    payload = _failed_run(_Settings(allowed))
    recorded = [t["error"] for t in payload["query_timings"] if t.get("error")]
    retries = [text for _code, text in payload["errors"]]
    assert recorded and retries
    seen = json.dumps([recorded, retries])
    if allowed:
        assert CELL in seen
    else:
        assert CELL not in seen and "invalid input syntax for type integer" in seen


def test_replay_cuts_error_text_recorded_while_data_was_visible():
    raw = f'(psycopg2.errors.InvalidTextRepresentation) invalid input syntax for type integer: "{CELL}"'
    observation = {
        "success": False, "terminal_execution_error": True, "final_answer": raw,
        "error": {"type": "infrastructure_failure", "message": f"Execution error: {raw}",
                  "detail": raw, "db_message": raw, "failed_sql": "SELECT email::int FROM people"},
        "retry_summary": {"attempts": 2, "last_error_message": raw},
        "query_timings": [{"sql": "SELECT email::int FROM people", "error": raw}],
    }
    for body in (observation, {"observation": observation}):
        hidden = hide_data_for_replay(body, "create_data")
        assert CELL not in json.dumps(hidden)
        assert "invalid input syntax for type integer" in json.dumps(hidden)
        assert "SELECT email::int FROM people" in json.dumps(hidden)


@pytest.mark.parametrize("settings,expected", [
    (None, True),
    (SimpleNamespace(), False),  # present but unreadable: never "yes" by accident
], ids=["no-settings", "unreadable"])
def test_one_reader_for_the_switch(settings, expected):
    from app.ai.tools.implementations._file_tool_common import allow_llm_see_data as file_tools_reader

    assert allow_llm_see_data(settings) is expected
    assert file_tools_reader({"organization": SimpleNamespace(settings=settings)}) is expected
    assert allow_llm_see_data(_Settings(False)) is False and allow_llm_see_data(_Settings(True)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False], ids=["visible", "hidden"])
async def test_write_csv_run_shows_the_model_cells_only_when_allowed(tmp_path, monkeypatch, allowed):
    """Through the tool's public stream (fake coder and DB, as in
    test_write_csv_output.py): the observation follows the setting; the
    output the UI renders keeps the rows either way."""
    import os

    from app.ai.tools.implementations import write_csv as write_csv_module

    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return pd.DataFrame({{'name': ['Jane', 'Joe'], 'ssn': ['{SSN}', '219-09-9999']}})\n"
    )

    class _Coder:
        def __init__(self, **_kwargs):
            pass

        async def generate_transform_code(self, **_kwargs):
            return code

    class _Db:
        def add(self, obj):
            obj.id = obj.id or "file-1"

        async def flush(self):
            pass

        async def execute(self, *_args, **_kwargs):
            pass

    async def _no_audit(*_args, **_kwargs):
        pass

    monkeypatch.chdir(tmp_path)
    os.makedirs(os.path.join("uploads", "files"))
    monkeypatch.setattr("app.ai.agents.coder.coder.Coder", _Coder)
    monkeypatch.setattr(write_csv_module, "log_tool_audit", _no_audit)
    runtime_ctx = {"settings": _Settings(allowed), "db": _Db(), "ds_clients": {}, "excel_files": []}
    events = [e async for e in write_csv_module.WriteCsvTool().run_stream(
        {"user_prompt": "People", "title": "People"}, runtime_ctx)]
    end = events[-1].payload
    assert end["output"]["success"], end["output"].get("error_message")
    assert SSN in json.dumps(end["output"], default=str)
    assert (SSN in json.dumps(end["observation"], default=str)) is allowed
    assert end["observation"]["row_count"] == 2
