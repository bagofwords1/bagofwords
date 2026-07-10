"""Concurrent multi-tool dispatch — deterministic invariants.

Covers the concurrency machinery introduced for parallel tool execution:

- `Agent._dispatch_action_batch`: serial default, cap-bounded overlap,
  per-data-source serialization, unsafe-tool gate, crash/sigkill handling,
  action-order preservation, serial state chaining.
- `Agent._aggregate_batch_observation`: single-action parity, per-action
  entries, partial-vs-total failure, analysis_complete/final_answer/image
  propagation, accept-cap `not_executed` reporting.
- `Agent._adopt_invocation_outcomes` / `_new_invocation_state`: reset-scope
  semantics and last-writer adoption.
- Caps: ai_tool_concurrency org setting (BOW_AGENT_TOOL_CONCURRENCY env
  override) / BOW_AGENT_MAX_ACTIONS_PER_DECISION.
- `ToolRunner`: per-tool validation-failure streaks (no cross-tool resets).
- Observation history: same-iteration observations stay full; the planner
  prompt compaction keeps the whole last batch full regardless of size.
- `_ThreadLocalStdoutRouter`: per-thread capture without cross-talk, and
  genuinely overlapping code executions (the old global lock serialized them).

Everything here is stubbed at the boundaries (no LLM, no DB writes) and
CI-safe. The real-environment leg lives in the sandbox feedback loop doc.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.ai.agent_v2 import (
    AgentV2,
    ToolInvocationState,
    _AgentStateProxy,
    _PARALLEL_SAFE_TOOLS,
)
from app.ai.runner.tool_runner import ToolRunner
from app.ai.runner.policies import RetryPolicy, TimeoutPolicy
from app.ai.context.builders.observation_context_builder import ObservationContextBuilder
from app.ai.agents.planner.prompt_builder import PromptBuilder, _RECENT_OBS_FULL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeAction:
    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments or {}
        self.type = "tool_call"


def make_agent(**env_current) -> AgentV2:
    """Bare Agent instance with just the state the dispatch helpers touch."""
    agent = AgentV2.__new__(AgentV2)
    agent.sigkill_event = asyncio.Event()
    agent.current_widget = env_current.get("widget")
    agent.current_query = env_current.get("query")
    agent.current_step = env_current.get("step")
    agent.current_step_id = env_current.get("step_id")
    agent.current_visualization = env_current.get("visualization")
    agent._tool_db_lock = asyncio.Lock()
    return agent


def actions_for_sources(tool, sources):
    return [
        FakeAction(tool, {"tables_by_source": [{"data_source_id": ds, "tables": ["t"]}]})
        for ds in sources
    ]


class OverlapTracker:
    """Tracks in-flight concurrency and per-key execution windows."""

    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        self.windows = {}  # key -> (start, end)

    def runner(self, sleep_s=0.05, fail_for=(), crash_for=()):
        async def run_one(idx, action, block_id, inv):
            if idx in crash_for:
                raise RuntimeError(f"boom-{idx}")
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            start = time.monotonic()
            await asyncio.sleep(sleep_s)
            self.in_flight -= 1
            self.windows[idx] = (start, time.monotonic())
            obs = {"summary": f"done-{idx}"}
            if idx in fail_for:
                obs["error"] = {"code": "runtime_error", "message": f"failed-{idx}"}
            return {
                "index": idx,
                "tool_name": action.name,
                "tool_input": action.arguments,
                "action": action,
                "observation": obs,
                "tool_output": None,
                "tool_execution": None,
                "block_id": block_id,
                "inv": inv,
                "created_widget_id": None,
                "created_step_id": None,
                "created_visualization_ids": None,
                "skipped": False,
            }
        return run_one


# ---------------------------------------------------------------------------
# Dispatch harness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_is_serial(monkeypatch):
    monkeypatch.delenv("BOW_AGENT_TOOL_CONCURRENCY", raising=False)
    agent = make_agent()
    tracker = OverlapTracker()
    actions = actions_for_sources("inspect_data", [f"ds{i}" for i in range(5)])
    outcomes = await agent._dispatch_action_batch(actions, [None] * 5, tracker.runner())
    assert len(outcomes) == 5
    assert tracker.max_in_flight == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("n_actions,cap", [(2, 2), (5, 4), (10, 8)])
async def test_concurrent_overlap_bounded_by_cap(monkeypatch, n_actions, cap):
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", str(cap))
    agent = make_agent()
    tracker = OverlapTracker()
    actions = actions_for_sources("inspect_data", [f"ds{i}" for i in range(n_actions)])
    started = time.monotonic()
    outcomes = await agent._dispatch_action_batch(actions, [None] * n_actions, tracker.runner(sleep_s=0.1))
    wall = time.monotonic() - started
    assert len(outcomes) == n_actions
    # Genuine overlap happened...
    assert tracker.max_in_flight > 1
    # ...bounded by the cap...
    assert tracker.max_in_flight <= cap
    # ...and wall-clock beats serial by a comfortable margin.
    serial_floor = n_actions * 0.1
    assert wall < serial_floor * 0.8


@pytest.mark.asyncio
async def test_outcomes_preserve_action_order_under_concurrency(monkeypatch):
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "8")
    agent = make_agent()

    async def run_one(idx, action, block_id, inv):
        # Later actions finish FIRST — order must still be action order.
        await asyncio.sleep((10 - idx) * 0.01)
        return {"index": idx, "tool_name": action.name, "tool_input": action.arguments,
                "action": action, "observation": {"summary": f"s{idx}"}, "inv": inv,
                "skipped": False}

    actions = actions_for_sources("inspect_data", [f"ds{i}" for i in range(8)])
    outcomes = await agent._dispatch_action_batch(actions, [None] * 8, run_one)
    assert [o["index"] for o in outcomes] == list(range(8))


@pytest.mark.asyncio
async def test_unsafe_tool_forces_batch_serial(monkeypatch):
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "8")
    agent = make_agent()
    tracker = OverlapTracker()
    actions = actions_for_sources("inspect_data", ["ds1", "ds2"]) + [FakeAction("send_email", {})]
    assert "send_email" not in _PARALLEL_SAFE_TOOLS
    outcomes = await agent._dispatch_action_batch(actions, [None] * 3, tracker.runner())
    assert len(outcomes) == 3
    assert tracker.max_in_flight == 1


@pytest.mark.asyncio
async def test_same_source_actions_serialize_distinct_sources_overlap(monkeypatch):
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "8")
    agent = make_agent()
    tracker = OverlapTracker()
    # 0,1 share a source; 2,3 have their own.
    actions = actions_for_sources("inspect_data", ["shared", "shared", "solo-a", "solo-b"])
    outcomes = await agent._dispatch_action_batch(actions, [None] * 4, tracker.runner(sleep_s=0.08))
    assert len(outcomes) == 4
    w = tracker.windows
    # Same source: execution windows must not overlap.
    assert w[0][1] <= w[1][0] or w[1][1] <= w[0][0]
    # Distinct sources overlapped with something (batch parallelism worked).
    assert tracker.max_in_flight > 1


@pytest.mark.asyncio
async def test_crashed_action_becomes_error_outcome(monkeypatch):
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "4")
    agent = make_agent()
    tracker = OverlapTracker()
    actions = actions_for_sources("inspect_data", ["a", "b", "c"])
    outcomes = await agent._dispatch_action_batch(actions, [None] * 3, tracker.runner(crash_for={1}))
    assert len(outcomes) == 3
    crashed = outcomes[1]
    assert crashed["skipped"] is True
    assert crashed["observation"]["error"]["code"] == "runtime_error"
    # Siblings unaffected
    assert outcomes[0]["observation"]["summary"] == "done-0"
    assert outcomes[2]["observation"]["summary"] == "done-2"


@pytest.mark.asyncio
async def test_sigkill_stops_concurrent_batch(monkeypatch):
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "2")
    agent = make_agent()
    agent.sigkill_event.set()
    tracker = OverlapTracker()
    actions = actions_for_sources("inspect_data", ["a", "b", "c"])
    outcomes = await agent._dispatch_action_batch(actions, [None] * 3, tracker.runner())
    assert all(o["skipped"] for o in outcomes)
    assert all(o["observation"].get("stopped") for o in outcomes)


@pytest.mark.asyncio
async def test_sigkill_stops_serial_batch_early(monkeypatch):
    monkeypatch.delenv("BOW_AGENT_TOOL_CONCURRENCY", raising=False)
    agent = make_agent()
    ran = []

    async def run_one(idx, action, block_id, inv):
        ran.append(idx)
        agent.sigkill_event.set()  # first action triggers stop
        return {"index": idx, "tool_name": action.name, "tool_input": {}, "action": action,
                "observation": {"summary": "ok"}, "inv": inv, "skipped": False}

    actions = actions_for_sources("inspect_data", ["a", "b", "c"])
    outcomes = await agent._dispatch_action_batch(actions, [None] * 3, run_one)
    assert ran == [0]
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_serial_batch_chains_invocation_state():
    agent = make_agent()
    seeded_step_ids = []

    async def run_one(idx, action, block_id, inv):
        seeded_step_ids.append(inv.current_step_id)
        inv.current_step_id = f"step-{idx}"
        return {"index": idx, "tool_name": action.name, "tool_input": {}, "action": action,
                "observation": {"summary": "ok"}, "inv": inv, "skipped": False}

    # read_query is NOT a reset-scope tool: it inherits the previous state.
    actions = [FakeAction("read_query", {}), FakeAction("read_query", {})]
    await agent._dispatch_action_batch(actions, [None, None], run_one)
    # Second action saw the first action's created step (old serial behavior).
    assert seeded_step_ids == [None, "step-0"]
    assert agent.current_step_id == "step-1"


@pytest.mark.asyncio
async def test_scale_many_iterations_of_max_batches(monkeypatch):
    """20 iterations x 10 actions: order, attribution, and no state bleed."""
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "8")
    agent = make_agent()
    total = 0
    for iteration in range(20):
        tracker = OverlapTracker()
        actions = actions_for_sources("inspect_data", [f"it{iteration}-ds{i}" for i in range(10)])
        block_ids = [f"blk-{iteration}-{i}" for i in range(10)]
        outcomes = await agent._dispatch_action_batch(actions, block_ids, tracker.runner(sleep_s=0.005))
        assert [o["index"] for o in outcomes] == list(range(10))
        assert [o["block_id"] for o in outcomes] == block_ids
        total += len(outcomes)
    assert total == 200


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _outcome(tool="inspect_data", obs=None, skipped=False):
    return {"tool_name": tool, "observation": obs or {"summary": "ok"}, "skipped": skipped,
            "tool_input": {}, "inv": None}


def test_single_action_observation_is_passed_through_verbatim():
    obs = {"summary": "only", "step_id": "s1"}
    result = AgentV2._aggregate_batch_observation([_outcome(obs=obs)], [])
    assert result is obs  # exact parity with the serial path


def test_multi_action_aggregate_has_per_action_entries_and_counts():
    outcomes = [
        _outcome(obs={"summary": "a ok", "step_id": "s1"}),
        _outcome(obs={"summary": "b failed", "error": {"code": "x", "message": "m"}}),
        _outcome(obs={"summary": "c ok", "created_visualization_ids": ["v1"]}),
    ]
    agg = AgentV2._aggregate_batch_observation(outcomes, [])
    assert len(agg["parallel_actions"]) == 3
    assert agg["parallel_actions"][0]["step_id"] == "s1"
    assert agg["parallel_actions"][1]["error"]["code"] == "x"
    assert agg["parallel_actions"][2]["created_visualization_ids"] == ["v1"]
    # Partial failure: batch-level error NOT set (would trip circuit breakers)
    assert "error" not in agg


def test_all_failed_batch_is_marked_failed():
    outcomes = [
        _outcome(obs={"summary": "a", "error": {"code": "x"}}),
        _outcome(obs={"summary": "b", "error": {"code": "y"}}),
    ]
    agg = AgentV2._aggregate_batch_observation(outcomes, [])
    assert agg["error"]["code"] == "parallel_all_failed"


def test_analysis_complete_and_final_answer_propagate():
    outcomes = [
        _outcome(obs={"summary": "a"}),
        _outcome(obs={"summary": "b", "analysis_complete": True, "final_answer": "done!"}),
    ]
    agg = AgentV2._aggregate_batch_observation(outcomes, [])
    assert agg["analysis_complete"] is True
    assert agg["final_answer"] == "done!"


def test_images_hoisted_to_batch_level():
    img = {"data": "abc", "media_type": "image/png"}
    outcomes = [
        _outcome(obs={"summary": "a", "images": [img]}),
        _outcome(obs={"summary": "b"}),
    ]
    agg = AgentV2._aggregate_batch_observation(outcomes, [])
    assert agg["images"] == [img]


def test_dropped_actions_reported_as_not_executed():
    dropped = [FakeAction("inspect_data"), FakeAction("create_data")]
    agg = AgentV2._aggregate_batch_observation([_outcome(), _outcome()], dropped)
    names = [e["tool_name"] for e in agg["not_executed"]]
    assert names == ["inspect_data", "create_data"]


def test_empty_batch_returns_none():
    assert AgentV2._aggregate_batch_observation([], []) is None


# ---------------------------------------------------------------------------
# Invocation state
# ---------------------------------------------------------------------------

def test_reset_tools_get_fresh_invocation_state():
    agent = make_agent(step_id="old-step", query="old-query", widget="w")
    inv = agent._new_invocation_state("create_data")
    assert inv.current_step_id is None and inv.current_query is None
    assert inv.current_widget == "w"  # widget scope is not reset


def test_non_reset_tools_inherit_current_state():
    agent = make_agent(step_id="old-step", query="old-query")
    inv = agent._new_invocation_state("read_query")
    assert inv.current_step_id == "old-step"
    assert inv.current_query == "old-query"


def test_adoption_last_writer_wins_and_reset_clears_stale_state():
    agent = make_agent(step_id="stale-step")
    inv1 = ToolInvocationState(step_id="s1")
    inv2 = ToolInvocationState(step_id="s2")
    agent._adopt_invocation_outcomes([
        {"tool_name": "create_data", "inv": inv1},
        {"tool_name": "create_data", "inv": inv2},
    ])
    assert agent.current_step_id == "s2"

    # A failed reset-scope batch (created nothing) clears stale state.
    agent2 = make_agent(step_id="stale-step")
    agent2._adopt_invocation_outcomes([{"tool_name": "create_data", "inv": ToolInvocationState()}])
    assert agent2.current_step_id is None


def test_agent_state_proxy_reads_and_writes_agent_fields():
    agent = make_agent(step_id="s0")
    proxy = _AgentStateProxy(agent)
    assert proxy.current_step_id == "s0"
    proxy.current_step_id = "s1"
    assert agent.current_step_id == "s1"


def test_action_data_source_keys_dedupes_and_sorts():
    a = FakeAction("inspect_data", {"tables_by_source": [
        {"data_source_id": "b"}, {"data_source_id": "a"}, {"data_source_id": "b"},
    ]})
    assert AgentV2._action_data_source_keys(a) == ["a", "b"]
    assert AgentV2._action_data_source_keys(FakeAction("x", {})) == []


# ---------------------------------------------------------------------------
# Concurrency caps: org setting + env override
# ---------------------------------------------------------------------------

class FakeOrgSettings:
    """Minimal organization_settings stand-in exposing get_config()."""

    def __init__(self, value):
        self._value = value

    def get_config(self, key, default=None):
        assert key == "ai_tool_concurrency"
        return SimpleNamespace(value=self._value)


def test_caps_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("BOW_AGENT_TOOL_CONCURRENCY", raising=False)
    monkeypatch.delenv("BOW_AGENT_MAX_ACTIONS_PER_DECISION", raising=False)
    agent = make_agent()
    assert agent._tool_concurrency() == 1  # no org settings, no env -> serial
    assert AgentV2._max_actions_per_decision() == 10

    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "100")
    assert agent._tool_concurrency() <= 8  # never above the code-exec pool

    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "garbage")
    assert agent._tool_concurrency() == 1

    monkeypatch.setenv("BOW_AGENT_MAX_ACTIONS_PER_DECISION", "0")
    assert AgentV2._max_actions_per_decision() >= 1


def test_org_setting_governs_concurrency(monkeypatch):
    monkeypatch.delenv("BOW_AGENT_TOOL_CONCURRENCY", raising=False)
    agent = make_agent()

    agent.organization_settings = FakeOrgSettings(4)
    assert agent._tool_concurrency() == 4

    # Clamped to the code-exec pool ceiling and to >= 1
    agent.organization_settings = FakeOrgSettings(100)
    assert agent._tool_concurrency() == 8
    agent.organization_settings = FakeOrgSettings(0)
    assert agent._tool_concurrency() == 1
    agent.organization_settings = FakeOrgSettings(None)
    assert agent._tool_concurrency() == 1
    agent.organization_settings = FakeOrgSettings("not-a-number")
    assert agent._tool_concurrency() == 1


def test_env_var_overrides_org_setting(monkeypatch):
    agent = make_agent()
    agent.organization_settings = FakeOrgSettings(4)
    monkeypatch.setenv("BOW_AGENT_TOOL_CONCURRENCY", "2")
    assert agent._tool_concurrency() == 2
    monkeypatch.delenv("BOW_AGENT_TOOL_CONCURRENCY", raising=False)
    assert agent._tool_concurrency() == 4


@pytest.mark.asyncio
async def test_dispatch_uses_org_setting_concurrency(monkeypatch):
    """End-to-end through _dispatch_action_batch: the org setting alone
    (no env var) must produce genuine overlap."""
    monkeypatch.delenv("BOW_AGENT_TOOL_CONCURRENCY", raising=False)
    agent = make_agent()
    agent.organization_settings = FakeOrgSettings(5)
    tracker = OverlapTracker()
    actions = actions_for_sources("inspect_data", [f"ds{i}" for i in range(5)])
    outcomes = await agent._dispatch_action_batch(actions, [None] * 5, tracker.runner(sleep_s=0.1))
    assert len(outcomes) == 5
    assert tracker.max_in_flight > 1
    assert tracker.max_in_flight <= 5


# ---------------------------------------------------------------------------
# ToolRunner per-tool validation streaks
# ---------------------------------------------------------------------------

class _StrictInput(BaseModel):
    required_field: str


class _ValidatingTool:
    name = "strict_tool"
    input_model = _StrictInput
    output_model = None

    class metadata:
        allowed_modes = None

    async def run_stream(self, arguments, runtime_ctx):
        yield {"type": "tool.end", "payload": {"observation": {"summary": "ok"}, "output": None}}


class _OtherTool(_ValidatingTool):
    name = "other_tool"


async def _noop_emit(ev):
    pass


@pytest.mark.asyncio
async def test_validation_failures_tracked_per_tool_not_globally():
    runner = ToolRunner(retry=RetryPolicy(max_attempts=1), timeout=TimeoutPolicy(5, 5, 5))
    a, b = _ValidatingTool(), _OtherTool()

    r1 = await runner.run(a, {}, {}, _noop_emit)  # invalid input for a
    assert r1["error"]["type"] == "validation_error"

    # A SUCCESS on tool b must not reset tool a's streak
    rb = await runner.run(b, {"required_field": "x"}, {}, _noop_emit)
    assert "error" not in rb.get("observation", {})

    r2 = await runner.run(a, {}, {}, _noop_emit)  # second invalid input for a
    assert r2["error"]["type"] == "repeated_validation_error"
    assert r2.get("analysis_complete") is True

    # And tool b's own streak is independent
    rb1 = await runner.run(b, {}, {}, _noop_emit)
    assert rb1["error"]["type"] == "validation_error"


@pytest.mark.asyncio
async def test_success_resets_only_that_tools_streak():
    runner = ToolRunner(retry=RetryPolicy(max_attempts=1), timeout=TimeoutPolicy(5, 5, 5))
    a = _ValidatingTool()
    await runner.run(a, {}, {}, _noop_emit)               # streak 1
    await runner.run(a, {"required_field": "x"}, {}, _noop_emit)  # success -> reset
    r = await runner.run(a, {}, {}, _noop_emit)
    assert r["error"]["type"] == "validation_error"       # streak restarted, not terminal


# ---------------------------------------------------------------------------
# Observation history: same-iteration exemption + prompt compaction window
# ---------------------------------------------------------------------------

def _fat_create_data_obs(n_rows=20):
    return {
        "summary": "rows",
        "data_preview": {"rows": [{"v": i} for i in range(n_rows)], "row_count": n_rows},
    }


def test_same_iteration_observations_not_compacted_by_batch_mates():
    builder = ObservationContextBuilder()
    for i in range(5):
        builder.add_tool_observation("create_data", {"i": i}, _fat_create_data_obs(), loop_index=7)
    # All five batch members keep their full previews
    for entry in builder.tool_observations:
        assert entry["loop_index"] == 7
        assert len(entry["observation"]["data_preview"]["rows"]) == 20

    # The NEXT iteration compacts the previous batch
    builder.add_tool_observation("create_data", {"i": 99}, _fat_create_data_obs(), loop_index=8)
    for entry in builder.tool_observations[:5]:
        assert entry["observation"]["data_preview"].get("sampled") is True
        assert len(entry["observation"]["data_preview"]["rows"]) < 20


def test_prompt_compaction_keeps_entire_last_batch_full():
    past = []
    # 10 old single-action iterations
    for i in range(10):
        past.append({"tool_name": "inspect_data", "execution_number": i, "loop_index": i,
                     "observation": {"summary": f"old-{i}", "details": "x" * 50}})
    # one final batch of 8 parallel actions — larger than _RECENT_OBS_FULL
    batch_size = _RECENT_OBS_FULL + 3
    for j in range(batch_size):
        past.append({"tool_name": "inspect_data", "execution_number": 10 + j, "loop_index": 42,
                     "observation": {"summary": f"batch-{j}", "details": "y" * 50}})

    compacted = PromptBuilder._compact_past_observations(past)
    tail = compacted[-batch_size:]
    # Whole batch survives in full (has 'observation' payload with details)
    assert all("observation" in o and "details" in o["observation"] for o in tail)
    # Old entries beyond the window got minified (no raw details)
    assert all("observation" not in o for o in compacted[: len(past) - batch_size - _RECENT_OBS_FULL])


def test_prompt_compaction_without_loop_index_behaves_like_before():
    past = [{"tool_name": "t", "execution_number": i,
             "observation": {"summary": f"s{i}", "details": "z"}} for i in range(12)]
    compacted = PromptBuilder._compact_past_observations(past)
    assert all("observation" in o for o in compacted[-_RECENT_OBS_FULL:])
    assert all("observation" not in o for o in compacted[: 12 - _RECENT_OBS_FULL])


# ---------------------------------------------------------------------------
# Stdout router
# ---------------------------------------------------------------------------

def test_stdout_router_no_cross_talk_between_threads():
    from app.ai.code_execution.code_execution import _stdout_router
    import io

    router = _stdout_router()
    results = {}
    barrier = threading.Barrier(4)

    def worker(tag):
        buf = io.StringIO()
        router.bind(buf)
        try:
            barrier.wait(timeout=5)
            for i in range(200):
                print(f"{tag}-{i}")
        finally:
            router.unbind()
        results[tag] = buf.getvalue()

    threads = [threading.Thread(target=worker, args=(f"t{k}",)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for tag, out in results.items():
        lines = [l for l in out.splitlines() if l]
        assert len(lines) == 200
        assert all(l.startswith(f"{tag}-") for l in lines), f"cross-talk into {tag}"


def test_stdout_router_install_is_idempotent():
    from app.ai.code_execution.code_execution import _stdout_router, _ThreadLocalStdoutRouter
    r1 = _stdout_router()
    r2 = _stdout_router()
    assert r1 is r2
    assert isinstance(r1, _ThreadLocalStdoutRouter)


@pytest.mark.asyncio
async def test_code_executions_overlap_without_global_lock():
    """Two executions whose generate_df blocks on I/O must overlap.

    Under the old _STDOUT_REDIRECT_LOCK this took ~2x the sleep; with the
    per-thread router it takes ~1x. This is THE unlock for running
    create_data/inspect_data against several data sources at once.
    """
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    class SleepyClient:
        def execute_query(self, sql):
            import pandas as pd
            time.sleep(0.4)  # simulates an I/O-bound warehouse query
            return pd.DataFrame({"a": [1]})

    code = (
        "def generate_df(ds_clients, excel_files):\n"
        "    print('starting query')\n"
        "    df = ds_clients['src'].execute_query('SELECT 1')\n"
        "    print('query done')\n"
        "    return df\n"
    )

    executor = StreamingCodeExecutor(organization_settings=None)

    async def one():
        return await executor.execute_code_async(
            code=code, ds_clients={"src": SleepyClient()}, excel_files=[],
        )

    started = time.monotonic()
    (df1, log1, _), (df2, log2, _) = await asyncio.gather(one(), one())
    wall = time.monotonic() - started

    assert len(df1) == 1 and len(df2) == 1
    # stdout captured per execution, no cross-talk, both got both lines
    for log in (log1, log2):
        assert log.count("starting query") == 1
        assert log.count("query done") == 1
    # Overlap: two 0.4s queries in well under 0.8s
    assert wall < 0.7, f"executions serialized: wall={wall:.2f}s"
