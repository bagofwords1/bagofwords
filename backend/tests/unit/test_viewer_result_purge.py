"""A purge of viewer results, once started, runs to its end.

A purge runs last in a request that changed what viewers' results read (a
row policy, an extract, a connection's auth policy, a member's groups), in a
session of its own (app/services/viewer_result_freshness.purge_quietly). A
request cancelled meanwhile (a disconnect, a timeout, a shutdown) lets it
finish, then stops: it never leaves the purge's work half done.
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.asyncio


async def test_a_cancelled_request_lets_its_purge_finish_then_stops(tmp_path):
    from app.services.viewer_result_freshness import purge_quietly

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'purge.db'}")
    entered, release = asyncio.Event(), asyncio.Event()
    finished: list = []

    async def purge(own, *, rows):
        entered.set()
        await release.wait()
        finished.append(rows)
        return rows

    try:
        task = asyncio.create_task(purge_quietly(SimpleNamespace(bind=engine), purge, rows=3))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [3]
    finally:
        await engine.dispose()
