import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Union
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ee.license import has_feature
from app.models.connection import Connection
from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.membership import Membership
from app.models.role_assignment import RoleAssignment
from app.models.usage_policy import (
    UsageCounter,
    UsageEvent,
    UsagePolicy,
    UsagePolicyAssignment,
    UsagePolicyConnectionOverride,
)
from app.schemas.usage_policy_schema import (
    EffectiveUsagePolicySchema,
    UsageDailyPointSchema,
    UsageDailySeriesSchema,
    UsagePolicyAssignmentSchema,
    UsagePolicyConnectionOverrideInput,
    UsagePolicyCreate,
    UsagePolicyPrincipalAssignmentResult,
    UsagePolicySchema,
    UsagePolicyUpdate,
    UsageQuotaConnectionSchema,
    UsageQuotaDailySchema,
    UsageQuotaMetricSchema,
    UsageQuotaSpendMetricSchema,
    UsageQuotaSummarySchema,
)


METRIC_LLM_TOKENS = "llm_tokens"
METRIC_DATA_QUERIES = "data_queries"
METRIC_DATA_BYTES = "data_bytes"
# LLM spend is tracked in micro-USD (USD * 1_000_000) so it fits the integer
# UsageCounter.used column while preserving the 6-decimal precision of the
# per-call cost figures. Convert back to USD only at the API boundary.
METRIC_LLM_COST = "llm_cost_micro_usd"
SCOPE_ORGANIZATION = "organization"
SCOPE_CONNECTION = "connection"

# USD <-> micro-USD conversion factor (6 decimal places, matching the
# Numeric(18, 6) cost columns on llm_usage_records / usage_policies).
USD_MICRO = 1_000_000


def usd_to_micro(amount_usd: Optional[float]) -> Optional[int]:
    if amount_usd is None:
        return None
    return int(round(float(amount_usd) * USD_MICRO))


def micro_to_usd(amount_micro: Optional[int]) -> Optional[float]:
    if amount_micro is None:
        return None
    return round(int(amount_micro) / USD_MICRO, 6)


# Daily counters live in the same usage_counters table as the monthly ones,
# under their own metric name. A separate name (not just a different window)
# is required: in a UTC org the day window of the 1st starts at the same
# instant as the month window, and the counter's unique key would merge them.
DAILY_METRIC_SUFFIX = ":daily"

WINDOW_MONTHLY = "monthly"
WINDOW_DAILY = "daily"

# Data-cache key standing in for a connection id on the daily data caps, which
# count every connection together (connection overrides are monthly only).
_DAILY_DATA_KEY = "__daily__"


def daily_metric(metric: str) -> str:
    return f"{metric}{DAILY_METRIC_SUFFIX}"


class UsageLimitExceeded(Exception):
    def __init__(
        self,
        detail: str,
        *,
        metric: str,
        limit: int,
        used: int,
        requested: int,
        window: str = WINDOW_MONTHLY,
        resets_at: Optional[datetime] = None,
        timezone: Optional[str] = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.metric = metric
        self.limit = limit
        self.used = used
        self.requested = requested
        self.window = window
        self.resets_at = resets_at
        self.timezone = timezone


# The monthly wording is load-bearing: app/ai/llm/errors.py recognises our own
# caps by these phrases (_INTERNAL_QUOTA_MARKERS) so they never read as a
# provider outage and never trigger a model fallback. Keep the two in sync.
_LIMIT_MESSAGES = {
    (WINDOW_MONTHLY, METRIC_LLM_TOKENS): "Monthly LLM token quota exceeded.",
    (WINDOW_MONTHLY, METRIC_LLM_COST): "Monthly LLM spend quota exceeded.",
    (WINDOW_MONTHLY, METRIC_DATA_QUERIES): "Monthly usage quota exceeded.",
    (WINDOW_MONTHLY, METRIC_DATA_BYTES): "Monthly usage quota exceeded.",
    (WINDOW_DAILY, METRIC_LLM_TOKENS): "Daily LLM token quota exceeded.",
    (WINDOW_DAILY, METRIC_LLM_COST): "Daily LLM spend quota exceeded.",
    (WINDOW_DAILY, METRIC_DATA_QUERIES): "Daily usage quota exceeded (data queries).",
    (WINDOW_DAILY, METRIC_DATA_BYTES): "Daily usage quota exceeded (data scanned).",
}


def _format_wait(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def build_limit_exceeded(
    metric: str,
    window: str,
    *,
    limit: int,
    used: int,
    requested: int,
    tz_name: Optional[str] = None,
    now: Optional[datetime] = None,
) -> UsageLimitExceeded:
    """One place that words a cap error, so every enforcement path (LLM, data,
    buffered or direct) tells the user which window ran out and when it resets."""
    now = now or datetime.utcnow()
    detail = _LIMIT_MESSAGES[(window, metric)]
    if window == WINDOW_DAILY:
        tz_name = tz_name or "UTC"
        _, resets_at = current_day_window(tz_name, now)
        detail += f" Resets at 00:00 {tz_name} (in {_format_wait(resets_at - now)})."
    else:
        _, resets_at = current_month_window(now)
    return UsageLimitExceeded(
        detail,
        metric=metric,
        limit=limit,
        used=used,
        requested=requested,
        window=window,
        resets_at=resets_at,
        timezone=tz_name,
    )


@dataclass
class EffectiveUsageLimits:
    enabled: bool
    organization_id: str
    user_id: str
    monthly_token_limit: Optional[int] = None
    monthly_query_limit: Optional[int] = None
    monthly_data_bytes_limit: Optional[int] = None
    # Stored in micro-USD (USD * 1_000_000). None = unlimited spend.
    monthly_spend_micro_usd: Optional[int] = None
    # Daily caps (None = no daily cap). The day is midnight to midnight in
    # `timezone`, the organization's IANA timezone.
    daily_token_limit: Optional[int] = None
    daily_query_limit: Optional[int] = None
    daily_data_bytes_limit: Optional[int] = None
    daily_spend_micro_usd: Optional[int] = None
    timezone: str = "UTC"
    policy_ids: List[str] = field(default_factory=list)
    resolution_source: str = "default"
    query_base_by_policy: Dict[str, Optional[int]] = field(default_factory=dict)
    query_overrides_by_policy: Dict[str, Dict[str, Optional[int]]] = field(default_factory=dict)
    data_bytes_base_by_policy: Dict[str, Optional[int]] = field(default_factory=dict)
    data_bytes_overrides_by_policy: Dict[str, Dict[str, Optional[int]]] = field(default_factory=dict)

    def query_limit_for_connection(self, connection_id: Optional[str]) -> Optional[int]:
        if not connection_id:
            return self.monthly_query_limit
        limits: list[Optional[int]] = []
        for policy_id in self.policy_ids:
            override = self.query_overrides_by_policy.get(policy_id, {})
            if connection_id in override:
                limits.append(override[connection_id])
            else:
                limits.append(self.query_base_by_policy.get(policy_id))
        if limits:
            base_limits = [
                limit for limit in limits if limit is not None
            ]
            if base_limits:
                return min(base_limits)
            return None
        return self.monthly_query_limit

    def data_bytes_limit_for_connection(self, connection_id: Optional[str]) -> Optional[int]:
        if not connection_id:
            return self.monthly_data_bytes_limit
        limits: list[Optional[int]] = []
        for policy_id in self.policy_ids:
            override = self.data_bytes_overrides_by_policy.get(policy_id, {})
            if connection_id in override:
                limits.append(override[connection_id])
            else:
                limits.append(self.data_bytes_base_by_policy.get(policy_id))
        if limits:
            base_limits = [
                limit for limit in limits if limit is not None
            ]
            if base_limits:
                return min(base_limits)
            return None
        return self.monthly_data_bytes_limit

    def to_schema(self) -> EffectiveUsagePolicySchema:
        return EffectiveUsagePolicySchema(
            enabled=self.enabled,
            organization_id=self.organization_id,
            user_id=self.user_id,
            monthly_token_limit=self.monthly_token_limit,
            monthly_query_limit=self.monthly_query_limit,
            monthly_data_bytes_limit=self.monthly_data_bytes_limit,
            monthly_spend_limit_usd=micro_to_usd(self.monthly_spend_micro_usd),
            daily_token_limit=self.daily_token_limit,
            daily_query_limit=self.daily_query_limit,
            daily_data_bytes_limit=self.daily_data_bytes_limit,
            daily_spend_limit_usd=micro_to_usd(self.daily_spend_micro_usd),
            timezone=self.timezone,
            policy_ids=self.policy_ids,
            resolution_source=self.resolution_source,
        )


@dataclass
class UsageLimitContext:
    """Per-agent quota state.

    The hot path (`add_tokens`) used to issue a SELECT FOR UPDATE +
    UPDATE per LLM call, blocking the LLM completion on a row lock.
    Now we accumulate in-memory and `flush()` once at end of agent run
    (or when explicitly drained). Tradeoff: counter lags behind reality
    by one agent run; preflight enforcement gets a slight grace window.
    Acceptable for monthly quotas; not for hard rate limits.

    The check path (`check_tokens`) caches the effective limit + the
    last-known used amount and only refreshes from DB on a TTL or when
    the cached budget is about to be exceeded. Without this, every LLM
    call (~6 per agent) does a SELECT against usage_counters; at 20
    concurrent agents that's ~120 connection checkouts in a few seconds,
    blowing past the singleton pool size. The cache makes the steady-
    state check purely in-memory.
    """

    organization_id: str
    user_id: str
    source: str
    source_ref_id: Optional[str] = None
    session_maker: Optional[Callable[[], AsyncSession]] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    # Accumulated, not yet persisted. Plain int because CPython GIL
    # makes `int += int` atomic; only one thread/loop adds at a time
    # in our LLM call path so explicit locking is unnecessary here.
    _pending_tokens: int = field(default=0, init=False, repr=False)
    # Accumulated LLM spend, buffered in micro-USD just like _pending_tokens.
    _pending_cost_micro: int = field(default=0, init=False, repr=False)
    _last_metadata: Optional[dict] = field(default=None, init=False, repr=False)
    _last_cost_metadata: Optional[dict] = field(default=None, init=False, repr=False)
    # Cached limit + counter snapshot. `_cache_loaded_at` < 0 means
    # "never loaded"; the next check_tokens() will load it. Refresh
    # cadence is the TTL below; we additionally force-refresh when the
    # uncached headroom is about to be exceeded by pending+requested.
    _cached_limit: Optional[int] = field(default=None, init=False, repr=False)
    _cached_used: int = field(default=0, init=False, repr=False)
    # Spend cap mirror of the token cache, in micro-USD.
    _cached_spend_limit_micro: Optional[int] = field(default=None, init=False, repr=False)
    _cached_spend_used_micro: int = field(default=0, init=False, repr=False)
    # Daily mirrors of the four values above, for the day window that ends at
    # `_cached_day_end` (UTC). Crossing that instant forces a refresh, so a
    # long-lived context starts the new day at zero instead of yesterday's sum.
    _cached_daily_limit: Optional[int] = field(default=None, init=False, repr=False)
    _cached_daily_used: int = field(default=0, init=False, repr=False)
    _cached_daily_spend_limit_micro: Optional[int] = field(default=None, init=False, repr=False)
    _cached_daily_spend_used_micro: int = field(default=0, init=False, repr=False)
    _cached_day_end: Optional[datetime] = field(default=None, init=False, repr=False)
    _cached_timezone: str = field(default="UTC", init=False, repr=False)
    _cache_loaded_at: float = field(default=-1.0, init=False, repr=False)
    _CACHE_TTL_SECONDS: float = field(default=30.0, init=False, repr=False)
    _cache_lock: Optional[asyncio.Lock] = field(default=None, init=False, repr=False)
    # Buffered data-plane metering (queries / bytes), mirroring the token/cost
    # buffers above. Events are appended from sandboxed code-exec worker
    # THREADS (see TrackedClient in code_execution.py), so unlike the int
    # buffers these need an explicit threading.Lock. Each event:
    # {metric, connection_id, amount, source, source_ref_id, metadata}.
    _pending_data_events: List[dict] = field(default_factory=list, init=False, repr=False)
    _data_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # Per-(metric, connection_id) enforcement cache: {"limit", "used", "loaded_at"}.
    # Same TTL/refresh-then-raise discipline as the token cache.
    _data_cache: Dict[tuple, dict] = field(default_factory=dict, init=False, repr=False)
    # Set on contexts created via for_source(): buffered amounts/events and
    # quota checks delegate to the root context so one end-of-run flush covers
    # everything added under any derived label.
    _parent: Optional["UsageLimitContext"] = field(default=None, init=False, repr=False)

    def _root(self) -> "UsageLimitContext":
        ctx = self
        while ctx._parent is not None:
            ctx = ctx._parent
        return ctx

    def for_source(self, source: str, source_ref_id: Optional[str] = None) -> "UsageLimitContext":
        """Derive a child context that relabels `source` but shares the
        parent's accumulation buffers and quota caches (via `_parent`
        delegation). Without the delegation, usage added through a derived
        context (e.g. the coder's tokens inside a create_data tool call) sat
        in the child's own buffers, which nothing ever flushed."""
        child = UsageLimitContext(
            organization_id=self.organization_id,
            user_id=self.user_id,
            source=source,
            source_ref_id=source_ref_id or self.source_ref_id,
            session_maker=self.session_maker,
            loop=self.loop,
        )
        child._parent = self
        return child

    def add_tokens(self, amount: int, metadata: Optional[dict] = None) -> None:
        """Buffer tokens for a later flush. Cheap, no IO, no lock.

        Skips entirely when the `usage_limits` feature is off — no point
        buffering data that flush() would no-op on anyway, and avoids
        firing the end-of-agent bg task at all in the common case.
        """
        if amount <= 0:
            return
        if not has_feature("usage_limits"):
            return
        root = self._root()
        root._pending_tokens += int(amount)
        if metadata is not None:
            root._last_metadata = metadata

    def add_cost(self, amount_micro_usd: int, metadata: Optional[dict] = None) -> None:
        """Buffer LLM spend (in micro-USD) for a later flush. Mirrors add_tokens."""
        if amount_micro_usd <= 0:
            return
        if not has_feature("usage_limits"):
            return
        root = self._root()
        root._pending_cost_micro += int(amount_micro_usd)
        if metadata is not None:
            root._last_cost_metadata = metadata

    @property
    def pending_tokens(self) -> int:
        return self._pending_tokens

    @property
    def pending_cost_micro(self) -> int:
        return self._pending_cost_micro

    async def _refresh_quota_cache(self) -> None:
        """Load the monthly and daily token/spend limits and counters. One
        SELECT per capped value; uncapped values cost nothing."""
        if self.session_maker is None:
            return
        async with self.session_maker() as db:
            limits = await usage_policy_service.resolve_effective_limits(
                db, self.organization_id, self.user_id
            )
            day_start, day_end = current_day_window(limits.timezone)

            async def _used(metric: str, window_start: Optional[datetime] = None) -> int:
                return await usage_policy_service._get_counter_used(
                    db,
                    org_id=self.organization_id,
                    user_id=self.user_id,
                    metric=metric,
                    scope_type=SCOPE_ORGANIZATION,
                    scope_ref_id="",
                    window_start=window_start,
                )

            self._cached_limit = limits.monthly_token_limit
            self._cached_used = await _used(METRIC_LLM_TOKENS) if limits.monthly_token_limit is not None else 0
            self._cached_spend_limit_micro = limits.monthly_spend_micro_usd
            self._cached_spend_used_micro = (
                await _used(METRIC_LLM_COST) if limits.monthly_spend_micro_usd is not None else 0
            )
            self._cached_daily_limit = limits.daily_token_limit
            self._cached_daily_used = (
                await _used(daily_metric(METRIC_LLM_TOKENS), day_start)
                if limits.daily_token_limit is not None else 0
            )
            self._cached_daily_spend_limit_micro = limits.daily_spend_micro_usd
            self._cached_daily_spend_used_micro = (
                await _used(daily_metric(METRIC_LLM_COST), day_start)
                if limits.daily_spend_micro_usd is not None else 0
            )
            self._cached_day_end = day_end
            self._cached_timezone = limits.timezone
        import time as _time
        self._cache_loaded_at = _time.monotonic()

    def _cache_is_stale(self, now: float) -> bool:
        if self._cache_loaded_at < 0 or (now - self._cache_loaded_at) >= self._CACHE_TTL_SECONDS:
            return True
        # The day rolled over since the last load: yesterday's daily usage
        # must not keep blocking (or keep counting toward) today.
        return self._cached_day_end is not None and datetime.utcnow() >= self._cached_day_end

    async def _ensure_fresh(self) -> None:
        import time as _time
        now = _time.monotonic()
        # Need a refresh? First call ever, TTL expired, or a new day began.
        if self._cache_is_stale(now):
            # Serialize concurrent refreshers — only one DB roundtrip even
            # when N tasks race here at the same agent run's first call.
            if self._cache_lock is None:
                self._cache_lock = asyncio.Lock()
            async with self._cache_lock:
                if self._cache_is_stale(now):
                    await self._refresh_quota_cache()

    async def prefetch(self) -> None:
        """Load the quota cache ahead of the first LLM call, so that call's
        check_tokens does not wait on the DB. Enforces nothing; errors are
        left for check_tokens to meet."""
        if self._parent is not None:
            return await self._root().prefetch()
        if not has_feature("usage_limits") or self.session_maker is None:
            return
        try:
            await self._ensure_fresh()
        except Exception:
            pass

    async def check_tokens(self, requested_tokens: int) -> None:
        """Cheap pre-LLM-call quota check. Hits the DB at most once per
        TTL window; otherwise compares against the cached limit/used.

        Enforces both the monthly token cap and the monthly USD spend cap.
        Raises UsageLimitExceeded if the call would push us over either.
        """
        if self._parent is not None:
            # Derived contexts share the root's cache and pending buffers.
            return await self._root().check_tokens(requested_tokens)
        if not has_feature("usage_limits") or self.session_maker is None:
            return
        if requested_tokens <= 0:
            requested_tokens = 1

        await self._ensure_fresh()

        # Spend caps: this call's dollar cost isn't known until after the
        # provider responds, so we can't project it. Instead we stop once
        # the buffered+recorded spend has already reached a cap — the same
        # one-call grace window the token cap tolerates.
        if self._cached_spend_limit_micro is not None or self._cached_daily_spend_limit_micro is not None:
            await self._enforce_spend_cap()

        if self._token_breach(requested_tokens) is None:
            return

        # We *might* be over — but our cache could be stale low. Force a
        # refresh and re-check before raising.
        if self._cache_lock is None:
            self._cache_lock = asyncio.Lock()
        async with self._cache_lock:
            await self._refresh_quota_cache()
        breach = self._token_breach(requested_tokens)
        if breach is not None:
            window, limit, used = breach
            raise build_limit_exceeded(
                METRIC_LLM_TOKENS, window,
                limit=limit, used=used, requested=int(requested_tokens),
                tz_name=self._cached_timezone,
            )

    def _token_breach(self, requested_tokens: int) -> Optional[tuple]:
        """(window, limit, used) of the first token cap this call would pass,
        or None. Monthly is checked first: when both are exhausted the user
        should hear about the one that lasts longer."""
        pending = self._pending_tokens
        for window, limit, used in (
            (WINDOW_MONTHLY, self._cached_limit, self._cached_used),
            (WINDOW_DAILY, self._cached_daily_limit, self._cached_daily_used),
        ):
            if limit is not None and used + pending + int(requested_tokens) > limit:
                return window, limit, used + pending
        return None

    def _spend_breach(self) -> Optional[tuple]:
        pending = self._pending_cost_micro
        for window, limit, used in (
            (WINDOW_MONTHLY, self._cached_spend_limit_micro, self._cached_spend_used_micro),
            (WINDOW_DAILY, self._cached_daily_spend_limit_micro, self._cached_daily_spend_used_micro),
        ):
            if limit is not None and used + pending >= limit:
                return window, limit, used + pending
        return None

    async def _enforce_spend_cap(self) -> None:
        """Raise UsageLimitExceeded if accumulated LLM spend has hit a cap."""
        if self._spend_breach() is None:
            return
        # Cache might be stale low — force a refresh and re-check before raising.
        if self._cache_lock is None:
            self._cache_lock = asyncio.Lock()
        async with self._cache_lock:
            await self._refresh_quota_cache()
        breach = self._spend_breach()
        if breach is not None:
            window, limit, used = breach
            raise build_limit_exceeded(
                METRIC_LLM_COST, window,
                limit=limit, used=used, requested=0,
                tz_name=self._cached_timezone,
            )

    async def flush(self) -> None:
        """Persist any buffered tokens, spend, and data-plane usage events.
        Called by the agent at end of run (and by non-agent owners of a
        context after their execution finishes)."""
        if self._parent is not None:
            # Buffers live on the root; flush there.
            return await self._root().flush()
        if self.session_maker is None:
            return
        await self._flush_tokens()
        await self._flush_cost()
        await self._flush_data_events()

    async def _flush_tokens(self) -> None:
        if self._pending_tokens <= 0:
            return
        amount = self._pending_tokens
        meta = self._last_metadata
        self._pending_tokens = 0
        self._last_metadata = None
        try:
            await usage_policy_service.record_llm_tokens_with_context(self, amount, meta)
            # Reflect the just-persisted amount in the cache so the next
            # check_tokens doesn't undercount.
            self._cached_used += amount
            self._cached_daily_used += amount
        except Exception:
            # Best-effort. Re-credit so a future flush retries.
            self._pending_tokens += amount
            if meta is not None:
                self._last_metadata = meta
            raise

    async def _flush_cost(self) -> None:
        if self._pending_cost_micro <= 0:
            return
        amount_micro = self._pending_cost_micro
        meta = self._last_cost_metadata
        self._pending_cost_micro = 0
        self._last_cost_metadata = None
        try:
            await usage_policy_service.record_llm_cost_with_context(self, amount_micro, meta)
            self._cached_spend_used_micro += amount_micro
            self._cached_daily_spend_used_micro += amount_micro
        except Exception:
            self._pending_cost_micro += amount_micro
            if meta is not None:
                self._last_cost_metadata = meta
            raise

    # ── Data-plane metering (queries / bytes) — buffered like tokens/cost ──
    #
    # Rationale (docs/feedback-loops/agent-latency-deep-dive.md): the previous
    # shape did a synchronous counter+event WRITE around every sandboxed
    # execute_query. On SQLite those writes queue behind the agent's writer
    # lock, wait out busy_timeout (30s) twice per query, then get skipped —
    # +60s per execution and the metering lost anyway. Now: enforcement is a
    # cached READ (WAL-safe), recording is buffered here and persisted by the
    # same end-of-run flush() the token/cost buffers already use.

    def _add_data_event(self, metric: str, connection_id: str, amount: int,
                        metadata: Optional[dict]) -> None:
        if amount <= 0 or not has_feature("usage_limits"):
            return
        # Source labeling comes from THIS context; storage lives on the root
        # so a single end-of-run flush covers derived (for_source) contexts.
        event = {
            "metric": metric,
            "connection_id": str(connection_id or ""),
            "amount": int(amount),
            "source": self.source,
            "source_ref_id": self.source_ref_id,
            "metadata": metadata,
        }
        root = self._root()
        with root._data_lock:
            root._pending_data_events.append(event)
            for key in ((metric, event["connection_id"]), (metric, _DAILY_DATA_KEY)):
                entry = root._data_cache.get(key)
                if entry is not None:
                    entry["pending"] = entry.get("pending", 0) + int(amount)

    def add_data_query(self, connection_id: str, metadata: Optional[dict] = None) -> None:
        """Buffer one data-query usage event. Cheap, no IO, thread-safe."""
        self._add_data_event(METRIC_DATA_QUERIES, connection_id, 1, metadata)

    def add_data_bytes(self, connection_id: str, amount: int, metadata: Optional[dict] = None) -> None:
        """Buffer a data-bytes usage event. Cheap, no IO, thread-safe."""
        self._add_data_event(METRIC_DATA_BYTES, connection_id, int(amount), metadata)

    async def _refresh_data_cache_entry(self, metric: str, connection_id: str) -> dict:
        """Load the effective limit + current counter for one (metric, key).

        `connection_id` is a real connection id for the monthly per-connection
        caps, or `_DAILY_DATA_KEY` for the daily cap, which counts every
        connection together in the organization's current day."""
        limit = None
        used = 0
        expires_at = None
        tz_name = "UTC"
        if self.session_maker is not None:
            async with self.session_maker() as db:
                limits = await usage_policy_service.resolve_effective_limits(
                    db, self.organization_id, self.user_id
                )
                tz_name = limits.timezone
                if connection_id == _DAILY_DATA_KEY:
                    limit = (
                        limits.daily_query_limit if metric == METRIC_DATA_QUERIES
                        else limits.daily_data_bytes_limit
                    )
                    day_start, expires_at = current_day_window(limits.timezone)
                    if limit is not None:
                        used = await usage_policy_service._get_counter_used(
                            db,
                            org_id=self.organization_id,
                            user_id=self.user_id,
                            metric=daily_metric(metric),
                            scope_type=SCOPE_ORGANIZATION,
                            scope_ref_id="",
                            window_start=day_start,
                        )
                else:
                    if metric == METRIC_DATA_QUERIES:
                        limit = limits.query_limit_for_connection(connection_id)
                    else:
                        limit = limits.data_bytes_limit_for_connection(connection_id)
                    if limit is not None:
                        used = await usage_policy_service._get_counter_used(
                            db,
                            org_id=self.organization_id,
                            user_id=self.user_id,
                            metric=metric,
                            scope_type=SCOPE_CONNECTION,
                            scope_ref_id=connection_id,
                        )
        import time as _time
        with self._data_lock:
            prior = self._data_cache.get((metric, connection_id)) or {}
            entry = {
                "limit": limit,
                "used": used,
                "loaded_at": _time.monotonic(),
                "expires_at": expires_at,
                "timezone": tz_name,
                # keep unflushed local usage in the projection
                "pending": prior.get("pending", 0),
            }
            self._data_cache[(metric, connection_id)] = entry
        return entry

    def _data_entry_stale(self, entry: Optional[dict]) -> bool:
        import time as _time
        if entry is None or (_time.monotonic() - entry["loaded_at"]) >= self._CACHE_TTL_SECONDS:
            return True
        expires_at = entry.get("expires_at")
        return expires_at is not None and datetime.utcnow() >= expires_at

    async def _check_data(self, metric: str, connection_id: str, amount: int) -> None:
        """Pre-query quota check. In-memory against the cached limit/counter
        (refreshed at most once per TTL); force-refresh before raising so a
        stale-low cache can't produce a false rejection. Same one-call grace
        window the token cap tolerates."""
        if not has_feature("usage_limits") or self.session_maker is None:
            return
        connection_id = str(connection_id or "")
        entry = self._data_cache.get((metric, connection_id))
        if self._data_entry_stale(entry):
            if self._cache_lock is None:
                self._cache_lock = asyncio.Lock()
            async with self._cache_lock:
                entry = self._data_cache.get((metric, connection_id))
                if self._data_entry_stale(entry):
                    entry = await self._refresh_data_cache_entry(metric, connection_id)
        if entry["limit"] is None:
            return
        projected = entry["used"] + entry.get("pending", 0) + int(amount)
        if projected <= entry["limit"]:
            return
        # Might be stale low — refresh and re-check before raising.
        entry = await self._refresh_data_cache_entry(metric, connection_id)
        if entry["limit"] is None:
            return
        projected = entry["used"] + entry.get("pending", 0) + int(amount)
        if projected > entry["limit"]:
            raise build_limit_exceeded(
                metric,
                WINDOW_DAILY if connection_id == _DAILY_DATA_KEY else WINDOW_MONTHLY,
                limit=entry["limit"],
                used=entry["used"] + entry.get("pending", 0),
                requested=int(amount),
                tz_name=entry.get("timezone"),
            )

    async def check_data_query(self, connection_id: str) -> None:
        root = self._root()
        await root._check_data(METRIC_DATA_QUERIES, connection_id, 1)
        await root._check_data(METRIC_DATA_QUERIES, _DAILY_DATA_KEY, 1)

    async def check_data_bytes(self, connection_id: str, amount: int) -> None:
        root = self._root()
        await root._check_data(METRIC_DATA_BYTES, connection_id, int(amount))
        await root._check_data(METRIC_DATA_BYTES, _DAILY_DATA_KEY, int(amount))

    async def _flush_data_events(self) -> None:
        with self._data_lock:
            if not self._pending_data_events:
                return
            events = self._pending_data_events
            self._pending_data_events = []
        try:
            await usage_policy_service.record_data_usage_with_context(self, events)
        except Exception:
            # Best-effort. Re-credit so a future flush retries.
            with self._data_lock:
                self._pending_data_events = events + self._pending_data_events
            raise
        # Move the flushed amounts from "pending" into "used" so checks
        # stay accurate without a DB refresh.
        with self._data_lock:
            for event in events:
                for key in ((event["metric"], event["connection_id"]), (event["metric"], _DAILY_DATA_KEY)):
                    entry = self._data_cache.get(key)
                    if entry is not None:
                        entry["pending"] = max(entry.get("pending", 0) - event["amount"], 0)
                        entry["used"] = entry.get("used", 0) + event["amount"]

    def run_blocking(self, coroutine):
        if self.loop and self.loop.is_running():
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("Cannot run usage limit check synchronously on the running event loop")


class UsagePolicyService:
    async def list_policies(self, db: AsyncSession, org_id: str) -> List[UsagePolicySchema]:
        result = await db.execute(
            select(UsagePolicy)
            .options(
                selectinload(UsagePolicy.assignments),
                selectinload(UsagePolicy.connection_overrides),
            )
            .where(UsagePolicy.organization_id == org_id, UsagePolicy.deleted_at.is_(None))
            .order_by(UsagePolicy.name)
        )
        return [self._policy_to_schema(policy) for policy in result.scalars().all()]

    async def create_policy(self, db: AsyncSession, org_id: str, data: UsagePolicyCreate) -> UsagePolicySchema:
        self._validate_daily_within_monthly(data)
        policy = UsagePolicy(
            organization_id=org_id,
            name=data.name,
            description=data.description,
            monthly_token_limit=data.monthly_token_limit,
            monthly_query_limit=data.monthly_query_limit,
            monthly_data_bytes_limit=data.monthly_data_bytes_limit,
            monthly_spend_limit_usd=data.monthly_spend_limit_usd,
            daily_token_limit=data.daily_token_limit,
            daily_query_limit=data.daily_query_limit,
            daily_data_bytes_limit=data.daily_data_bytes_limit,
            daily_spend_limit_usd=data.daily_spend_limit_usd,
            enabled=data.enabled,
        )
        db.add(policy)
        try:
            await db.flush()
            await self._sync_assignments(db, org_id, policy.id, data.assignments)
            await self._sync_connection_overrides(db, org_id, policy.id, data.connection_overrides)
            policy_id = policy.id
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"A usage policy named '{data.name}' already exists.",
            )
        db.expire_all()
        return await self.get_policy(db, org_id, policy_id)

    async def get_policy(self, db: AsyncSession, org_id: str, policy_id: str) -> UsagePolicySchema:
        policy = await self._get_policy_model(db, org_id, policy_id)
        return self._policy_to_schema(policy)

    async def update_policy(
        self,
        db: AsyncSession,
        org_id: str,
        policy_id: str,
        data: UsagePolicyUpdate,
    ) -> UsagePolicySchema:
        policy = await self._get_policy_model(db, org_id, policy_id)
        fields_set = data.model_fields_set
        if "name" in fields_set:
            policy.name = data.name
        if "description" in fields_set:
            policy.description = data.description
        if "monthly_token_limit" in fields_set:
            policy.monthly_token_limit = data.monthly_token_limit
        if "monthly_query_limit" in fields_set:
            policy.monthly_query_limit = data.monthly_query_limit
        if "monthly_data_bytes_limit" in fields_set:
            policy.monthly_data_bytes_limit = data.monthly_data_bytes_limit
        if "monthly_spend_limit_usd" in fields_set:
            policy.monthly_spend_limit_usd = data.monthly_spend_limit_usd
        for name in _DAILY_LIMIT_FIELDS:
            if name in fields_set:
                setattr(policy, name, getattr(data, name))
        # Checked on the merged result: a PUT that only lowers the monthly cap
        # must not leave a daily cap above it.
        self._validate_daily_within_monthly(policy)
        if "enabled" in fields_set:
            policy.enabled = bool(data.enabled)
        if data.assignments is not None:
            await self._sync_assignments(db, org_id, policy.id, data.assignments)
        if data.connection_overrides is not None:
            await self._sync_connection_overrides(db, org_id, policy.id, data.connection_overrides)
        policy_id = policy.id
        attempted_name = policy.name
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"A usage policy named '{attempted_name}' already exists.",
            )
        db.expire_all()
        return await self.get_policy(db, org_id, policy_id)

    async def delete_policy(self, db: AsyncSession, org_id: str, policy_id: str) -> None:
        policy = await self._get_policy_model(db, org_id, policy_id)
        await db.delete(policy)
        await db.commit()

    async def set_principal_policy(
        self,
        db: AsyncSession,
        org_id: str,
        *,
        principal_type: str,
        principal_id: str,
        policy_id: Optional[str],
    ) -> UsagePolicyPrincipalAssignmentResult:
        if principal_type not in {"user", "group", "role", "membership"}:
            raise HTTPException(status_code=400, detail="principal_type must be user, group, role, or membership")
        # A "membership" principal targets a pending (unregistered) invite; the
        # assignment is rewritten to a "user" principal when they register.
        if principal_type == "membership":
            await self._assert_pending_membership(db, org_id, principal_id)
        if policy_id is not None:
            await self._get_policy_model(db, org_id, policy_id)

        await db.execute(
            delete(UsagePolicyAssignment).where(
                UsagePolicyAssignment.organization_id == org_id,
                UsagePolicyAssignment.principal_type == principal_type,
                UsagePolicyAssignment.principal_id == principal_id,
            )
        )
        assignment = None
        if policy_id is not None:
            assignment = UsagePolicyAssignment(
                organization_id=org_id,
                policy_id=policy_id,
                principal_type=principal_type,
                principal_id=principal_id,
            )
            db.add(assignment)
            await db.flush()

        assignment_schema = UsagePolicyAssignmentSchema.model_validate(assignment) if assignment else None
        await db.commit()
        return UsagePolicyPrincipalAssignmentResult(
            principal_type=principal_type,
            principal_id=principal_id,
            policy_id=policy_id,
            assignment=assignment_schema,
        )

    @staticmethod
    def _validate_daily_within_monthly(source: Any) -> None:
        """A daily cap above the monthly cap of the same metric can never bind
        and almost always means a typo (an extra zero). Reject it. Either side
        may be empty: daily only, monthly only, both, or neither are valid."""
        for daily_name, monthly_name, label in _DAILY_MONTHLY_PAIRS:
            daily = getattr(source, daily_name, None)
            monthly = getattr(source, monthly_name, None)
            if daily is not None and monthly is not None and float(daily) > float(monthly):
                raise HTTPException(
                    status_code=422,
                    detail=f"Daily {label} limit can't be higher than the monthly {label} limit.",
                )

    async def _assert_pending_membership(self, db: AsyncSession, org_id: str, membership_id: str) -> None:
        """Validate the membership is an unregistered invite in this org."""
        result = await db.execute(
            select(Membership).where(
                Membership.id == membership_id,
                Membership.organization_id == org_id,
            )
        )
        membership = result.scalar_one_or_none()
        if not membership:
            raise HTTPException(status_code=404, detail="Membership not found")
        if membership.user_id:
            raise HTTPException(
                status_code=400,
                detail="Membership is already registered; use a user principal instead",
            )

    async def resolve_effective_limits(
        self,
        db: AsyncSession,
        org_id: str,
        user_id: str,
    ) -> EffectiveUsageLimits:
        if not has_feature("usage_limits"):
            return EffectiveUsageLimits(
                enabled=False,
                organization_id=org_id,
                user_id=user_id,
                resolution_source="disabled",
            )

        from app.services.reindex_schedule import get_org_timezone

        tz_name = (await get_org_timezone(db, org_id)).key
        direct = await self._policies_for_principals(db, org_id, [("user", user_id)])
        if direct:
            return self._compose_limits(org_id, user_id, direct, "direct", tz_name)

        group_ids = await self._user_group_ids(db, org_id, user_id)
        role_ids = await self._user_role_ids(db, org_id, user_id, group_ids)
        principals = [("group", group_id) for group_id in group_ids]
        principals.extend(("role", role_id) for role_id in role_ids)
        inherited = await self._policies_for_principals(db, org_id, principals)
        if inherited:
            return self._compose_limits(org_id, user_id, inherited, "inherited", tz_name)

        return EffectiveUsageLimits(
            enabled=True,
            organization_id=org_id,
            user_id=user_id,
            timezone=tz_name,
            resolution_source="default",
        )

    async def get_user_quota_summary(
        self,
        db: AsyncSession,
        org_id: str,
        user_id: str,
    ) -> UsageQuotaSummarySchema:
        window_start, window_end = current_month_window()
        if not has_feature("usage_limits"):
            return UsageQuotaSummarySchema(
                enabled=False,
                organization_id=org_id,
                user_id=user_id,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                resolution_source="disabled",
            )

        limits = await self.resolve_effective_limits(db, org_id, user_id)
        counters = await self._get_current_counters(db, org_id, user_id, window_start)

        token_used = counters.get((METRIC_LLM_TOKENS, SCOPE_ORGANIZATION, ""), 0)
        spend_used_micro = counters.get((METRIC_LLM_COST, SCOPE_ORGANIZATION, ""), 0)
        query_used_by_connection = {
            scope_ref_id: used
            for (metric, scope_type, scope_ref_id), used in counters.items()
            if metric == METRIC_DATA_QUERIES and scope_type == SCOPE_CONNECTION and scope_ref_id
        }
        data_used_by_connection = {
            scope_ref_id: used
            for (metric, scope_type, scope_ref_id), used in counters.items()
            if metric == METRIC_DATA_BYTES and scope_type == SCOPE_CONNECTION and scope_ref_id
        }

        connection_ids = set(query_used_by_connection) | set(data_used_by_connection)
        for overrides in limits.query_overrides_by_policy.values():
            connection_ids.update(overrides.keys())
        for overrides in limits.data_bytes_overrides_by_policy.values():
            connection_ids.update(overrides.keys())

        connection_names = await self._connection_names(db, org_id, connection_ids)
        connections = []
        for connection_id in sorted(connection_ids, key=lambda cid: connection_names.get(cid, cid).lower()):
            if connection_id not in connection_names:
                continue
            query_limit = limits.query_limit_for_connection(connection_id)
            data_limit = limits.data_bytes_limit_for_connection(connection_id)
            query_used = query_used_by_connection.get(connection_id, 0)
            data_used = data_used_by_connection.get(connection_id, 0)
            if not query_used and not data_used and query_limit is None and data_limit is None:
                continue
            connections.append(
                UsageQuotaConnectionSchema(
                    id=connection_id,
                    name=connection_names[connection_id],
                    queries=self._quota_metric(query_used, query_limit),
                    data_bytes=self._quota_metric(data_used, data_limit),
                )
            )

        query_used = sum(query_used_by_connection.values())
        data_used = sum(data_used_by_connection.values())

        day_start, day_end = current_day_window(limits.timezone)
        day_counters = await self._get_current_counters(db, org_id, user_id, day_start)

        def _day_used(metric: str) -> int:
            return day_counters.get((daily_metric(metric), SCOPE_ORGANIZATION, ""), 0)

        daily = UsageQuotaDailySchema(
            window_start=day_start.isoformat(),
            window_end=day_end.isoformat(),
            timezone=limits.timezone,
            tokens=self._quota_metric(_day_used(METRIC_LLM_TOKENS), limits.daily_token_limit),
            queries=self._quota_metric(_day_used(METRIC_DATA_QUERIES), limits.daily_query_limit),
            data_bytes=self._quota_metric(_day_used(METRIC_DATA_BYTES), limits.daily_data_bytes_limit),
            spend=self._spend_metric(_day_used(METRIC_LLM_COST), limits.daily_spend_micro_usd),
        )
        return UsageQuotaSummarySchema(
            enabled=limits.enabled,
            organization_id=org_id,
            user_id=user_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            resolution_source=limits.resolution_source,
            policy_ids=limits.policy_ids,
            tokens=self._quota_metric(token_used, limits.monthly_token_limit),
            queries=self._quota_metric(query_used, limits.monthly_query_limit),
            data_bytes=self._quota_metric(data_used, limits.monthly_data_bytes_limit),
            spend=self._spend_metric(spend_used_micro, limits.monthly_spend_micro_usd),
            connections=connections,
            daily=daily,
        )

    async def get_user_daily_usage(
        self,
        db: AsyncSession,
        org_id: str,
        user_id: str,
    ) -> UsageDailySeriesSchema:
        """Per-day usage since the start of the month, zero-filled through today.

        Aggregated from usage_events (counters are one row per month, so events
        are the only per-day source). Days before recording existed simply show 0.
        """
        window_start, window_end = current_month_window()
        series = UsageDailySeriesSchema(
            enabled=False,
            organization_id=org_id,
            user_id=user_id,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )
        if not has_feature("usage_limits"):
            return series

        day_expr = func.date(UsageEvent.created_at)
        result = await db.execute(
            select(day_expr, UsageEvent.metric, func.sum(UsageEvent.amount))
            .where(
                UsageEvent.organization_id == org_id,
                UsageEvent.user_id == user_id,
                UsageEvent.created_at >= window_start,
                UsageEvent.created_at < window_end,
            )
            .group_by(day_expr, UsageEvent.metric)
        )
        by_day: Dict[str, Dict[str, int]] = {}
        for day, metric, total in result.all():
            by_day.setdefault(str(day), {})[metric] = int(total or 0)

        metric_field = {
            METRIC_LLM_TOKENS: "tokens",
            METRIC_DATA_QUERIES: "queries",
            METRIC_DATA_BYTES: "data_bytes",
        }
        today = datetime.utcnow().date()
        cursor = window_start.date()
        while cursor <= today and cursor < window_end.date():
            key = cursor.isoformat()
            point = UsageDailyPointSchema(date=key)
            for metric, amount in (by_day.get(key) or {}).items():
                if metric == METRIC_LLM_COST:
                    point.spend_usd = micro_to_usd(amount) or 0.0
                elif metric in metric_field:
                    setattr(point, metric_field[metric], amount)
            series.days.append(point)
            cursor += timedelta(days=1)
        series.enabled = True
        return series

    async def check_llm_tokens_available(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        requested_tokens: int,
    ) -> None:
        if not has_feature("usage_limits"):
            return
        if requested_tokens <= 0:
            requested_tokens = 1
        limits = await self.resolve_effective_limits(db, org_id, user_id)
        day_start, _ = current_day_window(limits.timezone)
        for window, metric, limit, window_start in (
            (WINDOW_MONTHLY, METRIC_LLM_TOKENS, limits.monthly_token_limit, None),
            (WINDOW_DAILY, daily_metric(METRIC_LLM_TOKENS), limits.daily_token_limit, day_start),
        ):
            if limit is None:
                continue
            used = await self._get_counter_used(
                db,
                org_id=org_id,
                user_id=user_id,
                metric=metric,
                scope_type=SCOPE_ORGANIZATION,
                scope_ref_id="",
                window_start=window_start,
            )
            if used + requested_tokens > limit:
                raise build_limit_exceeded(
                    METRIC_LLM_TOKENS, window,
                    limit=limit, used=used, requested=requested_tokens,
                    tz_name=limits.timezone,
                )

    async def record_llm_tokens(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        amount: int,
        source: Optional[str] = None,
        source_ref_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        if not has_feature("usage_limits") or amount <= 0:
            return
        limits = await self.resolve_effective_limits(db, org_id, user_id)
        await self._increment_counter(
            db,
            org_id=org_id,
            user_id=user_id,
            metric=METRIC_LLM_TOKENS,
            scope_type=SCOPE_ORGANIZATION,
            scope_ref_id="",
            amount=amount,
            limit=limits.monthly_token_limit,
            enforce_limit=False,
        )
        await self._increment_daily(db, org_id, user_id, METRIC_LLM_TOKENS, amount, limits.timezone)
        self._add_event(
            db,
            org_id=org_id,
            user_id=user_id,
            policy_id=limits.policy_ids[0] if limits.policy_ids else None,
            metric=METRIC_LLM_TOKENS,
            amount=amount,
            scope_type=SCOPE_ORGANIZATION,
            scope_ref_id="",
            source=source,
            source_ref_id=source_ref_id,
            metadata=metadata,
        )

    async def record_llm_cost(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        amount_micro_usd: int,
        source: Optional[str] = None,
        source_ref_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Record LLM spend (in micro-USD) against the monthly spend counter.

        Recorded whenever the feature is licensed, cap or no cap — the per-user
        counters back the profile Usage tab, and always-on recording keeps a
        cap accurate when an admin introduces one mid-month.
        """
        if not has_feature("usage_limits") or amount_micro_usd <= 0:
            return
        limits = await self.resolve_effective_limits(db, org_id, user_id)
        await self._increment_counter(
            db,
            org_id=org_id,
            user_id=user_id,
            metric=METRIC_LLM_COST,
            scope_type=SCOPE_ORGANIZATION,
            scope_ref_id="",
            amount=amount_micro_usd,
            limit=limits.monthly_spend_micro_usd,
            enforce_limit=False,
        )
        await self._increment_daily(db, org_id, user_id, METRIC_LLM_COST, amount_micro_usd, limits.timezone)
        self._add_event(
            db,
            org_id=org_id,
            user_id=user_id,
            policy_id=limits.policy_ids[0] if limits.policy_ids else None,
            metric=METRIC_LLM_COST,
            amount=amount_micro_usd,
            scope_type=SCOPE_ORGANIZATION,
            scope_ref_id="",
            source=source,
            source_ref_id=source_ref_id,
            metadata=metadata,
        )

    async def consume_data_bytes(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        connection_id: str,
        amount: int,
        source: Optional[str] = None,
        source_ref_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        if not has_feature("usage_limits") or amount <= 0:
            return
        limits = await self.resolve_effective_limits(db, org_id, user_id)
        data_bytes_limit = limits.data_bytes_limit_for_connection(connection_id)
        await self._increment_counter(
            db,
            org_id=org_id,
            user_id=user_id,
            metric=METRIC_DATA_BYTES,
            scope_type=SCOPE_CONNECTION,
            scope_ref_id=connection_id or "",
            amount=amount,
            limit=data_bytes_limit,
        )
        await self._increment_daily(
            db, org_id, user_id, METRIC_DATA_BYTES, amount, limits.timezone,
            limit=limits.daily_data_bytes_limit, enforce_limit=True,
        )
        self._add_event(
            db,
            org_id=org_id,
            user_id=user_id,
            policy_id=limits.policy_ids[0] if limits.policy_ids else None,
            metric=METRIC_DATA_BYTES,
            amount=amount,
            scope_type=SCOPE_CONNECTION,
            scope_ref_id=connection_id or "",
            source=source,
            source_ref_id=source_ref_id,
            metadata=metadata,
        )

    async def consume_data_query(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        connection_id: str,
        source: Optional[str] = None,
        source_ref_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        if not has_feature("usage_limits"):
            return
        limits = await self.resolve_effective_limits(db, org_id, user_id)
        query_limit = limits.query_limit_for_connection(connection_id)
        await self._increment_counter(
            db,
            org_id=org_id,
            user_id=user_id,
            metric=METRIC_DATA_QUERIES,
            scope_type=SCOPE_CONNECTION,
            scope_ref_id=connection_id or "",
            amount=1,
            limit=query_limit,
        )
        await self._increment_daily(
            db, org_id, user_id, METRIC_DATA_QUERIES, 1, limits.timezone,
            limit=limits.daily_query_limit, enforce_limit=True,
        )
        self._add_event(
            db,
            org_id=org_id,
            user_id=user_id,
            policy_id=limits.policy_ids[0] if limits.policy_ids else None,
            metric=METRIC_DATA_QUERIES,
            amount=1,
            scope_type=SCOPE_CONNECTION,
            scope_ref_id=connection_id or "",
            source=source,
            source_ref_id=source_ref_id,
            metadata=metadata,
        )

    async def check_llm_tokens_with_context(self, context: UsageLimitContext, requested_tokens: int) -> None:
        if not context.session_maker:
            return
        async with context.session_maker() as db:
            await self.check_llm_tokens_available(
                db,
                org_id=context.organization_id,
                user_id=context.user_id,
                requested_tokens=requested_tokens,
            )

    async def record_llm_tokens_with_context(
        self,
        context: UsageLimitContext,
        amount: int,
        metadata: Optional[dict] = None,
    ) -> None:
        if not context.session_maker:
            return
        async with context.session_maker() as db:
            await self.record_llm_tokens(
                db,
                org_id=context.organization_id,
                user_id=context.user_id,
                amount=amount,
                source=context.source,
                source_ref_id=context.source_ref_id,
                metadata=metadata,
            )
            await db.commit()

    async def record_llm_cost_with_context(
        self,
        context: UsageLimitContext,
        amount_micro_usd: int,
        metadata: Optional[dict] = None,
    ) -> None:
        if not context.session_maker:
            return
        async with context.session_maker() as db:
            await self.record_llm_cost(
                db,
                org_id=context.organization_id,
                user_id=context.user_id,
                amount_micro_usd=amount_micro_usd,
                source=context.source,
                source_ref_id=context.source_ref_id,
                metadata=metadata,
            )
            await db.commit()

    async def consume_data_query_with_context(
        self,
        context: UsageLimitContext,
        *,
        connection_id: str,
        metadata: Optional[dict] = None,
    ) -> None:
        if not context.session_maker:
            return
        # Same gate as UsageLimitContext.add_tokens: with the feature off
        # there is nothing to enforce, and this write is pure overhead —
        # on SQLite it can burn the whole 30s busy_timeout PER QUERY when
        # another writer holds the lock during code execution.
        if not has_feature("usage_limits"):
            return
        async with context.session_maker() as db:
            await self.consume_data_query(
                db,
                org_id=context.organization_id,
                user_id=context.user_id,
                connection_id=connection_id,
                source=context.source,
                source_ref_id=context.source_ref_id,
                metadata=metadata,
            )
            await db.commit()

    async def record_data_usage_with_context(
        self,
        context: UsageLimitContext,
        events: List[dict],
    ) -> None:
        """Persist buffered data-plane usage events (queries / bytes) in one
        session+commit. Counters are aggregated per (metric, connection);
        enforcement never happens here — it already ran at check time, and a
        bookkeeping flush must not raise quota errors."""
        if not context.session_maker or not events:
            return
        async with context.session_maker() as db:
            await self.record_data_usage(
                db,
                org_id=context.organization_id,
                user_id=context.user_id,
                events=events,
            )
            await db.commit()

    async def record_data_usage(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        events: List[dict],
    ) -> None:
        if not has_feature("usage_limits") or not events:
            return
        limits = await self.resolve_effective_limits(db, org_id, user_id)
        policy_id = limits.policy_ids[0] if limits.policy_ids else None
        totals: Dict[tuple, int] = {}
        for event in events:
            key = (event["metric"], event.get("connection_id") or "")
            totals[key] = totals.get(key, 0) + int(event.get("amount") or 0)
        for (metric, connection_id), amount in totals.items():
            await self._increment_counter(
                db,
                org_id=org_id,
                user_id=user_id,
                metric=metric,
                scope_type=SCOPE_CONNECTION,
                scope_ref_id=connection_id,
                amount=amount,
                limit=None,
                enforce_limit=False,
            )
        daily_totals: Dict[str, int] = {}
        for (metric, _connection_id), amount in totals.items():
            daily_totals[metric] = daily_totals.get(metric, 0) + amount
        for metric, amount in daily_totals.items():
            await self._increment_daily(db, org_id, user_id, metric, amount, limits.timezone)
        for event in events:
            self._add_event(
                db,
                org_id=org_id,
                user_id=user_id,
                policy_id=policy_id,
                metric=event["metric"],
                amount=int(event.get("amount") or 0),
                scope_type=SCOPE_CONNECTION,
                scope_ref_id=event.get("connection_id") or "",
                source=event.get("source"),
                source_ref_id=event.get("source_ref_id"),
                metadata=event.get("metadata"),
            )

    async def consume_data_bytes_with_context(
        self,
        context: UsageLimitContext,
        *,
        connection_id: str,
        amount: int,
        metadata: Optional[dict] = None,
    ) -> None:
        if not context.session_maker:
            return
        if not has_feature("usage_limits"):
            return
        async with context.session_maker() as db:
            await self.consume_data_bytes(
                db,
                org_id=context.organization_id,
                user_id=context.user_id,
                connection_id=connection_id,
                amount=amount,
                source=context.source,
                source_ref_id=context.source_ref_id,
                metadata=metadata,
            )
            await db.commit()

    def _compose_limits(
        self,
        org_id: str,
        user_id: str,
        policies: List[UsagePolicy],
        source: str,
        tz_name: str = "UTC",
    ) -> EffectiveUsageLimits:
        token_limit = self._most_restrictive([p.monthly_token_limit for p in policies])
        query_limit = self._most_restrictive([p.monthly_query_limit for p in policies])
        data_bytes_limit = self._most_restrictive([p.monthly_data_bytes_limit for p in policies])
        spend_micro_limit = self._most_restrictive(
            [usd_to_micro(p.monthly_spend_limit_usd) for p in policies]
        )
        query_overrides: Dict[str, Dict[str, Optional[int]]] = {}
        data_bytes_overrides: Dict[str, Dict[str, Optional[int]]] = {}
        for policy in policies:
            query_overrides[policy.id] = {
                override.connection_id: override.monthly_query_limit
                for override in (policy.connection_overrides or [])
            }
            data_bytes_overrides[policy.id] = {
                override.connection_id: override.monthly_data_bytes_limit
                for override in (policy.connection_overrides or [])
            }
        return EffectiveUsageLimits(
            enabled=True,
            organization_id=org_id,
            user_id=user_id,
            monthly_token_limit=token_limit,
            monthly_query_limit=query_limit,
            monthly_data_bytes_limit=data_bytes_limit,
            monthly_spend_micro_usd=spend_micro_limit,
            daily_token_limit=self._most_restrictive([p.daily_token_limit for p in policies]),
            daily_query_limit=self._most_restrictive([p.daily_query_limit for p in policies]),
            daily_data_bytes_limit=self._most_restrictive([p.daily_data_bytes_limit for p in policies]),
            daily_spend_micro_usd=self._most_restrictive(
                [usd_to_micro(p.daily_spend_limit_usd) for p in policies]
            ),
            timezone=tz_name,
            policy_ids=[p.id for p in policies],
            resolution_source=source,
            query_base_by_policy={p.id: p.monthly_query_limit for p in policies},
            query_overrides_by_policy=query_overrides,
            data_bytes_base_by_policy={p.id: p.monthly_data_bytes_limit for p in policies},
            data_bytes_overrides_by_policy=data_bytes_overrides,
        )

    @staticmethod
    def _most_restrictive(values: Iterable[Optional[int]]) -> Optional[int]:
        finite = [value for value in values if value is not None]
        if not finite:
            return None
        return min(finite)

    async def _get_policy_model(self, db: AsyncSession, org_id: str, policy_id: str) -> UsagePolicy:
        result = await db.execute(
            select(UsagePolicy)
            .options(
                selectinload(UsagePolicy.assignments),
                selectinload(UsagePolicy.connection_overrides),
            )
            .where(
                UsagePolicy.id == policy_id,
                UsagePolicy.organization_id == org_id,
                UsagePolicy.deleted_at.is_(None),
            )
        )
        policy = result.scalar_one_or_none()
        if not policy:
            raise HTTPException(status_code=404, detail="Usage policy not found")
        return policy

    async def _sync_assignments(self, db: AsyncSession, org_id: str, policy_id: str, assignments) -> None:
        await db.execute(delete(UsagePolicyAssignment).where(UsagePolicyAssignment.policy_id == policy_id))
        for assignment in assignments or []:
            await db.execute(
                delete(UsagePolicyAssignment).where(
                    UsagePolicyAssignment.organization_id == org_id,
                    UsagePolicyAssignment.principal_type == assignment.principal_type,
                    UsagePolicyAssignment.principal_id == assignment.principal_id,
                )
            )
            db.add(
                UsagePolicyAssignment(
                    organization_id=org_id,
                    policy_id=policy_id,
                    principal_type=assignment.principal_type,
                    principal_id=assignment.principal_id,
                )
            )
        await db.flush()

    async def _sync_connection_overrides(
        self,
        db: AsyncSession,
        org_id: str,
        policy_id: str,
        overrides: List[UsagePolicyConnectionOverrideInput],
    ) -> None:
        # Validate before writing: a duplicate or unknown/foreign connection id
        # used to fail at flush time (unique/FK violation) as a 500.
        requested = [str(o.connection_id) for o in (overrides or [])]
        if len(set(requested)) != len(requested):
            raise HTTPException(status_code=422, detail="Each connection can only have one override")
        if requested:
            found = set((await db.execute(
                select(Connection.id).where(
                    Connection.id.in_(requested),
                    Connection.organization_id == org_id,
                )
            )).scalars().all())
            if found != set(requested):
                raise HTTPException(status_code=422, detail="Unknown connection in connection_overrides")
        await db.execute(delete(UsagePolicyConnectionOverride).where(UsagePolicyConnectionOverride.policy_id == policy_id))
        for override in overrides or []:
            db.add(
                UsagePolicyConnectionOverride(
                    organization_id=org_id,
                    policy_id=policy_id,
                    connection_id=override.connection_id,
                    monthly_query_limit=override.monthly_query_limit,
                    monthly_data_bytes_limit=override.monthly_data_bytes_limit,
                )
            )
        await db.flush()

    async def _policies_for_principals(
        self,
        db: AsyncSession,
        org_id: str,
        principals: List[tuple[str, str]],
    ) -> List[UsagePolicy]:
        if not principals:
            return []
        conditions = [
            (UsagePolicyAssignment.principal_type == principal_type)
            & (UsagePolicyAssignment.principal_id == principal_id)
            for principal_type, principal_id in principals
        ]
        result = await db.execute(
            select(UsagePolicy)
            .join(UsagePolicyAssignment, UsagePolicyAssignment.policy_id == UsagePolicy.id)
            .options(selectinload(UsagePolicy.connection_overrides))
            .where(
                UsagePolicy.organization_id == org_id,
                UsagePolicy.enabled.is_(True),
                UsagePolicy.deleted_at.is_(None),
                UsagePolicyAssignment.deleted_at.is_(None),
                or_(*conditions),
            )
            .distinct()
        )
        return list(result.scalars().all())

    async def _user_group_ids(self, db: AsyncSession, org_id: str, user_id: str) -> List[str]:
        result = await db.execute(
            select(GroupMembership.group_id)
            .join(Group, Group.id == GroupMembership.group_id)
            .where(
                Group.organization_id == org_id,
                GroupMembership.user_id == user_id,
                GroupMembership.deleted_at.is_(None),
                Group.deleted_at.is_(None),
            )
        )
        return [row[0] for row in result.all()]

    async def _user_role_ids(
        self,
        db: AsyncSession,
        org_id: str,
        user_id: str,
        group_ids: List[str],
    ) -> List[str]:
        conditions = [
            (RoleAssignment.principal_type == "user") & (RoleAssignment.principal_id == user_id)
        ]
        if group_ids:
            conditions.append(
                (RoleAssignment.principal_type == "group") & (RoleAssignment.principal_id.in_(group_ids))
            )
        result = await db.execute(
            select(RoleAssignment.role_id)
            .where(
                RoleAssignment.organization_id == org_id,
                RoleAssignment.deleted_at.is_(None),
                or_(*conditions),
            )
            .distinct()
        )
        return [row[0] for row in result.all()]

    async def _get_counter(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        metric: str,
        scope_type: str,
        scope_ref_id: str,
    ) -> UsageCounter:
        window_start, window_end = current_month_window()
        stmt = (
            select(UsageCounter)
            .where(
                UsageCounter.organization_id == org_id,
                UsageCounter.user_id == user_id,
                UsageCounter.metric == metric,
                UsageCounter.scope_type == scope_type,
                UsageCounter.scope_ref_id == (scope_ref_id or ""),
                UsageCounter.window_start == window_start,
            )
            .with_for_update()
        )
        result = await db.execute(stmt)
        counter = result.scalar_one_or_none()
        if counter:
            return counter

        counter = UsageCounter(
            organization_id=org_id,
            user_id=user_id,
            metric=metric,
            scope_type=scope_type,
            scope_ref_id=scope_ref_id or "",
            window_start=window_start,
            window_end=window_end,
            used=0,
        )
        db.add(counter)
        try:
            await db.flush()
            return counter
        except IntegrityError:
            await db.rollback()
            result = await db.execute(stmt)
            counter = result.scalar_one()
            return counter

    async def _get_counter_used(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        metric: str,
        scope_type: str,
        scope_ref_id: str,
        window_start: Optional[datetime] = None,
    ) -> int:
        if window_start is None:
            window_start, _ = current_month_window()
        result = await db.execute(
            select(UsageCounter.used).where(
                UsageCounter.organization_id == org_id,
                UsageCounter.user_id == user_id,
                UsageCounter.metric == metric,
                UsageCounter.scope_type == scope_type,
                UsageCounter.scope_ref_id == (scope_ref_id or ""),
                UsageCounter.window_start == window_start,
            )
        )
        return int(result.scalar_one_or_none() or 0)

    async def _get_current_counters(
        self,
        db: AsyncSession,
        org_id: str,
        user_id: str,
        window_start: datetime,
    ) -> Dict[tuple[str, str, str], int]:
        result = await db.execute(
            select(UsageCounter).where(
                UsageCounter.organization_id == org_id,
                UsageCounter.user_id == user_id,
                UsageCounter.window_start == window_start,
            )
        )
        return {
            (counter.metric, counter.scope_type, counter.scope_ref_id or ""): int(counter.used or 0)
            for counter in result.scalars().all()
        }

    async def _connection_names(
        self,
        db: AsyncSession,
        org_id: str,
        connection_ids: Iterable[str],
    ) -> Dict[str, str]:
        ids = [connection_id for connection_id in connection_ids if connection_id]
        if not ids:
            return {}
        result = await db.execute(
            select(Connection.id, Connection.name).where(
                Connection.organization_id == org_id,
                Connection.id.in_(ids),
                Connection.deleted_at.is_(None),
            )
        )
        return {str(connection_id): name for connection_id, name in result.all()}

    @staticmethod
    def _quota_metric(used: int, limit: Optional[int]) -> UsageQuotaMetricSchema:
        remaining = None
        percent = None
        if limit is not None:
            remaining = max(int(limit) - int(used or 0), 0)
            if limit > 0:
                percent = round((int(used or 0) / limit) * 100, 2)
            else:
                percent = 100.0 if used else 0.0
        return UsageQuotaMetricSchema(
            used=int(used or 0),
            limit=limit,
            remaining=remaining,
            percent=percent,
        )

    @staticmethod
    def _spend_metric(used_micro: int, limit_micro: Optional[int]) -> UsageQuotaSpendMetricSchema:
        used_micro = int(used_micro or 0)
        remaining = None
        percent = None
        if limit_micro is not None:
            remaining = micro_to_usd(max(int(limit_micro) - used_micro, 0))
            if limit_micro > 0:
                percent = round((used_micro / limit_micro) * 100, 2)
            else:
                percent = 100.0 if used_micro else 0.0
        return UsageQuotaSpendMetricSchema(
            used=micro_to_usd(used_micro) or 0.0,
            limit=micro_to_usd(limit_micro),
            remaining=remaining,
            percent=percent,
        )

    async def _increment_counter(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        metric: str,
        scope_type: str,
        scope_ref_id: str,
        amount: int,
        limit: Optional[int],
        enforce_limit: bool = True,
        window: Optional[tuple[datetime, datetime]] = None,
        window_kind: str = WINDOW_MONTHLY,
        tz_name: Optional[str] = None,
        limit_metric: Optional[str] = None,
    ) -> None:
        """Atomically add ``amount`` to the counter. No SELECT FOR UPDATE.

        Previously this acquired a row-level lock via `with_for_update()`,
        held it across the limit check + the in-Python increment + the
        flush. Every LLM call took that lock, so two requests for the
        same (org, user) serialized end-to-end and stacked
        `idle in transaction` connections under load.

        New shape:
          1. issue a single ``UPDATE ... SET used = used + :amount``
             — Postgres handles the row lock for the duration of the
             statement only, then releases.
          2. if zero rows were updated the counter row doesn't exist
             yet, so insert it; on a race the UNIQUE constraint trips
             and we retry the UPDATE once.
          3. enforcement uses a follow-up SELECT (only when callers
             pass ``enforce_limit=True`` AND a finite limit). Slight
             over-shoot under heavy contention is acceptable for a
             monthly quota and far cheaper than the lock.
        """
        window_start, window_end = window or current_month_window()
        scope_ref_id = scope_ref_id or ""
        amount = int(amount)
        if amount <= 0:
            return

        update_stmt = (
            update(UsageCounter)
            .where(
                UsageCounter.organization_id == org_id,
                UsageCounter.user_id == user_id,
                UsageCounter.metric == metric,
                UsageCounter.scope_type == scope_type,
                UsageCounter.scope_ref_id == scope_ref_id,
                UsageCounter.window_start == window_start,
            )
            .values(used=UsageCounter.used + amount)
        )

        result = await db.execute(update_stmt)
        if result.rowcount == 0:
            # Counter doesn't exist yet — insert. On race another tx wins
            # the unique constraint; retry the UPDATE once.
            counter = UsageCounter(
                organization_id=org_id,
                user_id=user_id,
                metric=metric,
                scope_type=scope_type,
                scope_ref_id=scope_ref_id,
                window_start=window_start,
                window_end=window_end,
                used=amount,
            )
            db.add(counter)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                await db.execute(update_stmt)

        if enforce_limit and limit is not None:
            used_now = await self._get_counter_used(
                db,
                org_id=org_id,
                user_id=user_id,
                metric=metric,
                scope_type=scope_type,
                scope_ref_id=scope_ref_id,
                window_start=window_start,
            )
            if used_now > limit:
                raise build_limit_exceeded(
                    limit_metric or metric,
                    window_kind,
                    limit=limit,
                    used=used_now - amount,
                    requested=amount,
                    tz_name=tz_name,
                )

    async def _increment_daily(
        self,
        db: AsyncSession,
        org_id: str,
        user_id: str,
        metric: str,
        amount: int,
        tz_name: str,
        *,
        limit: Optional[int] = None,
        enforce_limit: bool = False,
    ) -> None:
        """Add to the user's counter for today (org timezone), counted across
        all connections. Recorded whether or not a daily cap is set, so a cap
        an admin adds mid-day already sees the day's real usage."""
        await self._increment_counter(
            db,
            org_id=org_id,
            user_id=user_id,
            metric=daily_metric(metric),
            scope_type=SCOPE_ORGANIZATION,
            scope_ref_id="",
            amount=amount,
            limit=limit,
            enforce_limit=enforce_limit,
            window=current_day_window(tz_name),
            window_kind=WINDOW_DAILY,
            tz_name=tz_name,
            limit_metric=metric,
        )

    def _add_event(
        self,
        db: AsyncSession,
        *,
        org_id: str,
        user_id: str,
        policy_id: Optional[str],
        metric: str,
        amount: int,
        scope_type: str,
        scope_ref_id: str,
        source: Optional[str],
        source_ref_id: Optional[str],
        metadata: Optional[dict],
    ) -> None:
        db.add(
            UsageEvent(
                organization_id=org_id,
                user_id=user_id,
                policy_id=policy_id,
                metric=metric,
                amount=amount,
                scope_type=scope_type,
                scope_ref_id=scope_ref_id or "",
                source=source,
                source_ref_id=source_ref_id,
                usage_metadata=metadata,
            )
        )

    def _policy_to_schema(self, policy: UsagePolicy) -> UsagePolicySchema:
        return UsagePolicySchema.model_validate(policy)


def current_month_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now = now or datetime.utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def current_day_window(
    tz: Union[str, ZoneInfo, None] = None,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Today's window in `tz`, midnight to the next midnight, returned as naive
    UTC datetimes (the convention every usage_counters column uses). On a DST
    change day the window is 23 or 25 hours long, as the local day is."""
    if tz is None or isinstance(tz, str):
        from app.services.reindex_schedule import resolve_timezone

        tz = resolve_timezone(tz)
    now = now or datetime.utcnow()
    local = now.replace(tzinfo=dt_timezone.utc).astimezone(tz)
    next_day = local.date() + timedelta(days=1)
    start_local = datetime(local.year, local.month, local.day, tzinfo=tz)
    end_local = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)

    def _to_naive_utc(value: datetime) -> datetime:
        return value.astimezone(dt_timezone.utc).replace(tzinfo=None)

    return _to_naive_utc(start_local), _to_naive_utc(end_local)


_DAILY_LIMIT_FIELDS = (
    "daily_token_limit",
    "daily_query_limit",
    "daily_data_bytes_limit",
    "daily_spend_limit_usd",
)

_DAILY_MONTHLY_PAIRS = (
    ("daily_token_limit", "monthly_token_limit", "token"),
    ("daily_query_limit", "monthly_query_limit", "query"),
    ("daily_data_bytes_limit", "monthly_data_bytes_limit", "data"),
    ("daily_spend_limit_usd", "monthly_spend_limit_usd", "spend"),
)

usage_policy_service = UsagePolicyService()
