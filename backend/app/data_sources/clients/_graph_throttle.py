"""Microsoft Graph throttling discipline, shared by every Graph-backed client.

SharePoint / OneDrive throttling limits are fixed per app registration per
tenant and cannot be raised on request. Microsoft's guidance for a client is
therefore entirely behavioural: honour ``Retry-After`` on 429 / 503, decorate
traffic with an identifying ``User-Agent`` (undecorated apps are throttled
sooner), keep concurrency modest, and reduce the number of calls. An app that
keeps sending while throttled is escalated to a full block with a long
``Retry-After`` — which is what "continuously blocked" looks like from the
customer's side.

This module holds the process-wide state that makes those behaviours hold
across *every* client instance and thread hitting the same app registration:

- :class:`AppThrottleState` — one per app registration (tenant + client id):
  a shared "paused until" timestamp so a 429 seen by one thread stops every
  other thread (and every other connection using the same registration) from
  piling on, plus a bounded in-flight semaphore so N users each walking with
  K threads can never exceed a fixed burst.
- :class:`ListingCache` — a short-TTL cache of listing results so repeated
  live listings (per-user OAuth connections walk the library on every tool
  call) within one conversation don't re-enumerate the library.
- :class:`GraphThrottledError` — the error surfaced when a request could not
  be completed within the retry budget; carries the retry-after so callers
  can tell the user something actionable instead of a raw ``429``.

Every knob is overridable through the environment so an operator can tune a
struggling tenant without a deploy.
"""
from __future__ import annotations

import email.utils
import hashlib
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


#: Statuses that mean "try again later" rather than "your request is wrong".
#: 429 is throttling proper; 503 / 504 are what Graph returns when the
#: SharePoint backend sheds load, and Microsoft documents both as retryable
#: with the same ``Retry-After`` contract.
RETRY_STATUSES = frozenset({429, 503, 504})

#: Attempts per request (the first send plus retries).
GRAPH_MAX_ATTEMPTS = _env_int("BOW_GRAPH_MAX_ATTEMPTS", 5)
#: Total seconds one request may spend sleeping on throttle responses before
#: giving up. Background indexing can afford to wait; a chat tool call cannot
#: wait forever, and a Retry-After beyond this is surfaced as an error.
GRAPH_RETRY_BUDGET_S = _env_float("BOW_GRAPH_RETRY_BUDGET_S", 120.0)
#: When the app is already paused (another thread was throttled) and the
#: remaining pause exceeds this, fail fast instead of sleeping — every request
#: sent while blocked extends the block, and a chat turn is better served by a
#: clear "throttled, retry in N s" than by a two-minute stall.
GRAPH_FAIL_FAST_AFTER_S = _env_float("BOW_GRAPH_FAIL_FAST_S", 30.0)
#: Longest single Retry-After we honour in-line. Ordinary throttling comes
#: with a few seconds; a full block comes with minutes. Sleeping a chat turn
#: for minutes helps nobody, so anything longer than this is surfaced at once
#: as a GraphThrottledError (the scheduled reindex simply runs again later).
GRAPH_MAX_SINGLE_WAIT_S = _env_float("BOW_GRAPH_MAX_SINGLE_WAIT_S", 30.0)
#: Process-wide cap on concurrent in-flight Graph requests per app
#: registration, across all client instances and threads.
GRAPH_MAX_INFLIGHT = max(1, _env_int("BOW_GRAPH_MAX_INFLIGHT", 12))
#: How long a listing result is reused before the library is re-enumerated.
GRAPH_LISTING_CACHE_TTL_S = _env_float("BOW_GRAPH_LISTING_CACHE_TTL_S", 60.0)
#: Proactive slow-down: when Graph reports fewer than this many resource
#: units remaining in the window, pause until the window resets.
GRAPH_RATELIMIT_FLOOR = _env_int("BOW_GRAPH_RATELIMIT_FLOOR", 20)


def _read_version() -> str:
    """The product version for the User-Agent, read from the repo's VERSION
    file (settings reads it relative to the cwd, which is not stable here)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "VERSION"
        if candidate.is_file():
            try:
                return candidate.read_text().strip() or "0"
            except OSError:
                break
    return "0"


#: Microsoft's documented decoration format for SharePoint / Graph traffic:
#: ``ISV|<Company>|<App>/<Version>``. Undecorated traffic is throttled more
#: aggressively and is invisible in the tenant's throttling diagnostics.
GRAPH_USER_AGENT = f"ISV|BagOfWords|bagofwords/{_read_version()}"


class GraphThrottledError(ValueError):
    """Graph kept throttling past the retry budget (or the app is paused).

    A ``ValueError`` so every existing ``except ValueError`` around Graph
    calls keeps working; the extra attributes let the tool layer produce a
    message the model can act on ("retry in 40 s") instead of a raw status.
    """

    def __init__(self, url: str, status: int, retry_after: float, detail: str = ""):
        self.url = url
        self.status = status
        self.retry_after = float(retry_after)
        self.detail = detail
        super().__init__(
            f"Microsoft Graph is throttling this app (HTTP {status}); "
            f"retry in about {int(round(self.retry_after)) or 1}s. {detail}".strip()
        )


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """``Retry-After`` as seconds. Accepts delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def backoff_delay(attempt: int) -> float:
    """Jittered exponential backoff for retryable responses that carry no
    ``Retry-After``: ~1-3 s, ~2-6 s, ~4-12 s ... capped at 30 s."""
    base = min(2 ** max(0, attempt - 1), 15)
    return base * (1.0 + random.random())


class AppThrottleState:
    """Per-app-registration state shared by every client in the process."""

    def __init__(self, key: str):
        self.key = key
        self._lock = threading.Lock()
        self._pause_until = 0.0  # time.monotonic()
        self.last_status: int = 0
        self.inflight = threading.BoundedSemaphore(GRAPH_MAX_INFLIGHT)
        # Counters for logs / tests.
        self.throttled_responses = 0

    def pause_for(self, seconds: float, status: int = 429) -> None:
        """Hold every request for this app for ``seconds`` (never shortens an
        existing, longer pause)."""
        until = time.monotonic() + max(0.0, float(seconds))
        with self._lock:
            if until > self._pause_until:
                self._pause_until = until
            self.last_status = status

    def remaining_pause(self) -> float:
        with self._lock:
            return max(0.0, self._pause_until - time.monotonic())

    def clear(self) -> None:
        with self._lock:
            self._pause_until = 0.0
            self.last_status = 0

    def observe_headers(self, headers: Mapping[str, str]) -> None:
        """Proactive slow-down from Graph's ``RateLimit-*`` headers.

        Graph adds ``RateLimit-Limit`` / ``RateLimit-Remaining`` /
        ``RateLimit-Reset`` to SharePoint responses once an app is close to
        its allowance. Pausing at the floor turns a would-be 429 into a short
        wait — and a would-be block into nothing at all.
        """
        try:
            remaining_raw = headers.get("RateLimit-Remaining") or headers.get("ratelimit-remaining")
            if remaining_raw is None:
                return
            remaining = int(float(remaining_raw))
            reset_raw = headers.get("RateLimit-Reset") or headers.get("ratelimit-reset")
            reset = float(reset_raw) if reset_raw else 0.0
        except (TypeError, ValueError):
            return
        if remaining <= GRAPH_RATELIMIT_FLOOR and reset > 0:
            logger.warning(
                "graph throttle[%s]: RateLimit-Remaining=%d, pausing %.0fs until the "
                "window resets", self.key, remaining, reset,
            )
            self.pause_for(min(reset, GRAPH_MAX_SINGLE_WAIT_S), status=0)


_STATES: Dict[str, AppThrottleState] = {}
_STATES_LOCK = threading.Lock()


def throttle_state_for(key: str) -> AppThrottleState:
    with _STATES_LOCK:
        state = _STATES.get(key)
        if state is None:
            state = _STATES[key] = AppThrottleState(key)
        return state


def reset_throttle_states() -> None:
    """Tests only."""
    with _STATES_LOCK:
        _STATES.clear()


def token_fingerprint(token: Optional[str]) -> str:
    """Short, non-reversible identity for a delegated token (keys the listing
    cache per signed-in user without holding the token itself)."""
    if not token:
        return "app"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class ListingCache:
    """Tiny TTL + LRU cache for listing results (process-local)."""

    def __init__(self, ttl_s: float = GRAPH_LISTING_CACHE_TTL_S, max_entries: int = 256):
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._entries: Dict[Tuple, Tuple[float, List[dict]]] = {}
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_s > 0

    def get(self, key: Tuple) -> Optional[List[dict]]:
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None or hit[0] <= now:
                if hit is not None:
                    self._entries.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            # Fresh dicts: callers slice / mutate rows freely.
            return [dict(r) for r in hit[1]]

    def put(self, key: Tuple, rows: List[dict]) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            if len(self._entries) >= self.max_entries:
                # Drop expired first, then the oldest.
                for k in [k for k, (exp, _) in self._entries.items() if exp <= now]:
                    self._entries.pop(k, None)
                while len(self._entries) >= self.max_entries:
                    self._entries.pop(next(iter(self._entries)), None)
            self._entries[key] = (now + self.ttl_s, [dict(r) for r in rows])

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = self.misses = 0


listing_cache = ListingCache()
