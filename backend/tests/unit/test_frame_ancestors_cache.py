"""The saved framing-sites list as the security-headers middleware sees it.

Contract: a request never waits on the database. The list comes from memory;
a stale list starts one background read, bounded by a timeout. A slow or
failing read keeps the previous list; a successful one replaces it for the
requests that follow.
"""
import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.core.security_headers import SecurityHeadersMiddleware
from app.services import frame_ancestors_service as svc


@pytest.fixture(autouse=True)
def _fresh_cache():
    svc.reset_cache()
    yield
    svc.reset_cache()


def _reader(monkeypatch, result=None, hang: asyncio.Event | None = None, fail=False):
    calls = []

    async def _read():
        calls.append(1)
        if hang is not None:
            await hang.wait()
        if fail:
            raise RuntimeError("database down")
        return tuple(result or ())

    monkeypatch.setattr(svc, "_read", _read)
    return calls


def test_a_stale_list_is_served_at_once_and_refreshed_in_the_background(monkeypatch):
    async def scenario():
        svc._cache.update(origins=("https://old.example.com",), loaded_at=time.monotonic() - svc.REFRESH_SECONDS - 1)
        release = asyncio.Event()
        _reader(monkeypatch, ["https://new.example.com"], hang=release)

        assert svc.cached_origins() == ("https://old.example.com",)
        release.set()
        await svc._cache["task"]
        assert svc.cached_origins() == ("https://new.example.com",)

    asyncio.run(scenario())


def test_a_hanging_read_times_out_and_keeps_the_previous_list(monkeypatch):
    async def scenario():
        monkeypatch.setattr(svc, "READ_TIMEOUT_SECONDS", 0.01)
        svc._cache.update(origins=("https://kept.example.com",), loaded_at=None)
        _reader(monkeypatch, hang=asyncio.Event())

        assert svc.cached_origins() == ("https://kept.example.com",)
        await svc._cache["task"]
        assert svc.cached_origins() == ("https://kept.example.com",)
        assert svc._cache["loaded_at"] is not None  # not retried on every request

    asyncio.run(scenario())


def test_a_failing_read_keeps_the_previous_list(monkeypatch):
    async def scenario():
        svc._cache.update(origins=("https://kept.example.com",), loaded_at=None)
        _reader(monkeypatch, fail=True)
        svc.cached_origins()
        await svc._cache["task"]
        assert svc.cached_origins() == ("https://kept.example.com",)

    asyncio.run(scenario())


def test_only_one_background_read_runs_at_a_time(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        calls = _reader(monkeypatch, ["https://a.example.com"], hang=release)
        for _ in range(20):
            svc.cached_origins()
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await svc._cache["task"]

    asyncio.run(scenario())


def test_a_fresh_list_does_not_read_again(monkeypatch):
    svc._cache.update(origins=("https://a.example.com",), loaded_at=time.monotonic())
    calls = _reader(monkeypatch, ["https://b.example.com"])
    assert svc.cached_origins() == ("https://a.example.com",)
    assert calls == [] and svc._cache["task"] is None


def test_a_request_does_not_wait_for_a_stuck_database(monkeypatch):
    monkeypatch.setattr(svc, "READ_TIMEOUT_SECONDS", 30)  # as slow as a full pool
    _reader(monkeypatch, hang=asyncio.Event())

    app = FastAPI()
    app.get("/{path:path}")(lambda path: PlainTextResponse("x"))
    app.add_middleware(SecurityHeadersMiddleware)
    with TestClient(app) as client:
        started = time.monotonic()
        for path in ("/users/sign-in", "/api/settings", "/health"):
            assert client.get(path).status_code == 200
        assert time.monotonic() - started < 2
