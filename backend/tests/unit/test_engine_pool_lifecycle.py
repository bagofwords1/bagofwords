"""Pooled engines must not hold server sessions open forever.

Two lifecycle guarantees layered on top of the engine cache:

  * ``ephemeral()`` — health probes get throwaway NullPool engines: nothing
    cached, nothing pooled, everything disposed at context exit. Without
    this, the 5-minute connection-status sweep was the only traffic on quiet
    sources and kept re-warming their pools indefinitely — DBAs saw sessions
    idle for an hour whose last statement was ``SELECT 1``.
  * ``prune_idle_engines()`` — ``pool_recycle`` fires only at checkout, so a
    pool nobody checks out from never closes its connections. The reaper
    disposes pools of engines unused past the TTL; the engine stays cached
    and usable (next checkout reconnects).
"""

import asyncio

import pytest
import sqlalchemy

from app.data_sources import engine_pool


@pytest.fixture(autouse=True)
def _clean_pool():
    engine_pool.dispose_all()
    yield
    engine_pool.dispose_all()


def _uri(tmp_path, name="db.sqlite"):
    return f"sqlite:///{tmp_path / name}"


# --------------------------------------------------------------------------
# ephemeral()
# --------------------------------------------------------------------------

def test_ephemeral_engine_is_nullpool_and_not_cached(tmp_path):
    with engine_pool.ephemeral():
        eng = engine_pool.get_engine(_uri(tmp_path))
        assert isinstance(eng.pool, sqlalchemy.pool.NullPool)
    assert engine_pool.stats()["cached_engines"] == 0


def test_ephemeral_leaves_no_open_connections(tmp_path):
    """Every physical connection a probe opens must be closed by the time the
    probe's checkout ends — nothing may linger server-side."""
    opened, closed = [], []
    with engine_pool.ephemeral():
        eng = engine_pool.get_engine(_uri(tmp_path))
        sqlalchemy.event.listen(eng, "connect", lambda dbapi, rec: opened.append(rec))
        sqlalchemy.event.listen(eng, "close", lambda dbapi, rec: closed.append(rec))
        with eng.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        # NullPool: the physical connection closed with the checkout, not at
        # some later recycle/dispose.
        assert len(opened) == 1
        assert len(closed) == 1


def test_ephemeral_does_not_leak_into_other_contexts(tmp_path):
    """Only the probe's own thread/task sees ephemeral mode."""
    uri = _uri(tmp_path)

    async def _main():
        async def probe():
            with engine_pool.ephemeral():
                await asyncio.sleep(0.01)
                return engine_pool.get_engine(uri)

        async def regular():
            await asyncio.sleep(0)
            return engine_pool.get_engine(uri)

        return await asyncio.gather(probe(), regular())

    probe_eng, regular_eng = asyncio.run(_main())
    assert isinstance(probe_eng.pool, sqlalchemy.pool.NullPool)
    assert isinstance(regular_eng.pool, sqlalchemy.pool.QueuePool)
    assert engine_pool.stats()["cached_engines"] == 1


def test_ephemeral_strips_queuepool_only_kwargs(tmp_path):
    """Callers pass pool sizing through get_engine; NullPool must not choke
    on it. Pool-agnostic kwargs (creator, pool_recycle) still apply."""
    with engine_pool.ephemeral():
        eng = engine_pool.get_engine(
            _uri(tmp_path), pool_size=7, max_overflow=3, pool_recycle=1500
        )
        with eng.connect() as conn:
            assert conn.execute(sqlalchemy.text("SELECT 1")).scalar() == 1


def test_atest_connection_probes_ephemerally(tmp_path):
    """The base-client async wrapper is what the status sweep and the test
    endpoints call — it must not populate or touch the engine cache."""
    from app.data_sources.clients.sqlite_client import SqliteClient

    db = tmp_path / "probe.sqlite"
    sqlalchemy.create_engine(f"sqlite:///{db}").connect().close()

    client = SqliteClient(database=str(db))
    result = asyncio.run(client.atest_connection())
    assert result["success"] is True
    assert engine_pool.stats()["cached_engines"] == 0


# --------------------------------------------------------------------------
# prune_idle_engines()
# --------------------------------------------------------------------------

def test_prune_closes_idle_pooled_connections(tmp_path):
    eng = engine_pool.get_engine(_uri(tmp_path))
    with eng.connect() as conn:
        conn.execute(sqlalchemy.text("SELECT 1"))
    assert eng.pool.checkedin() == 1

    assert engine_pool.prune_idle_engines(max_idle_s=0) == 1
    assert eng.pool.checkedin() == 0
    # Engine stays cached and usable: next checkout just reconnects.
    assert engine_pool.stats()["cached_engines"] == 1
    with eng.connect() as conn:
        assert conn.execute(sqlalchemy.text("SELECT 1")).scalar() == 1


def test_prune_respects_ttl(tmp_path):
    eng = engine_pool.get_engine(_uri(tmp_path))
    with eng.connect() as conn:
        conn.execute(sqlalchemy.text("SELECT 1"))
    # Recently used — a generous TTL must leave it alone.
    assert engine_pool.prune_idle_engines(max_idle_s=3600) == 0
    assert eng.pool.checkedin() == 1


def test_prune_skips_engines_with_checked_out_connections(tmp_path):
    """A long-running query's engine must never have its pool yanked."""
    eng = engine_pool.get_engine(_uri(tmp_path))
    conn = eng.connect()
    try:
        assert engine_pool.prune_idle_engines(max_idle_s=0) == 0
    finally:
        conn.close()


def test_prune_skips_empty_pools(tmp_path):
    engine_pool.get_engine(_uri(tmp_path))  # never connected
    assert engine_pool.prune_idle_engines(max_idle_s=0) == 0


def test_ttl_zero_disables_reaping(tmp_path, monkeypatch):
    monkeypatch.setenv("BOW_ENGINE_IDLE_TTL", "0")
    eng = engine_pool.get_engine(_uri(tmp_path))
    with eng.connect() as conn:
        conn.execute(sqlalchemy.text("SELECT 1"))
    # No explicit override → env TTL of 0 means "never reap".
    assert engine_pool.prune_idle_engines() == 0
    assert eng.pool.checkedin() == 1


def test_pool_uses_lifo_checkout(tmp_path):
    """LIFO keeps light steady traffic on one warm connection so the rest of
    the pool actually goes idle and can be reaped."""
    eng = engine_pool.get_engine(_uri(tmp_path))
    assert eng.pool._pool.use_lifo is True
