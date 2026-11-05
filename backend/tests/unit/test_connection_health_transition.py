"""A shared connection announces a health change once per edge, never per probe.

``ConnectionService._record_probe_outcome`` is the single point that flips
``Connection.is_active`` from the background status sweep. The down/restored
alert is fired off its *return value* — the True↔False edge — so the promise
this test pins is: a connection that stays down across many probes is announced
exactly once, and only after the debounce threshold, while a recovery is
announced on the first success. That is what keeps a flapping or long-down
source from emailing the admins every five minutes.
"""
import pytest

from app.services.connection_service import ConnectionService


class _Conn:
    """Just the two fields the probe-outcome logic reads/writes."""
    def __init__(self, is_active=True, failures=0):
        self.is_active = is_active
        self.consecutive_test_failures = failures


_record = ConnectionService._record_probe_outcome


def test_down_edge_only_after_threshold_failures(monkeypatch):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "3")
    c = _Conn()

    # Below the threshold: no edge, connection stays usable.
    assert _record(c, False) is None
    assert _record(c, False) is None
    assert c.is_active is True

    # The failure that crosses the threshold is the one down-edge.
    assert _record(c, False) == "down"
    assert c.is_active is False


def test_down_is_announced_once_while_it_stays_down(monkeypatch):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "2")
    c = _Conn()
    edges = [_record(c, False) for _ in range(6)]
    # Exactly one "down" across the whole outage, no matter how many probes.
    assert edges.count("down") == 1
    assert edges[0] is None and edges[1] == "down"
    assert all(e is None for e in edges[2:])


def test_recovery_edge_on_first_success_then_quiet(monkeypatch):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "3")
    c = _Conn(is_active=False, failures=5)

    assert _record(c, True) == "up"
    assert c.is_active is True
    assert c.consecutive_test_failures == 0
    # Already up: a further success is not a new edge.
    assert _record(c, True) is None


def test_single_success_resets_the_failure_streak(monkeypatch):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "3")
    c = _Conn()
    _record(c, False)
    _record(c, False)
    assert _record(c, True) is None          # recovered before deactivating
    assert c.consecutive_test_failures == 0
    # The streak restarts from zero, so it again takes the full threshold.
    assert _record(c, False) is None
    assert _record(c, False) is None
    assert _record(c, False) == "down"


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("BOW_CONN_DEACTIVATE_AFTER_FAILURES", "1")
    c = _Conn()
    # One failure is enough when the threshold is 1.
    assert _record(c, False) == "down"
    assert c.is_active is False
