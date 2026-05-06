import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ee.license import has_feature
from app.models.connection import Connection
from app.models.group import Group
from app.models.group_membership import GroupMembership
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
    UsagePolicyAssignmentSchema,
    UsagePolicyConnectionOverrideInput,
    UsagePolicyCreate,
    UsagePolicyPrincipalAssignmentResult,
    UsagePolicySchema,
    UsagePolicyUpdate,
    UsageQuotaConnectionSchema,
    UsageQuotaMetricSchema,
    UsageQuotaSummarySchema,
)


METRIC_LLM_TOKENS = "llm_tokens"
METRIC_DATA_QUERIES = "data_queries"
METRIC_DATA_BYTES = "data_bytes"
SCOPE_ORGANIZATION = "organization"
SCOPE_CONNECTION = "connection"


class UsageLimitExceeded(Exception):
    def __init__(self, detail: str, *, metric: str, limit: int, used: int, requested: int):
        super().__init__(detail)
        self.detail = detail
        self.metric = metric
        self.limit = limit
        self.used = used
        self.requested = requested


@dataclass
class EffectiveUsageLimits:
    enabled: bool
    organization_id: str
    user_id: str
    monthly_token_limit: Optional[int] = None
    monthly_query_limit: Optional[int] = None
    monthly_data_bytes_limit: Optional[int] = None
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
    _last_metadata: Optional[dict] = field(default=None, init=False, repr=False)
    # Cached limit + counter snapshot. `_cache_loaded_at` < 0 means
    # "never loaded"; the next check_tokens() will load it. Refresh
    # cadence is the TTL below; we additionally force-refresh when the
    # uncached headroom is about to be exceeded by pending+requested.
    _cached_limit: Optional[int] = field(default=None, init=False, repr=False)
    _cached_used: int = field(default=0, init=False, repr=False)
    _cache_loaded_at: float = field(default=-1.0, init=False, repr=False)
    _CACHE_TTL_SECONDS: float = field(default=30.0, init=False, repr=False)
    _cache_lock: Optional[asyncio.Lock] = field(default=None, init=False, repr=False)

    def for_source(self, source: str, source_ref_id: Optional[str] = None) -> "UsageLimitContext":
        return UsageLimitContext(
            organization_id=self.organization_id,
            user_id=self.user_id,
            source=source,
            source_ref_id=source_ref_id or self.source_ref_id,
            session_maker=self.session_maker,
            loop=self.loop,
        )

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
        self._pending_tokens += int(amount)
        if metadata is not None:
            self._last_metadata = metadata

    @property
    def pending_tokens(self) -> int:
        return self._pending_tokens

    async def _refresh_quota_cache(self) -> None:
        """Load `_cached_limit` and `_cached_used` from the DB. ~1 SELECT each."""
        if self.session_maker is None:
            return
        async with self.session_maker() as db:
            limits = await usage_policy_service.resolve_effective_limits(
                db, self.organization_id, self.user_id
            )
            self._cached_limit = limits.monthly_token_limit
            if limits.monthly_token_limit is not None:
                self._cached_used = await usage_policy_service._get_counter_used(
                    db,
                    org_id=self.organization_id,
                    user_id=self.user_id,
                    metric=METRIC_LLM_TOKENS,
                    scope_type=SCOPE_ORGANIZATION,
                    scope_ref_id="",
                )
            else:
                self._cached_used = 0
        import time as _time
        self._cache_loaded_at = _time.monotonic()

    async def check_tokens(self, requested_tokens: int) -> None:
        """Cheap pre-LLM-call quota check. Hits the DB at most once per
        TTL window; otherwise compares against the cached limit/used.

        Raises UsageLimitExceeded if the call would push us over.
        """
        if not has_feature("usage_limits") or self.session_maker is None:
            return
        if requested_tokens <= 0:
            requested_tokens = 1

        import time as _time
        now = _time.monotonic()
        # Need a refresh? First call ever, or TTL expired.
        if self._cache_loaded_at < 0 or (now - self._cache_loaded_at) >= self._CACHE_TTL_SECONDS:
            # Serialize concurrent refreshers — only one DB roundtrip even
            # when N tasks race here at the same agent run's first call.
            if self._cache_lock is None:
                self._cache_lock = asyncio.Lock()
            async with self._cache_lock:
                if self._cache_loaded_at < 0 or (now - self._cache_loaded_at) >= self._CACHE_TTL_SECONDS:
                    await self._refresh_quota_cache()

        if self._cached_limit is None:
            return  # no quota configured, nothing to check

        projected = self._cached_used + self._pending_tokens + int(requested_tokens)
        if projected <= self._cached_limit:
            return

        # We *might* be over — but our cache could be stale low. Force a
        # refresh and re-check before raising.
        if self._cache_lock is None:
            self._cache_lock = asyncio.Lock()
        async with self._cache_lock:
            await self._refresh_quota_cache()
        if self._cached_limit is None:
            return
        projected = self._cached_used + self._pending_tokens + int(requested_tokens)
        if projected > self._cached_limit:
            raise UsageLimitExceeded(
                "Monthly LLM token quota exceeded.",
                metric=METRIC_LLM_TOKENS,
                limit=self._cached_limit,
                used=self._cached_used + self._pending_tokens,
                requested=int(requested_tokens),
            )

    async def flush(self) -> None:
        """Persist any buffered tokens. Called by the agent at end of run."""
        if self._pending_tokens <= 0 or self.session_maker is None:
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
        except Exception:
            # Best-effort. Re-credit so a future flush retries.
            self._pending_tokens += amount
            if meta is not None:
                self._last_metadata = meta
            raise

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
        policy = UsagePolicy(
            organization_id=org_id,
            name=data.name,
            description=data.description,
            monthly_token_limit=data.monthly_token_limit,
            monthly_query_limit=data.monthly_query_limit,
            monthly_data_bytes_limit=data.monthly_data_bytes_limit,
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
        if principal_type not in {"user", "group", "role"}:
            raise HTTPException(status_code=400, detail="principal_type must be user, group, or role")
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

        direct = await self._policies_for_principals(db, org_id, [("user", user_id)])
        if direct:
            return self._compose_limits(org_id, user_id, direct, "direct")

        group_ids = await self._user_group_ids(db, org_id, user_id)
        role_ids = await self._user_role_ids(db, org_id, user_id, group_ids)
        principals = [("group", group_id) for group_id in group_ids]
        principals.extend(("role", role_id) for role_id in role_ids)
        inherited = await self._policies_for_principals(db, org_id, principals)
        if inherited:
            return self._compose_limits(org_id, user_id, inherited, "inherited")

        return EffectiveUsageLimits(
            enabled=True,
            organization_id=org_id,
            user_id=user_id,
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
            connections=connections,
        )

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
        if limits.monthly_token_limit is None:
            return
        used = await self._get_counter_used(
            db,
            org_id=org_id,
            user_id=user_id,
            metric=METRIC_LLM_TOKENS,
            scope_type=SCOPE_ORGANIZATION,
            scope_ref_id="",
        )
        if used + requested_tokens > limits.monthly_token_limit:
            raise UsageLimitExceeded(
                "Monthly LLM token quota exceeded.",
                metric=METRIC_LLM_TOKENS,
                limit=limits.monthly_token_limit,
                used=used,
                requested=requested_tokens,
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
        if limits.monthly_token_limit is None:
            return
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
        if data_bytes_limit is None:
            return
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
        if query_limit is None:
            return
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

    async def consume_data_query_with_context(
        self,
        context: UsageLimitContext,
        *,
        connection_id: str,
        metadata: Optional[dict] = None,
    ) -> None:
        if not context.session_maker:
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
    ) -> EffectiveUsageLimits:
        token_limit = self._most_restrictive([p.monthly_token_limit for p in policies])
        query_limit = self._most_restrictive([p.monthly_query_limit for p in policies])
        data_bytes_limit = self._most_restrictive([p.monthly_data_bytes_limit for p in policies])
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
    ) -> int:
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
        window_start, window_end = current_month_window()
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
            )
            if used_now > limit:
                raise UsageLimitExceeded(
                    "Monthly usage quota exceeded.",
                    metric=metric,
                    limit=limit,
                    used=used_now - amount,
                    requested=amount,
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


usage_policy_service = UsagePolicyService()
