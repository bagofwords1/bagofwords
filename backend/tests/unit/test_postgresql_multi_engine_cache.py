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
    MAX_ENGINE_LIFETIME_SECONDS,
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
        self.built_at = engine_module.monotonic()

    def ensure_views(self, views, types=None):
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
        threading.Thread(target=lambda: results.append(get_engine(CONN, VIEWS, connection_id="c1").attached))
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
    assert results == [False] * 4
    release.set()
    get_engine(CONN, VIEWS, connection_id="c1")
    assert len(builds) == 2


def test_a_busy_healthy_engine_is_still_rebuilt_after_its_maximum_lifetime(monkeypatch, clock):
    built = _counting_build(monkeypatch)
    first = get_engine(CONN, VIEWS, ttl_seconds=600, connection_id="c1")

    # Queried every minute: each use slides the expiry, but never past the cap.
    while clock[0] < 1000.0 + MAX_ENGINE_LIFETIME_SECONDS - 60:
        clock[0] += 60
        assert get_engine(CONN, VIEWS, ttl_seconds=600, connection_id="c1") is first
    clock[0] = 1000.0 + MAX_ENGINE_LIFETIME_SECONDS

    assert get_engine(CONN, VIEWS, ttl_seconds=600, connection_id="c1") is not first
    assert len(built) == 2


def test_expire_soon_shortens_a_cached_engine_to_the_degraded_ttl(monkeypatch, clock):
    built = _counting_build(monkeypatch)
    first = get_engine(CONN, VIEWS, ttl_seconds=600, connection_id="c1")
    first.cache_key = next(iter(engine_module._cache))

    first.degraded = True  # what leaving a tenant out of a view does
    engine_module.expire_soon(first)
    clock[0] += DEGRADED_ENGINE_TTL_SECONDS - 1
    assert get_engine(CONN, VIEWS, ttl_seconds=600, connection_id="c1") is first
    clock[0] += 2

    assert get_engine(CONN, VIEWS, ttl_seconds=600, connection_id="c1") is not first
    assert len(built) == 2


def test_invalidating_during_a_build_does_not_start_a_second_build_beside_it(monkeypatch):
    # A re-index while a query thread is building used to wipe the build
    # locks, so the next caller built a second engine in parallel and every
    # tenant was attached twice. Now the next caller waits, and builds afresh
    # (the interrupted build predates the re-index, so it is not reused).
    builds: list = []
    running, peak = [0], [0]
    lock = threading.Lock()
    release = threading.Event()

    def slow_build(conn, views, **kwargs):
        with lock:
            builds.append(1)
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(5)
        with lock:
            running[0] -= 1
        return _StubEngine()

    monkeypatch.setattr(engine_module, "build_engine", slow_build)
    got: dict = {}
    first = threading.Thread(target=lambda: got.setdefault("first", get_engine(CONN, VIEWS, connection_id="c1")))
    first.start()
    assert _wait_for(lambda: len(builds) == 1, timeout=5)

    invalidate_engines("c1")
    second = threading.Thread(target=lambda: got.setdefault("second", get_engine(CONN, VIEWS, connection_id="c1")))
    second.start()
    assert not _wait_for(lambda: len(builds) > 1, timeout=0.5)  # waits, does not build beside it
    release.set()
    first.join()
    second.join()

    assert peak[0] == 1
    assert len(builds) == 2
    assert got["second"] is not got["first"]
    assert get_engine(CONN, VIEWS, connection_id="c1") is got["second"]


def test_a_new_view_on_a_cached_engine_does_not_hold_up_other_queries(monkeypatch):
    built = []
    entered = threading.Event()
    release = threading.Event()

    class _SlowViews(_StubEngine):
        def ensure_views(self, views, types=None):
            if "public.slow" in views:
                entered.set()
                release.wait(5)

    def build(conn, views, **kwargs):
        built.append(_SlowViews())
        return built[-1]

    monkeypatch.setattr(engine_module, "build_engine", build)
    engine = get_engine(CONN, VIEWS, connection_id="c1")
    slow = threading.Thread(
        target=lambda: get_engine(CONN, {"public.slow": ["tenant_a"]}, tenants=["tenant_a", "tenant_b"],
                                  connection_id="c1"))
    slow.start()
    assert entered.wait(5)

    done = threading.Event()
    other = threading.Thread(target=lambda: (get_engine(CONN, VIEWS, connection_id="c1"), done.set()))
    other.start()
    finished_while_slow_view_builds = done.wait(2)
    release.set()
    slow.join()
    other.join()

    assert finished_while_slow_view_builds
    assert len(built) == 1 and engine is built[0]


def test_cached_engines_are_capped_least_recently_used_first(monkeypatch, clock):
    monkeypatch.setenv(engine_module.MAX_CACHED_ENGINES_ENV, "2")
    built = _counting_build(monkeypatch)
    a = get_engine(CONN, VIEWS, connection_id="a")
    clock[0] += 1
    b = get_engine(CONN, VIEWS, connection_id="b")
    clock[0] += 1
    assert get_engine(CONN, VIEWS, connection_id="a") is a  # a is now the most recently used
    clock[0] += 1
    get_engine(CONN, VIEWS, connection_id="c")

    assert len(engine_module._cache) == 2
    assert get_engine(CONN, VIEWS, connection_id="a") is a
    assert get_engine(CONN, VIEWS, connection_id="b") is not b
    assert len(built) == 4


def test_bookkeeping_of_unused_keys_is_pruned_when_engines_expire(monkeypatch, clock):
    _counting_build(monkeypatch)
    for password in ("old", "new"):  # a credential rotation changes the key
        get_engine(PgConn(host="db.invalid", port=5432, user="reader", password=password, sslmode="disable"),
                   VIEWS, ttl_seconds=60, connection_id="c1")
    assert len(engine_module._build_locks) == 2
    clock[0] += 61
    get_engine(CONN, VIEWS, ttl_seconds=60, connection_id="other")

    assert len(engine_module._build_locks) == 1
    assert set(engine_module._finished_builds) <= set(engine_module._build_locks)
