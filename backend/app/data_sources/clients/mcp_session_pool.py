"""Reusable MCP client sessions, one per (server, credentials) per worker.

Opening a session costs an ``initialize`` + ``notifications/initialized`` round
trip, and the MCP SDK then calls ``tools/list`` after the first ``call_tool`` of
every session to validate output schemas (``ClientSession._validate_tool_result``
lists tools when its cache is empty). Opening a fresh session per tool call paid
all three on every call (~225 ms against a local server).

Sessions open lazily on a server's first tool call, so only servers a run
actually uses get one; the caller decides which servers are in scope. They are
then kept for reuse:

* keyed by event loop, transport, URL and a hash of the request headers, so
  per-user tokens never share a session and a refreshed token opens a new one;
* at most ``BOW_MCP_POOL_SIZE`` sessions per worker (least recently used closed
  first; 0 turns pooling off), closed after ``BOW_MCP_POOL_IDLE_S`` idle;
* pinged before reuse once idle for ``PING_AFTER_S``, reopened if the ping fails.

A call that raises discards its session but is never retried on a new one: a
tool call may have side effects, so whether to try again is the caller's call.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PING_AFTER_S = 60.0
_PING_TIMEOUT_S = 5.0
_DEFAULT_SIZE = 32
_DEFAULT_IDLE_S = 300.0


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def pool_size() -> int:
    return max(0, int(_env_number("BOW_MCP_POOL_SIZE", _DEFAULT_SIZE)))


def idle_ttl() -> float:
    return max(1.0, _env_number("BOW_MCP_POOL_IDLE_S", _DEFAULT_IDLE_S))


def session_key(transport: str, url: str, headers: Dict[str, str]) -> Tuple[str, str, str]:
    """Credentials are part of the key, but only as a digest."""
    digest = hashlib.sha256(
        "\n".join(f"{k.lower()}:{v}" for k, v in sorted(headers.items())).encode()
    ).hexdigest()
    return (transport, url, digest)


class _Entry:
    """One open session, held open by a worker task until closed or idle."""

    def __init__(self, opener: Callable[[], Any]):
        self._opener = opener
        self.session = None
        self.in_flight = 0
        self.last_used = time.monotonic()
        self.dead = False
        self._ready = asyncio.Event()
        self._close = asyncio.Event()
        self._error: Optional[BaseException] = None
        self._task = asyncio.create_task(self._hold())

    async def _hold(self) -> None:
        try:
            async with self._opener() as session:
                self.session = session
                self._ready.set()
                while not self._close.is_set():
                    try:
                        await asyncio.wait_for(self._close.wait(), timeout=min(idle_ttl(), 30.0))
                    except asyncio.TimeoutError:
                        if self.in_flight == 0 and time.monotonic() - self.last_used >= idle_ttl():
                            break
        except BaseException as e:  # opening failed or the transport died
            self._error = e
        finally:
            self.dead = True
            self._ready.set()

    async def wait_ready(self):
        await self._ready.wait()
        if self.session is None or self.dead:
            raise self._error or ConnectionError("MCP session closed before it was ready")
        return self.session

    def close(self) -> None:
        self.dead = True
        self._close.set()


class McpSessionPool:
    def __init__(self):
        self._entries: Dict[Tuple[int, Tuple[str, str, str]], _Entry] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop = None

    def _loop_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def __len__(self) -> int:
        return sum(1 for e in self._entries.values() if not e.dead)

    async def _ping_ok(self, entry: _Entry) -> bool:
        try:
            await asyncio.wait_for(entry.session.send_ping(), timeout=_PING_TIMEOUT_S)
            return True
        except Exception:
            return False

    def _evict_over_cap(self, cap: int) -> None:
        live = [(k, e) for k, e in self._entries.items() if not e.dead]
        idle = sorted((x for x in live if x[1].in_flight == 0), key=lambda x: x[1].last_used)
        for k, e in idle[: max(0, len(live) - cap)]:
            e.close()
            self._entries.pop(k, None)

    @contextlib.asynccontextmanager
    async def session(self, key: Tuple[str, str, str], opener: Callable[[], Any]) -> AsyncIterator[Any]:
        """Yield a ready ClientSession for ``key``, opening one if needed."""
        loop_key = (id(asyncio.get_running_loop()), key)
        async with self._loop_lock():
            entry = self._entries.get(loop_key)
            if entry is not None and entry.dead:
                self._entries.pop(loop_key, None)
                entry = None
            if entry is None:
                entry = _Entry(opener)
                self._entries[loop_key] = entry
                self._evict_over_cap(pool_size())
            entry.in_flight += 1
        try:
            try:
                session = await entry.wait_ready()
            except BaseException:
                self._entries.pop(loop_key, None)
                raise
            if time.monotonic() - entry.last_used > PING_AFTER_S and entry.in_flight == 1:
                if not await self._ping_ok(entry):
                    entry.close()
                    self._entries.pop(loop_key, None)
                    entry.in_flight -= 1
                    entry = _Entry(opener)
                    entry.in_flight += 1
                    self._entries[loop_key] = entry
                    session = await entry.wait_ready()
            try:
                yield session
            except BaseException:
                # The session may be broken (transport error, expired session id).
                # Drop it; the next call opens a fresh one.
                entry.close()
                if self._entries.get(loop_key) is entry:
                    self._entries.pop(loop_key, None)
                raise
        finally:
            entry.in_flight -= 1
            entry.last_used = time.monotonic()

    async def aclose_all(self) -> None:
        """Close every session opened on the running loop and wait for them."""
        loop = asyncio.get_running_loop()
        mine = [(k, e) for k, e in self._entries.items() if k[0] == id(loop)]
        for k, e in mine:
            e.close()
            self._entries.pop(k, None)
        await asyncio.gather(*(e._task for _, e in mine), return_exceptions=True)


POOL = McpSessionPool()
