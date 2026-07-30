"""A burst of agent queries must not arrive at one source all at once.

The timeout bounds how long a query runs and the rate limiter bounds how often
a connection is queried; neither bounds how many run simultaneously, which is
the number a struggling on-prem box actually feels. These tests pin the cap's
behaviour: bursts queue rather than fail, connections don't share a budget, a
slot is always released, and paths outside the agent (indexing, health checks)
are never gated.
"""

import threading
import time

import pytest

from app.data_sources import query_concurrency as qcc
from app.data_sources.query_concurrency import ConnectionBusyError


@pytest.fixture(autouse=True)
def _clean():
    qcc.reset()
    yield
    qcc.reset()


class FakeClient:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


class FakeSettings:
    def __init__(self, value):
        self._value = value

    def get_config(self, name):
        assert name == "max_concurrent_queries_per_connection"
        return type("C", (), {"value": self._value})()


# --------------------------------------------------------------------------
# Limit resolution
# --------------------------------------------------------------------------

def test_connection_override_wins_over_the_org_default():
    client = FakeClient(_bow_connection_max_concurrent_queries=2)
    assert qcc.effective_limit(client, FakeSettings(8)) == 2


def test_org_default_applies_without_a_connection_override():
    assert qcc.effective_limit(FakeClient(), FakeSettings(8)) == 8


def test_falls_back_to_the_builtin_without_any_setting():
    assert qcc.effective_limit(FakeClient(), None) == qcc.DEFAULT_MAX_CONCURRENT_QUERIES


def test_a_zero_does_not_mean_unlimited():
    """An accidental 0 must not silently remove the protection."""
    client = FakeClient(_bow_connection_max_concurrent_queries=0)
    assert qcc.effective_limit(client, FakeSettings(0)) == qcc.DEFAULT_MAX_CONCURRENT_QUERIES


def test_broken_org_settings_do_not_break_queries():
    class Exploding:
        def get_config(self, name):
            raise RuntimeError("settings unavailable")

    assert qcc.effective_limit(FakeClient(), Exploding()) == qcc.DEFAULT_MAX_CONCURRENT_QUERIES


# --------------------------------------------------------------------------
# The cap itself
# --------------------------------------------------------------------------

def test_slot_is_held_for_the_duration_and_then_released():
    with qcc.slot("conn-1", 2, wait_seconds=5):
        assert qcc.in_flight("conn-1") == 1
    assert qcc.in_flight("conn-1") == 0


def test_slot_is_released_even_when_the_query_raises():
    with pytest.raises(ValueError):
        with qcc.slot("conn-1", 1, wait_seconds=5):
            raise ValueError("query failed")
    assert qcc.in_flight("conn-1") == 0
    # And the freed slot is immediately reusable.
    with qcc.slot("conn-1", 1, wait_seconds=1):
        pass


def test_a_burst_queues_instead_of_failing():
    """Serialising a burst is the point; failing would just make the agent
    retry and add more load."""
    limit = 2
    peak = {"value": 0}
    peak_lock = threading.Lock()
    start = threading.Barrier(6)
    errors = []

    def worker():
        try:
            start.wait(10)
            with qcc.slot("conn-burst", limit, wait_seconds=20):
                with peak_lock:
                    now = qcc.in_flight("conn-burst")
                    peak["value"] = max(peak["value"], now)
                time.sleep(0.3)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == []
    assert peak["value"] <= limit, f"observed {peak['value']} concurrent, cap {limit}"
    assert qcc.in_flight("conn-burst") == 0


def test_waiting_past_the_budget_raises_a_legible_error():
    held = threading.Event()
    release = threading.Event()

    def hog():
        with qcc.slot("conn-busy", 1, wait_seconds=5):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(ConnectionBusyError) as exc:
            with qcc.slot("conn-busy", 1, wait_seconds=1, connection_name="Oracle Prod"):
                pass
        message = str(exc.value)
        # The agent has to be able to act on this: name the source, the cap,
        # and what to do differently.
        assert "Oracle Prod" in message
        assert "limit 1 concurrent" in message
        assert "fewer queries at once" in message
    finally:
        release.set()
        t.join(10)


def test_connections_do_not_share_a_budget():
    with qcc.slot("conn-a", 1, wait_seconds=5):
        # conn-b is a different source; saturating conn-a must not block it.
        with qcc.slot("conn-b", 1, wait_seconds=1):
            assert qcc.in_flight("conn-a") == 1
            assert qcc.in_flight("conn-b") == 1


def test_paths_without_a_connection_id_are_not_gated():
    """Indexing and connection tests are not the burst source, and gating them
    would let a slow agent query block a health check."""
    with qcc.slot(None, 1, wait_seconds=1) as acquired:
        assert acquired is False
    with qcc.slot(None, 1, wait_seconds=1) as acquired:
        assert acquired is False


def test_changing_the_cap_rebuilds_the_semaphore():
    with qcc.slot("conn-c", 1, wait_seconds=5):
        pass
    # Admin raises the cap; the new value must take effect.
    a = threading.Event()
    done = threading.Event()

    def hold():
        with qcc.slot("conn-c", 3, wait_seconds=5):
            a.set()
            done.wait(10)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    a.wait(5)
    try:
        with qcc.slot("conn-c", 3, wait_seconds=2):
            assert qcc.in_flight("conn-c") == 2
    finally:
        done.set()
        t.join(10)


# --------------------------------------------------------------------------
# Wired into the query wrapper
# --------------------------------------------------------------------------

def test_the_wrapper_holds_a_slot_while_a_query_runs():
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    observed = []

    class Client:
        _bow_connection_id = "conn-wrapped"
        _bow_connection_name = "Shop"

        def execute_query(self, sql):
            observed.append(qcc.in_flight("conn-wrapped"))
            return [1]

    w = QueryCapturingClientWrapper(Client(), [], [], max_concurrent_queries=3)
    w.execute_query("SELECT 1")
    assert observed == [1]
    assert qcc.in_flight("conn-wrapped") == 0


def test_the_wrapper_releases_the_slot_when_a_query_fails():
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    class Client:
        _bow_connection_id = "conn-fail"

        def execute_query(self, sql):
            raise RuntimeError("syntax error")

    w = QueryCapturingClientWrapper(Client(), [], [], max_concurrent_queries=2)
    with pytest.raises(RuntimeError):
        w.execute_query("SELCT 1")
    assert qcc.in_flight("conn-fail") == 0


def test_a_timed_out_query_releases_its_slot():
    """The timeout abandons a thread; the slot must not be abandoned with it,
    or a connection silently loses capacity for the process lifetime."""
    from app.ai.code_execution.code_execution import (
        QueryCapturingClientWrapper, QueryTimeoutError,
    )

    class Slow:
        _bow_connection_id = "conn-timeout"

        def execute_query(self, sql):
            threading.Event().wait(30)

    w = QueryCapturingClientWrapper(
        Slow(), [], [], query_timeout_seconds=1, max_concurrent_queries=1,
    )
    with pytest.raises(QueryTimeoutError):
        w.execute_query("SELECT pg_sleep(30)")
    assert qcc.in_flight("conn-timeout") == 0
    # Capacity is back: the next query is admitted immediately.
    with qcc.slot("conn-timeout", 1, wait_seconds=1):
        pass
