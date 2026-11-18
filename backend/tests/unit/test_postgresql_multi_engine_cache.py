"""The process-wide cache of `postgresql_multi` engines, with the DuckDB build stubbed.

What is promised here is about the cache itself (who builds, who shares, what
is kept), so the engine build is replaced by a stand-in that records each call;
the real build is covered against a Postgres container in
`tests/integrations/test_postgresql_multi_engine.py`.
"""

import threading
import time

import pytest

from app.data_sources.clients import postgresql_multi_engine as engine_module
from app.data_sources.clients.postgresql_multi_engine import (
    DEGRADED_ENGINE_TTL_SECONDS,
    PgConn,
    evict_engine,
    get_engine,
    invalidate_engines,
)

CONN = PgConn(host="db.invalid", port=5432, user="reader", password="pw", sslmode="disable")
VIEWS = {"public.orders": ["tenant_a", "tenant_b"]}


class _StubEngine:
    def __init__(self, attached: bool = True):
        self.attached = attached
        self.degraded = not attached
        self.closed = False

    def ensure_views(self, views):
        pass

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_cache():
    invalidate_engines()
    yield
    invalidate_engines()


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(engine_module, "monotonic", lambda: now[0])
    return now


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_concurrent_callers_after_expiry_share_one_rebuild(monkeypatch, clock):
    builds: list = []
    release = threading.Event()

    def slow_build(conn, views, **kwargs):
        builds.append(threading.current_thread().name)
        if len(builds) > 1:
            release.wait(5)
        return _StubEngine()

    monkeypatch.setattr(engine_module, "build_engine", slow_build)
    get_engine(CONN, VIEWS, ttl_seconds=10, connection_id="c1")
    clock[0] += 11

    results: dict = {}

    def caller(name):
        results[name] = get_engine(CONN, VIEWS, ttl_seconds=10, connection_id="c1")

    first = threading.Thread(target=caller, args=("first",), name="first")
    first.start()
    assert _wait_for(lambda: len(builds) == 2, timeout=5)
    others = [threading.Thread(target=caller, args=(n,), name=n) for n in ("second", "third")]
    for t in others:
        t.start()
    _wait_for(lambda: len(builds) > 2, timeout=0.5)
    release.set()
    for t in [first, *others]:
        t.join()

    assert len(builds) == 2, builds
    assert len({id(e) for e in results.values()}) == 1


def _counting_build(monkeypatch, attached=True) -> list:
    built: list = []

    def build(conn, views, **kwargs):
        engine = _StubEngine(attached=attached)
        built.append(engine)
        return engine

    monkeypatch.setattr(engine_module, "build_engine", build)
    return built


def test_engine_with_no_attached_tenant_is_not_cached(monkeypatch, clock):
    built = _counting_build(monkeypatch, attached=False)

    first = get_engine(CONN, VIEWS, connection_id="c1")
    second = get_engine(CONN, VIEWS, connection_id="c1")

    assert second is not first
    assert len(built) == 2


def test_forced_rebuild_is_allowed_once_per_degraded_ttl(monkeypatch, clock):
    built = _counting_build(monkeypatch)
    engine = get_engine(CONN, VIEWS, connection_id="c1")

    assert evict_engine(engine) is True
    rebuilt = get_engine(CONN, VIEWS, connection_id="c1")
    assert rebuilt is not engine

    clock[0] += DEGRADED_ENGINE_TTL_SECONDS - 1
    assert evict_engine(rebuilt) is False
    assert get_engine(CONN, VIEWS, connection_id="c1") is rebuilt

    clock[0] += 2
    assert evict_engine(rebuilt) is True
    assert get_engine(CONN, VIEWS, connection_id="c1") is not rebuilt
    assert len(built) == 3


def test_forced_rebuilds_are_counted_per_connection(monkeypatch, clock):
    _counting_build(monkeypatch)
    first = get_engine(CONN, VIEWS, connection_id="c1")
    other = get_engine(CONN, VIEWS, connection_id="c2")

    assert evict_engine(first) is True
    assert evict_engine(other) is True


def test_callers_failing_on_the_same_engine_all_move_to_one_rebuild(monkeypatch, clock):
    built = _counting_build(monkeypatch)
    failed = get_engine(CONN, VIEWS, connection_id="c1")

    retries = [evict_engine(failed) for _ in range(3)]
    rebuilt = {id(get_engine(CONN, VIEWS, connection_id="c1")) for _ in range(3)}

    assert retries == [True, True, True]
    assert len(rebuilt) == 1
    assert len(built) == 2


def test_a_retry_onto_a_newer_engine_does_not_use_up_the_rebuild_budget(monkeypatch, clock):
    _counting_build(monkeypatch)
    failed = get_engine(CONN, VIEWS, connection_id="c1")
    assert evict_engine(failed) is True
    current = get_engine(CONN, VIEWS, connection_id="c1")

    assert evict_engine(failed) is True
    assert get_engine(CONN, VIEWS, connection_id="c1") is current


def test_callers_waiting_on_a_build_that_attached_nothing_reuse_its_result(monkeypatch):
    builds: list = []
    release = threading.Event()

    def empty_build(conn, views, **kwargs):
        builds.append(1)
        release.wait(5)
        return _StubEngine(attached=False)

    monkeypatch.setattr(engine_module, "build_engine", empty_build)
    results: list = []
    callers = [
        threading.Thread(target=lambda: results.append(get_engine(CONN, VIEWS, connection_id="c1")))
        for _ in range(4)
    ]
    callers[0].start()
    assert _wait_for(lambda: len(builds) == 1, timeout=5)
    for t in callers[1:]:
        t.start()
    _wait_for(lambda: len(builds) > 1, timeout=0.5)
    release.set()
    for t in callers:
        t.join()

    assert len(builds) == 1
    assert len({id(e) for e in results}) == 1
    assert get_engine(CONN, VIEWS, connection_id="c1") is not results[0]
    assert len(builds) == 2
