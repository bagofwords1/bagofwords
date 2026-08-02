"""Feedback loop — "the agent gets stuck re-reading a query it was asked to
explain".

The planner consumes ONLY the tool observation (agent_v2 passes
`last_observation` / `past_observations` into PlannerInput — the tool `output`
never reaches the model). read_query's whole headline use case is "look at
previously written code", but `code` lived only on the OUTPUT, so a question
about the query itself ("why does this return 0 rows when the same SQL works
directly?") was unanswerable from what the model actually received — and it
re-read the same query instead of answering.

Invariant under test: a successful read_query surfaces the generated code in
the observation, the way read_artifact does, for both the single-id and
multi-id forms — and that code is compacted away one iteration later so it
doesn't stack up across the run.
"""
from __future__ import annotations

import json

import pytest

from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.tools.implementations.read_query import ReadQueryTool

SQL_A = "SELECT id, total FROM orders WHERE status = 'cancelled' AND 1 = 0"
SQL_B = "SELECT id, total FROM orders WHERE status = 'cancelled'"


class _Step:
    def __init__(self, step_id, code, title):
        self.id = step_id
        self.code = code
        self.title = title
        self.data = {
            "columns": [{"field": "id", "headerName": "id"}],
            "rows": [{"id": 1}],
            "info": {"total_rows": 1},
        }
        self.data_model = {"type": "table"}
        self.view = None


class _Query:
    def __init__(self, query_id, code, title):
        self.id = query_id
        self.title = title
        self.default_step = _Step(f"{query_id}-step", code, title)
        self.steps = [self.default_step]


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Returns the queued rows in call order: read_query looks up the Query
    first, then its Visualization."""

    def __init__(self, values):
        self._values = list(values)

    async def execute(self, *args, **kwargs):
        return _Result(self._values.pop(0) if self._values else None)


class _Org:
    id = "org-1"


class _Report:
    id = "report-1"


async def _observation(tool_input, rows):
    runtime_ctx = {"db": _FakeDB(rows), "organization": _Org(), "report": _Report()}
    events = [e async for e in ReadQueryTool().run_stream(tool_input, runtime_ctx)]
    payload = events[-1].payload
    assert payload["output"]["success"] is True, payload
    return payload["observation"]


@pytest.mark.asyncio
async def test_single_read_surfaces_code_in_observation():
    obs = await _observation(
        {"query_ids": ["q1"]},
        [_Query("q1", SQL_A, "Cancelled orders"), None],
    )
    # The model must be able to see WHAT THE QUERY RUNS, not just its title.
    assert SQL_A in json.dumps(obs, ensure_ascii=False, default=str)
    assert obs["code"] == SQL_A


@pytest.mark.asyncio
async def test_multi_read_surfaces_code_per_result():
    obs = await _observation(
        {"query_ids": ["q1", "q2"]},
        [_Query("q1", SQL_A, "Zero rows"), None, _Query("q2", SQL_B, "All rows"), None],
    )
    codes = [entry.get("code") for entry in obs["results_summary"]]
    assert codes == [SQL_A, SQL_B]


@pytest.mark.asyncio
async def test_code_is_compacted_on_the_next_iteration():
    single = await _observation(
        {"query_ids": ["q1"]}, [_Query("q1", SQL_A, "Cancelled orders"), None]
    )
    multi = await _observation(
        {"query_ids": ["q1", "q2"]},
        [_Query("q1", SQL_A, "Zero rows"), None, _Query("q2", SQL_B, "All rows"), None],
    )

    builder = ObservationContextBuilder()
    builder.add_tool_observation("read_query", {"query_ids": ["q1"]}, single, loop_index=0)
    builder.add_tool_observation("read_query", {"query_ids": ["q1", "q2"]}, multi, loop_index=1)
    # A later call from a further iteration compacts everything before it.
    builder.add_tool_observation("create_data", {}, {"summary": "built"}, loop_index=2)

    assert "code" not in single
    assert single["code_compacted"] == f"{len(SQL_A)} chars"
    for entry in multi["results_summary"]:
        assert "code" not in entry
        assert entry["code_compacted"].endswith(" chars")
    # The summary survives compaction, so the planner still knows what it read
    # and can re-read deliberately instead of by accident.
    assert single["summary"]
