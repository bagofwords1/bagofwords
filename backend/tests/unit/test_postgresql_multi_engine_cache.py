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
from app.data_sources.clients.postgresql_multi_engine import PgConn, get_engine, invalidate_engines

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
