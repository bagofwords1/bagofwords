"""LLM fallback — availability-driven model substitution (Enterprise).

When the effective model fails with an availability-class error (rate limit,
provider overload, network), the agent walks the org's admin-configured
fallback order and swaps to the first eligible candidate for the rest of the
run. This is orthogonal to the Auto model router (quality/cost, planner-invoked
via the route_model tool): routing decides the *desired* model, fallback decides
who *actually serves* when the desired one can't.

Key pieces:
  - ``FALLBACK_ELIGIBLE_CODES`` — which classified error codes may trigger a
    substitution (see app.ai.llm.errors.classify).
  - ``CircuitBreaker`` — in-process failure tracker. Trips at *model* scope for
    rate_limit/provider_error (per-model quotas and capacity) and at *provider*
    scope for network/auth (endpoint-wide outages), so a Claude Opus 429 never
    blocks Claude Haiku, while an unreachable endpoint blocks every model on it.
  - ``resolve_fallback_chain`` — loads the org's ordered candidate list
    (org settings key ``llm_fallback_order``) as live LLMModel rows.
  - ``FallbackController`` — per-run walker over the chain: skips the failing
    model, already-attempted models, and breaker-open entries.

Storage: the toggle is the ``llm_fallback`` org setting (EE-gated), the order
is ``llm_fallback_order`` (list of LLMModel db ids), managed via
POST /llm/fallback_order.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_model import LLMModel
from app.models.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

# Classified error codes (app.ai.llm.errors.ERROR_CODES) that justify serving
# the request from a different model. auth/context_length/unknown never do:
# auth won't heal by switching (and often means misconfiguration the admin must
# see), context_length follows the conversation to any model of similar window,
# and unknown could be anything — surfacing it is safer than masking it.
FALLBACK_ELIGIBLE_CODES = ("rate_limit", "provider_error", "network")

# Codes that indicate the whole provider endpoint is unhealthy (breaker trips
# at provider scope). Everything else eligible trips at model scope.
PROVIDER_SCOPE_CODES = ("network", "auth")

# Bound the chain walk; the admin UI shouldn't produce lists this long anyway.
MAX_FALLBACK_CHAIN = 10


class CircuitBreaker:
    """In-process, per-key failure window with cooldown.

    A key is ``model:<id>`` or ``provider:<id>`` depending on the error class.
    ``threshold`` failures within ``window_s`` opens the breaker for
    ``cooldown_s``. In-memory only: approximate under multi-worker deployments,
    which is acceptable — the worst case is one extra doomed attempt per worker.
    """

    def __init__(self, threshold: int = 2, window_s: float = 120.0, cooldown_s: float = 60.0):
        self.threshold = threshold
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._failures: Dict[str, deque] = {}
        self._open_until: Dict[str, float] = {}

    @staticmethod
    def _keys_for(provider_id: str, model_id: str, code: str) -> List[str]:
        if code in PROVIDER_SCOPE_CODES:
            return [f"provider:{provider_id}"]
        return [f"model:{model_id}"]

    def record_failure(self, provider_id: str, model_id: str, code: str) -> None:
        now = time.monotonic()
        for key in self._keys_for(str(provider_id), str(model_id), code):
            q = self._failures.setdefault(key, deque())
            q.append(now)
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.threshold:
                self._open_until[key] = now + self.cooldown_s
                logger.info("[fallback] circuit open: %s (cooldown %ss)", key, self.cooldown_s)

    def is_open(self, provider_id: str, model_id: str) -> bool:
        now = time.monotonic()
        for key in (f"provider:{provider_id}", f"model:{model_id}"):
            until = self._open_until.get(key)
            if until is not None:
                if now < until:
                    return True
                # Cooldown elapsed — close and forget.
                self._open_until.pop(key, None)
                self._failures.pop(key, None)
        return False

    def reset(self) -> None:
        self._failures.clear()
        self._open_until.clear()


# Module-level singleton shared by all runs in this process.
breaker = CircuitBreaker()


def get_fallback_order(settings: Any) -> List[str]:
    """The org's ordered fallback list (LLMModel db ids) from settings, or []."""
    try:
        raw = settings.get_config("llm_fallback_order") if settings else None
    except Exception:
        return []
    value = getattr(raw, "value", raw)
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if isinstance(x, str) and x][:MAX_FALLBACK_CHAIN]


async def resolve_fallback_chain(
    db: AsyncSession,
    organization: Any,
    order: List[str],
) -> List[LLMModel]:
    """Load the configured order as live, enabled LLMModel rows, order preserved.

    Disabled or deleted entries (models disabled after the list was saved) are
    silently dropped — the list is re-validated here at run time, not only at
    save time.
    """
    if not order:
        return []
    result = await db.execute(
        select(LLMModel)
        .join(LLMModel.provider)
        .filter(LLMModel.organization_id == organization.id)
        .filter(LLMModel.id.in_(order))
        .filter(LLMModel.deleted_at == None)  # noqa: E711
        .filter(LLMModel.is_enabled == True)  # noqa: E712
        .filter(LLMProvider.deleted_at == None)  # noqa: E711
        .filter(LLMProvider.is_enabled == True)  # noqa: E712
    )
    by_id = {str(m.id): m for m in result.unique().scalars().all()}
    return [by_id[i] for i in order if i in by_id]


class FallbackController:
    """Per-run fallback walker.

    Holds the resolved chain and the identity of the currently-effective model.
    ``next_candidate`` records the failure on the breaker and returns the next
    eligible model, or None when the chain is exhausted (caller then surfaces
    the original error exactly as before this feature existed).
    """

    def __init__(self, chain: List[LLMModel], current_model: Any):
        self.chain = chain
        self._current = current_model
        self._attempted_ids = {str(getattr(current_model, "id", ""))}

    @property
    def current(self) -> Any:
        return self._current

    def next_candidate(self, err_code: str) -> Optional[LLMModel]:
        if err_code not in FALLBACK_ELIGIBLE_CODES:
            return None
        cur = self._current
        cur_provider_id = str(getattr(getattr(cur, "provider", None), "id", ""))
        breaker.record_failure(cur_provider_id, str(getattr(cur, "id", "")), err_code)

        for m in self.chain:
            mid = str(m.id)
            if mid in self._attempted_ids:
                continue
            provider_id = str(getattr(getattr(m, "provider", None), "id", ""))
            if breaker.is_open(provider_id, mid):
                logger.info("[fallback] skipping %s — breaker open", getattr(m, "name", mid))
                self._attempted_ids.add(mid)
                continue
            self._attempted_ids.add(mid)
            self._current = m
            return m
        return None
