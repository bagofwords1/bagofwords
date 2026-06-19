"""Unit tests for the per-query timeout enforced by QueryCapturingClientWrapper
and the resolver that picks the per-connection budget.

These tests use small wall-clock waits (50–250ms) so the suite stays fast while
still exercising real thread join semantics — the wrapper relies on
threading.Thread.join(timeout), so a pure-mock approach would not catch the
real interaction.
"""
import time
from typing import Optional

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import (
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    QueryCapturingClientWrapper,
    QueryTimeoutError,
    resolve_query_timeout,
    wrap_clients_for_capture,
)


class _StubClient:
    """Tiny client whose execute_query optionally sleeps before returning."""

    def __init__(self, sleep_seconds: float = 0.0, raise_exc: Optional[Exception] = None):
        self.sleep_seconds = sleep_seconds
        self.raise_exc = raise_exc
        self.calls = 0
        # Quota metadata expected by the wrapper's _connection_id() helper —
        # leaving it empty disables quota enforcement so we can test timeout
        # behaviour in isolation.
        self._bow_connection_id = None

    def execute_query(self, query):
        self.calls += 1
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        if self.raise_exc:
            raise self.raise_exc
        return pd.DataFrame({"x": [1]})


def _make_wrapper(client, *, timeout: int = 1) -> QueryCapturingClientWrapper:
    return QueryCapturingClientWrapper(
        original_client=client,
        captured_queries=[],
        captured_timings=[],
        usage_context=None,
        client_key="main",
        query_timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# Wrapper enforcement
# ---------------------------------------------------------------------------


def test_slow_query_raises_query_timeout_error():
    client = _StubClient(sleep_seconds=2.0)
    wrapper = _make_wrapper(client, timeout=1)
    started = time.monotonic()
    with pytest.raises(QueryTimeoutError) as exc_info:
        wrapper.execute_query("select pg_sleep(2)")
    elapsed = time.monotonic() - started
    # Should bail near the deadline, not wait for the underlying sleep to end.
    assert 0.9 <= elapsed < 2.0
    assert exc_info.value.timeout_seconds == 1
    assert "1s" in str(exc_info.value)
    assert "smaller" in str(exc_info.value).lower()


def test_timeout_records_timeout_entry_in_captured_timings():
    client = _StubClient(sleep_seconds=2.0)
    wrapper = _make_wrapper(client, timeout=1)
    with pytest.raises(QueryTimeoutError):
        wrapper.execute_query("select pg_sleep(2)")
    timings = wrapper._captured_timings
    assert len(timings) == 1
    entry = timings[0]
    assert entry["error_type"] == "timeout"
    assert entry["timeout_seconds"] == 1
    # str(QueryTimeoutError) is what becomes db_message downstream.
    assert "exceeded" in entry["error"].lower()
    assert entry["sql"] == "select pg_sleep(2)"
    assert entry["rows"] is None


def test_timeout_message_carries_retry_guidance():
    err = QueryTimeoutError(45, sql="select * from t")
    msg = str(err)
    assert "45s" in msg
    # Hint words the planner can latch onto when deciding how to retry.
    for keyword in ("LIMIT", "smaller"):
        assert keyword in msg


def test_fast_query_passes_through_without_timeout():
    client = _StubClient(sleep_seconds=0.05)
    wrapper = _make_wrapper(client, timeout=2)
    df = wrapper.execute_query("select 1")
    assert isinstance(df, pd.DataFrame)
    assert client.calls == 1
    timings = wrapper._captured_timings
    assert len(timings) == 1
    # No error fields on the success path.
    assert "error" not in timings[0]
    assert "error_type" not in timings[0]


def test_multi_query_only_slow_one_marked_timeout():
    client = _StubClient(sleep_seconds=0.0)
    wrapper = _make_wrapper(client, timeout=1)
    # First call: fast path.
    wrapper.execute_query("select 1")
    # Second call: simulate slowness by mutating the stub's behaviour.
    client.sleep_seconds = 2.0
    with pytest.raises(QueryTimeoutError):
        wrapper.execute_query("select pg_sleep(2)")
    timings = wrapper._captured_timings
    assert len(timings) == 2
    assert "error_type" not in timings[0]
    assert timings[1]["error_type"] == "timeout"


def test_wrapper_remains_usable_after_a_timeout():
    """After a timeout the orphan thread keeps running, but a fresh call on
    the same wrapper must still succeed."""
    client = _StubClient(sleep_seconds=2.0)
    wrapper = _make_wrapper(client, timeout=1)
    with pytest.raises(QueryTimeoutError):
        wrapper.execute_query("select pg_sleep(2)")
    # Now make the underlying call fast again.
    client.sleep_seconds = 0.0
    df = wrapper.execute_query("select 1")
    assert isinstance(df, pd.DataFrame)
    timings = wrapper._captured_timings
    assert len(timings) == 2
    assert timings[0]["error_type"] == "timeout"
    assert "error" not in timings[1]


def test_query_alias_routes_through_instrumented_execute_query():
    """`.query(...)` is an alias the model often reaches for instead of
    `.execute_query(...)`. It must go through the wrapper's own execute_query so
    the call is still captured and timed — not delegated to the raw client."""
    client = _StubClient(sleep_seconds=0.0)
    wrapper = _make_wrapper(client, timeout=2)
    df = wrapper.query("select 1")
    assert isinstance(df, pd.DataFrame)
    assert client.calls == 1
    # Captured + timed exactly as if execute_query had been called directly.
    assert wrapper._captured_queries == ["select 1"]
    assert len(wrapper._captured_timings) == 1


def test_query_alias_honours_timeout():
    """The alias inherits the per-query timeout enforcement."""
    client = _StubClient(sleep_seconds=2.0)
    wrapper = _make_wrapper(client, timeout=1)
    with pytest.raises(QueryTimeoutError):
        wrapper.query("select pg_sleep(2)")


def test_non_timeout_exception_does_not_get_timeout_marker():
    client = _StubClient(raise_exc=RuntimeError("syntax error at or near 'FOO'"))
    wrapper = _make_wrapper(client, timeout=2)
    with pytest.raises(RuntimeError):
        wrapper.execute_query("FOO")
    timings = wrapper._captured_timings
    assert len(timings) == 1
    # Real DB errors fall through the existing path — no timeout marker.
    assert "error_type" not in timings[0]
    assert "syntax error" in timings[0]["error"]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class _StubOrgSettings:
    def __init__(self, value):
        self._value = value

    def get_config(self, key, default=None):
        if key == "query_timeout_seconds":
            # Mimic the FeatureConfig shape returned by the real model.
            class _FC:
                def __init__(self, v):
                    self.value = v
            return _FC(self._value)
        return default


class _ClientWithConn:
    def __init__(self, conn_timeout=None):
        if conn_timeout is not None:
            self._bow_connection_query_timeout = conn_timeout


def test_resolver_connection_value_wins_over_org():
    client = _ClientWithConn(conn_timeout=15)
    org = _StubOrgSettings(120)
    assert resolve_query_timeout(client, org) == 15


def test_resolver_falls_back_to_org_when_connection_unset():
    client = _ClientWithConn(conn_timeout=None)
    org = _StubOrgSettings(90)
    assert resolve_query_timeout(client, org) == 90


def test_resolver_falls_back_to_default_when_both_unset():
    client = _ClientWithConn(conn_timeout=None)
    assert resolve_query_timeout(client, organization_settings=None) == DEFAULT_QUERY_TIMEOUT_SECONDS


def test_resolver_ignores_non_positive_connection_value():
    client = _ClientWithConn(conn_timeout=0)
    org = _StubOrgSettings(45)
    assert resolve_query_timeout(client, org) == 45


def test_resolver_ignores_non_positive_org_value():
    client = _ClientWithConn(conn_timeout=None)
    org = _StubOrgSettings(0)
    assert resolve_query_timeout(client, org) == DEFAULT_QUERY_TIMEOUT_SECONDS


def test_resolver_handles_org_returning_raw_int():
    """get_config can return a raw int (when stored without the FeatureConfig
    envelope). The resolver should still pick it up."""

    class _RawOrg:
        def get_config(self, key, default=None):
            return 30 if key == "query_timeout_seconds" else default

    client = _ClientWithConn(conn_timeout=None)
    assert resolve_query_timeout(client, _RawOrg()) == 30


# ---------------------------------------------------------------------------
# wrap_clients_for_capture: per-connection resolution
# ---------------------------------------------------------------------------


def test_wrap_clients_resolves_per_connection_independently():
    """A single DS may expose multiple connections; each wrapper must pick up
    that connection's own timeout, not a single shared value."""
    slow_db = _StubClient()
    slow_db._bow_connection_query_timeout = 10
    fast_db = _StubClient()  # No connection override → falls back to org / default
    org = _StubOrgSettings(75)

    wrapped = wrap_clients_for_capture(
        ds_clients={"warehouse:slow": slow_db, "warehouse:fast": fast_db},
        captured_queries=[],
        captured_timings=[],
        usage_context=None,
        organization_settings=org,
    )
    assert isinstance(wrapped["warehouse:slow"], QueryCapturingClientWrapper)
    assert isinstance(wrapped["warehouse:fast"], QueryCapturingClientWrapper)
    assert wrapped["warehouse:slow"]._query_timeout_seconds == 10
    assert wrapped["warehouse:fast"]._query_timeout_seconds == 75


def test_wrap_clients_uses_default_when_no_settings_provided():
    client = _StubClient()
    wrapped = wrap_clients_for_capture(
        ds_clients={"main": client},
        captured_queries=[],
        captured_timings=[],
        usage_context=None,
        organization_settings=None,
    )
    assert wrapped["main"]._query_timeout_seconds == DEFAULT_QUERY_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Integration: timeout flows through generate_and_execute_stream_v2 and the
# retry loop activates so the agent can recover with a smaller query.
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402  (kept after the wrapper-only tests to keep them lean)

from app.ai.code_execution.code_execution import StreamingCodeExecutor  # noqa: E402
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest  # noqa: E402


def _drain(agen):
    async def _go():
        out = []
        async for ev in agen:
            out.append(ev)
        return out
    return asyncio.run(_go())


def test_timeout_triggers_retry_and_recovers_on_smaller_query():
    """First attempt sleeps past the deadline → QueryTimeoutError surfaces,
    retry loop kicks off, second attempt returns immediately."""
    slow_client = _StubClient(sleep_seconds=2.0)
    # Force the wrapper's per-connection budget to ~1s by stashing it on the
    # client. wrap_clients_for_capture reads this at wrap time.
    slow_client._bow_connection_query_timeout = 1

    attempt_codes = [
        # First attempt: slow query that will trip the timeout.
        "def generate_df(ds_clients, excel_files):\n"
        "    return ds_clients['main'].execute_query('select pg_sleep(2)')\n",
        # Second attempt: smaller query (the planner's expected reaction).
        "def generate_df(ds_clients, excel_files):\n"
        "    slow_client_sleep_off()  # noqa\n"
        "    return ds_clients['main'].execute_query('select 1 limit 1')\n",
    ]

    async def fake_code_generator_fn(**kwargs):
        return attempt_codes[kwargs.get("retries", 0)]

    # Mutate the stub between attempts so the retry actually returns fast.
    # The runtime hook `slow_client_sleep_off()` is injected via local namespace.
    def _disable_sleep():
        slow_client.sleep_seconds = 0.0

    # We can't inject a function into the exec'd user code from outside, so
    # instead we reset the sleep at the moment the second code blob is emitted.
    original_fn = fake_code_generator_fn

    async def code_gen_with_side_effect(**kwargs):
        if kwargs.get("retries", 0) == 1:
            _disable_sleep()
        # The injected helper name in the second attempt does not exist, so
        # we strip it before exec; simpler: just rewrite to plain code now.
        if kwargs.get("retries", 0) == 1:
            return (
                "def generate_df(ds_clients, excel_files):\n"
                "    return ds_clients['main'].execute_query('select 1 limit 1')\n"
            )
        return await original_fn(**kwargs)

    executor = StreamingCodeExecutor()
    ctx = CodeGenContext(user_prompt="show me one row", schemas_excerpt="")
    events = _drain(
        executor.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=ctx, retries=2),
            ds_clients={"main": slow_client},
            excel_files=[],
            code_generator_fn=code_gen_with_side_effect,
        )
    )

    stdouts = [e for e in events if e["type"] == "stdout"]
    progress = [e for e in events if e["type"] == "progress"]
    done = [e for e in events if e["type"] == "done"]
    assert done, f"expected a done event in: {[e['type'] for e in events]}"

    # First attempt's failure should have surfaced timeout text on stdout.
    timeout_messages = [s for s in stdouts if "timeout" in str(s["payload"]).lower()]
    assert timeout_messages, f"no timeout stdout among: {stdouts}"

    # Retry stage should have been emitted between attempts.
    retry_stages = [p for p in progress if p["payload"].get("stage") == "retry"]
    assert retry_stages, "expected a retry progress event after timeout"

    # The done event carries query_timings; the *successful* second attempt
    # cleared the per-attempt list so we expect a clean run there.
    payload = done[-1]["payload"]
    assert payload["df"] is not None and not payload["df"].empty
    # The errors list captures both the timeout and retry's outcome.
    assert payload["errors"], "expected the failed attempt to be recorded"


def test_timeout_failure_payload_carries_db_message_and_failed_sql():
    """If every attempt times out, the final done payload should expose the
    timeout via query_timings so the tool layer can pull db_message/failed_sql
    into the planner observation."""
    client = _StubClient(sleep_seconds=2.0)
    client._bow_connection_query_timeout = 1

    async def code_gen(**kwargs):
        return (
            "def generate_df(ds_clients, excel_files):\n"
            "    return ds_clients['main'].execute_query('select pg_sleep(2)')\n"
        )

    executor = StreamingCodeExecutor()
    ctx = CodeGenContext(user_prompt="big query", schemas_excerpt="")
    events = _drain(
        executor.generate_and_execute_stream_v2(
            request=CodeGenRequest(context=ctx, retries=2),
            ds_clients={"main": client},
            excel_files=[],
            code_generator_fn=code_gen,
        )
    )
    done = [e for e in events if e["type"] == "done"]
    assert done
    payload = done[-1]["payload"]

    timings = payload["query_timings"] or []
    timeout_timings = [t for t in timings if t.get("error_type") == "timeout"]
    assert timeout_timings, f"expected timeout entry in timings: {timings}"
    last = timeout_timings[-1]
    # Mirrors how create_data / inspect_data extract db_message + failed_sql
    # from captured_timings into the observation seen by the planner.
    assert last["error"]
    assert "exceeded" in last["error"].lower()
    assert last["sql"] == "select pg_sleep(2)"
    assert last["timeout_seconds"] == 1

    # Reproduce the extraction snippet shared by create_data.py and
    # inspect_data.py so a refactor of the timing schema fails this test
    # before it silently breaks the planner-facing observation.
    failed_timings = [t for t in timings if t.get("error")]
    assert failed_timings
    last_failed = failed_timings[-1]
    db_message = last_failed.get("error")
    failed_sql = last_failed.get("sql")
    assert db_message and "exceeded" in db_message.lower()
    assert failed_sql == "select pg_sleep(2)"
