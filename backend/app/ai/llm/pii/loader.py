"""Loads and caches an organization's compiled PII policies.

The LLM chokepoint calls this to obtain the redactor for the provider it is
about to call (the policy assigned to that provider, else Default); display
surfaces get the display policy. Two hard guarantees live here:

* **Enterprise gate.** If the instance is not licensed for ``pii_protection``,
  this always returns ``None`` — no config value can turn redaction on in a
  community build.
* **Cheap steady state.** The compiled policies are cached per org with a short
  TTL so redaction doesn't add a DB round-trip to every LLM call. Settings
  writes call :func:`invalidate` for instant reflection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Dict, Optional, Tuple

from sqlalchemy.future import select

from app.ee.license import has_feature
from .policy import CompiledPiiConfig, compile_config
from .redactor import PiiRedactor

logger = logging.getLogger(__name__)

# org_id -> (expires_at_monotonic, compiled_policies_or_None)
_CACHE: Dict[str, Tuple[float, Optional[CompiledPiiConfig]]] = {}
_CACHE_TTL_SECONDS = 10.0
# org_ids whose cached None above is a failed load, not "do not redact"
# (only load_redactor_for_org_checked reads it).
_FAILED: set = set()
# load_redactor_for_org_checked only: org_id -> monotonic time before which a
# failed load is not retried (one DB read and one warning per window while
# the settings stay unreadable).
_RETRY_AT: Dict[str, float] = {}
_FAILURE_BACKOFF_SECONDS = 1.0
# load_redactor_for_org_checked only: org_id -> its load in flight, which
# concurrent callers await instead of starting their own (single-flight).
_INFLIGHT: Dict[str, "asyncio.Future"] = {}


def invalidate(organization_id: Optional[str] = None) -> None:
    """Drop cached redactors. Called after a settings write so a toggle takes
    effect immediately instead of waiting out the TTL."""
    if organization_id is None:
        _CACHE.clear()
        _FAILED.clear()
        _RETRY_AT.clear()
        _INFLIGHT.clear()
    else:
        _CACHE.pop(str(organization_id), None)
        _FAILED.discard(str(organization_id))
        _RETRY_AT.pop(str(organization_id), None)
        _INFLIGHT.pop(str(organization_id), None)


async def load_redactor_for_org(
    organization_id: Optional[str],
    session_maker: Optional[Callable[[], "object"]],
    provider_id: Optional[str] = None,
) -> Optional[PiiRedactor]:
    """Return the redactor to apply, or None.

    With ``provider_id`` (the LLM provider a prompt is about to be sent to):
    that provider's policy, else Default. Without it (display surfaces): the
    display policy.

    None means "do not redact" — because the feature is unlicensed, disabled,
    the policy has no active rule or is Exempt, or the org/session is
    unavailable.
    """
    compiled = await load_policies_for_org(organization_id, session_maker)
    if compiled is None:
        return None
    if provider_id is not None:
        return compiled.for_provider(provider_id)
    return compiled.for_display()


async def load_redactor_for_call(
    organization_id: Optional[str],
    session_maker: Optional[Callable[[], "object"]],
    provider_id: Optional[str],
    agent_ids: Optional[Tuple[str, ...]],
) -> Optional[PiiRedactor]:
    """The redactor for one LLM call: the provider's policy merged with the
    policies of the agents in scope (``agent_ids``; ``None`` = unknown, which
    applies every agent policy). See ``docs/design/pii-agent-policies.md``."""
    compiled = await load_policies_for_org(organization_id, session_maker)
    if compiled is None:
        return None
    return compiled.for_call(provider_id, agent_ids)


async def load_policies_for_org(
    organization_id: Optional[str],
    session_maker: Optional[Callable[[], "object"]],
) -> Optional[CompiledPiiConfig]:
    """The org's compiled policies (cached), or None when PII protection
    cannot apply (unlicensed, no org, no session)."""
    # Enterprise gate first — never touches the DB on community instances.
    if not has_feature("pii_protection"):
        return None
    if not organization_id or session_maker is None:
        return None

    org_id = str(organization_id)
    cached = _CACHE.get(org_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    return (await _load(org_id, session_maker))[0]


async def load_redactor_for_org_checked(
    organization_id: Optional[str],
    session_maker: Optional[Callable[[], "object"]],
) -> Tuple[Optional[PiiRedactor], bool]:
    """``load_redactor_for_org``, also saying whether the load succeeded.

    ``(redactor_or_None, True)``: the answer is known (None: do not redact,
    as for unlicensed, disabled or rule-less). ``(None, False)``: the org's
    settings could not be read (or there is nothing to read them with), so
    redaction may be required and is not available; a display surface that
    must not show raw values withholds them. A cached failure is retried
    rather than trusted, but a failure of this loader at most once per
    ``_FAILURE_BACKOFF_SECONDS`` after that load ended (still ``(None,
    False)`` meanwhile). Concurrent callers for one org share one load.
    ``load_redactor_for_org`` is unchanged.
    """
    if not has_feature("pii_protection"):
        return None, True
    if not organization_id or session_maker is None:
        return None, False
    org_id = str(organization_id)
    now = time.monotonic()
    cached = _CACHE.get(org_id)
    if cached is not None and cached[0] > now and org_id not in _FAILED:
        return (cached[1].for_display() if cached[1] else None), True
    if org_id in _FAILED and _RETRY_AT.get(org_id, 0.0) > now:
        return None, False
    flight = _INFLIGHT.get(org_id)
    if flight is None or flight.get_loop() is not asyncio.get_running_loop():  # not one left by a closed loop
        flight = asyncio.ensure_future(_load_checked(org_id, session_maker))
        _INFLIGHT[org_id] = flight
        flight.add_done_callback(lambda f: _INFLIGHT.pop(org_id, None) if _INFLIGHT.get(org_id) is f else None)
    # Shielded: a caller cancelled while waiting does not cancel the others' load.
    compiled, ok = await asyncio.shield(flight)
    return (compiled.for_display() if compiled else None), ok


async def _load_checked(org_id: str, session_maker: Callable[[], "object"]) -> Tuple[Optional[CompiledPiiConfig], bool]:
    compiled, ok = await _load(org_id, session_maker)
    if ok:
        _RETRY_AT.pop(org_id, None)
    else:
        # From the end of the load: a read slower than the window still backs off.
        _RETRY_AT[org_id] = time.monotonic() + _FAILURE_BACKOFF_SECONDS
    return compiled, ok


async def _load(org_id: str, session_maker: Callable[[], "object"]) -> Tuple[Optional[CompiledPiiConfig], bool]:
    now = time.monotonic()
    compiled: Optional[CompiledPiiConfig] = None
    ok = True
    try:
        from app.models.organization_settings import OrganizationSettings

        async with session_maker() as session:
            result = await session.execute(
                select(OrganizationSettings).filter(
                    OrganizationSettings.organization_id == org_id
                )
            )
            settings = result.scalar_one_or_none()
            if settings and isinstance(settings.config, dict):
                compiled = compile_config(settings.config.get("pii_protection"))
    except Exception as exc:  # pragma: no cover - defensive
        # Loading must never break inference. Cache a short "no redactor" so we
        # don't hammer the DB on every call while it's failing.
        logger.warning("PII redactor load failed for org %s: %s", org_id, exc, exc_info=True)
        compiled = None
        ok = False

    _CACHE[org_id] = (now + _CACHE_TTL_SECONDS, compiled)
    if ok:
        _FAILED.discard(org_id)
    else:
        _FAILED.add(org_id)
    return compiled, ok

