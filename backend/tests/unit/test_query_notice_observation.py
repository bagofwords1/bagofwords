"""A data source's note on a result reaches the planner as a field of its own.

Connectors attach notes to the frames they return (``df.attrs["bow_notice"]``):
the row limit cut the result off, the request sent differs from the query as
written. Printed only into the run log, an agent skimmed past one and called a
row-capped result complete. The contract pinned here, for any connector: the
note rides the execution's per-query timing records and lands on the tool
observation as ``data_notices`` (flagged in the summary), and it is still
there once the observation has aged out of the planner's full window.
"""
import pandas as pd
import pytest

from app.ai.agents.planner.prompt_builder import PromptBuilder
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
from app.ai.tools.data_notices import attach_data_notices


class _RowCappedSource:
    """Any connector with a row limit: returns the first ``cap`` rows and says so."""

    def __init__(self, total, cap, label):
        self._total, self._cap, self._label = total, cap, label

    def execute_query(self, query=None, *args, **kwargs):
        df = pd.DataFrame({"name": [f"{self._label}-{i}" for i in range(self._total)]}).head(self._cap)
        if self._total > self._cap:
            df.attrs["bow_notice"] = f"{self._label}: only the first {self._cap} rows were returned."
        return df


async def _execute(code, clients):
    async def codegen(**kwargs):
        return code

    events = [e async for e in StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=CodeGenRequest(context=CodeGenContext(user_prompt="List everyone", schemas_excerpt=""), retries=1),
        ds_clients=clients, excel_files=[], code_generator_fn=codegen,
    )]
    done = events[-1]["payload"]
    assert done["df"] is not None, done.get("errors")
    return done


def _observation(rows):
    return {"summary": f"Created data 'People' successfully; {rows} rows x 1 cols.", "analysis_complete": False}


ONE_SOURCE = 'def generate_df(ds_clients, excel_files):\n    return ds_clients["a"].execute_query("q")\n'
TWO_SOURCES = '''def generate_df(ds_clients, excel_files):
    a = ds_clients["a"].execute_query("q1")
    b = ds_clients["b"].execute_query("q2")
    ds_clients["a"].execute_query("q3")
    return pd.concat([a, b])
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
@pytest.mark.parametrize("total,cap", [(30, 5), (1200, 1000)])
async def test_truncation_notice_reaches_the_observation(monkeypatch, mode, total, cap):
    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    done = await _execute(ONE_SOURCE, {"a": _RowCappedSource(total, cap, "Graph")})

    observation = _observation(len(done["df"]))
    attach_data_notices(observation, done["query_timings"])

    assert len(observation["data_notices"]) == 1
    assert f"first {cap} rows" in observation["data_notices"][0]
    # The one line every planner turn reads points at the notice.
    assert "data_notices" in observation["summary"]
    assert observation["summary"].startswith("Created data 'People' successfully")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
async def test_every_source_notice_is_kept_once_in_order(monkeypatch, mode):
    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    done = await _execute(TWO_SOURCES, {"a": _RowCappedSource(9, 4, "Alpha"),
                                        "b": _RowCappedSource(7, 2, "Beta")})

    observation = _observation(len(done["df"]))
    notices = attach_data_notices(observation, done["query_timings"])

    assert notices == observation["data_notices"]
    assert len(notices) == 2  # the repeated Alpha query adds no second copy
    assert notices[0].startswith("Alpha") and notices[1].startswith("Beta")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
async def test_a_complete_result_carries_no_notice(monkeypatch, mode):
    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    done = await _execute(ONE_SOURCE, {"a": _RowCappedSource(3, 50, "Graph")})

    observation = _observation(len(done["df"]))
    before = dict(observation)
    assert attach_data_notices(observation, done["query_timings"]) == []
    assert observation == before


def test_notice_survives_observation_compaction():
    """An older result may still be the one the answer rests on."""
    flagged = _observation(5)
    attach_data_notices(flagged, [{"index": 0, "rows": 5, "notice": "Source: only the first 5 rows were returned."}])
    history = [{"tool_name": "create_data", "observation": flagged, "loop_index": 0}]
    history += [{"tool_name": "describe_tables", "observation": {"summary": f"step {i}"}, "loop_index": i + 1}
                for i in range(8)]

    compacted = PromptBuilder._compact_past_observations(history)

    assert compacted[0]["data_notices"] == flagged["data_notices"]
    assert "data_notices" in compacted[0]["summary"]
