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


def test_warm_loads_the_list_before_the_first_request(monkeypatch):
    async def scenario():
        _reader(monkeypatch, ["https://portal.example.com"])
        await svc.warm()
        calls = _reader(monkeypatch, ["https://other.example.com"])
        assert svc.cached_origins() == ("https://portal.example.com",)
        assert calls == []  # fresh: the first request does not read again

    asyncio.run(scenario())


def test_warm_never_fails_startup(monkeypatch):
    async def scenario():
        monkeypatch.setattr(svc, "READ_TIMEOUT_SECONDS", 0.01)
        _reader(monkeypatch, hang=asyncio.Event())
        await svc.warm()
        _reader(monkeypatch, fail=True)
        await svc.warm()
        assert svc.cached_origins() == ()

    asyncio.run(scenario())


def test_a_save_during_a_background_read_wins(monkeypatch):
    async def scenario():
        svc._cache.update(origins=("https://old.example.com",), loaded_at=None)
        release = asyncio.Event()
        _reader(monkeypatch, ["https://old.example.com"], hang=release)
        svc.cached_origins()  # starts a read of the pre-save row
        await asyncio.sleep(0)
        svc._set_saved(("https://new.example.com",))  # what save_origins does after commit
        release.set()
        await svc._cache["task"]
        assert svc.cached_origins() == ("https://new.example.com",)

    asyncio.run(scenario())


def test_a_reset_during_a_read_leaves_the_cache_to_be_read_again(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        _reader(monkeypatch, ["https://a.example.com"], hang=release)
        svc.cached_origins()
        await asyncio.sleep(0)
        task = svc._cache["task"]
        svc.reset_cache()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert svc._cache["loaded_at"] is None and svc._cache["origins"] == ()

    asyncio.run(scenario())


class _Row:
    def __init__(self, value):
        self.value = value


@pytest.mark.parametrize("value, expected", [
    (["https://a.example.com"], []),
    ({"origins": "https://a.example.com"}, []),
    ({"origins": None}, []),
    ({}, []),
    ({"origins": ["https://a.example.com", "HTTPS://A.example.com/", "bad; script-src *"]}, ["https://a.example.com"]),
])
def test_a_malformed_stored_row_reads_as_clean_or_empty(value, expected):
    assert svc._stored_origins(_Row(value)) == expected


def test_the_stored_list_is_capped_on_read():
    origins = [f"https://s{i}.example.com" for i in range(svc.MAX_ORIGINS + 5)]
    assert len(svc._stored_origins(_Row({"origins": origins}))) == svc.MAX_ORIGINS
