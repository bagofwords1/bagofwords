"""Loads and caches a per-organization :class:`PiiRedactor`.

The LLM chokepoint calls this to obtain the redactor for the model's
organization. Two hard guarantees live here:

* **Enterprise gate.** If the instance is not licensed for ``pii_protection``,
  this always returns ``None`` — no config value can turn redaction on in a
  community build.
* **Cheap steady state.** The compiled ruleset is cached per org with a short
  TTL so redaction doesn't add a DB round-trip to every LLM call. Settings
  writes call :func:`invalidate` for instant reflection.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, Optional, Tuple

from sqlalchemy.future import select

from app.ee.license import has_feature
from .redactor import PiiRedactor, build_redactor

logger = logging.getLogger(__name__)

# org_id -> (expires_at_monotonic, redactor_or_None)
_CACHE: Dict[str, Tuple[float, Optional[PiiRedactor]]] = {}
_CACHE_TTL_SECONDS = 10.0


def invalidate(organization_id: Optional[str] = None) -> None:
    """Drop cached redactors. Called after a settings write so a toggle takes
    effect immediately instead of waiting out the TTL."""
    if organization_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(organization_id), None)


async def load_redactor_for_org(
    organization_id: Optional[str],
    session_maker: Optional[Callable[[], "object"]],
) -> Optional[PiiRedactor]:
    """Return the redactor for ``organization_id`` or None.

    None means "do not redact" — because the feature is unlicensed, disabled,
    has no active rules, or the org/session is unavailable.
    """
    # Enterprise gate first — never touches the DB on community instances.
    if not has_feature("pii_protection"):
        return None
    if not organization_id or session_maker is None:
        return None

    org_id = str(organization_id)
    now = time.monotonic()
    cached = _CACHE.get(org_id)
    if cached is not None and cached[0] > now:
        return cached[1]

    redactor: Optional[PiiRedactor] = None
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
                pii_config = settings.config.get("pii_protection")
                redactor = build_redactor(pii_config)
    except Exception as exc:  # pragma: no cover - defensive
        # Loading must never break inference. Cache a short "no redactor" so we
        # don't hammer the DB on every call while it's failing.
        logger.warning("PII redactor load failed for org %s: %s", org_id, exc)
        redactor = None

    _CACHE[org_id] = (now + _CACHE_TTL_SECONDS, redactor)
    return redactor
