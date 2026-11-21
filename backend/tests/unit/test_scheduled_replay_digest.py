"""Scheduled runs replay earlier runs' tool results as digests.

A scheduled task re-fetches live data every run, so resending every earlier
run's full payload (a custom API response can be half the context window)
only grows the prompt run over run. ``digest_prior_results`` replays each
successful prior result as its summary plus its ids instead. Invariants:

- the replayed size of a prior result does not depend on its payload size;
- the ids a later run needs to re-fetch the data survive;
- failed results stay in full (they say what went wrong);
- interactive replay (the default) is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.context.durable_transcript import build_durable_transcript
from app.ai.context.parts import Tier, ToolResultPart


def _run(idx: int, payload_rows: int, *, status: str = "success"):
    """One prior run: a completion with one execute_mcp call whose result
    carries ``payload_rows`` rows of API data."""
    completion = SimpleNamespace(id=f"run-{idx}", role="system")
    execution_id = f"te-{idx}"
    block = SimpleNamespace(
        tool_execution_id=execution_id,
        plan_decision_id=f"decision-{idx}",
        agent_execution_id=f"agent-{idx}",
        block_index=0,
    )
    rows = [{"id": n, "region": f"region-{n % 7}", "amount": n * 1.5} for n in range(payload_rows)]
    failed = status != "success"
    tool = SimpleNamespace(
        id=execution_id,
        action_index=0,
        provider_call_id=f"call-{idx}",
        provider_name=None,
        provider_signature=None,
        tool_name="execute_mcp",
        arguments_json={"tool": "orders_api"},
        status=status,
        context_summary_json=None if failed else {
            "summary": f"orders_api → {payload_rows} rows",
            "file_id": f"file-{idx}",
            "query_id": f"query-{idx}",
            "preview": rows,
        },
        result_json=None,
        result_summary=None if failed else f"orders_api → {payload_rows} rows",
        error_message="upstream returned 502" if failed else None,
    )
    return completion, block, tool


def _build(runs, **kwargs):
    completions, blocks, tools = [], {}, {}
    for completion, block, tool in runs:
        completions.append(completion)
        blocks[completion.id] = [block]
        tools[tool.id] = tool
    transcript, _ = build_durable_transcript(
        completions=completions, blocks_by_completion=blocks, tool_exec_by_id=tools, **kwargs
    )
    return transcript


def _results(transcript):
    return [p for t in transcript.turns for p in t.parts if isinstance(p, ToolResultPart)]


@pytest.mark.parametrize("payload_rows", [10, 1_000, 20_000])
def test_prior_result_replays_at_a_size_independent_of_its_payload(payload_rows):
    transcript = _build([_run(1, payload_rows)], digest_prior_results=True)
    [result] = _results(transcript)

    assert result.tier is Tier.DIGEST
    text = result.model_text()
    assert len(text) < 500, text
    assert result.tokens < 200
    # Re-fetchable: the handles a later run needs survive the digest.
    assert "file_id=file-1" in text
    assert "query_id=query-1" in text
    assert f"{payload_rows} rows" in text


def test_prompt_grows_by_a_constant_per_prior_run_not_by_payload():
    small = _build([_run(i, 10) for i in range(20)], digest_prior_results=True)
    large = _build([_run(i, 20_000) for i in range(20)], digest_prior_results=True)

    # Same number of runs, payloads 2000x apart: the replayed size is the same
    # order — it tracks the run count, not the data volume.
    assert large.tokens() < small.tokens() * 1.5
    assert large.tokens() < 20 * 200


def test_failed_prior_results_stay_in_full():
    transcript = _build([_run(1, 0, status="error")], digest_prior_results=True)
    [result] = _results(transcript)

    assert result.tier is Tier.FULL
    assert "upstream returned 502" in result.model_text()


def test_interactive_replay_is_unchanged_by_default():
    transcript = _build([_run(1, 1_000)])
    [result] = _results(transcript)

    assert result.tier is Tier.FULL
    assert '"region":"region-' in result.model_text()
