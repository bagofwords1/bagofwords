import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Mapping, Optional

from app.settings.config import settings


logger = logging.getLogger(__name__)

try:
    from posthog import Posthog  # type: ignore
except Exception:  # pragma: no cover - safe import guard
    Posthog = None  # type: ignore

# Hardcoded PostHog credentials per user request
POSTHOG_API_KEY = "phc_aWBVqSFPK846NT5XRUm9NmiiX0ElKNDJwA97lZ3DfGq"
POSTHOG_HOST = "https://us.i.posthog.com"

# Thread pool for running blocking PostHog calls without blocking the event loop
_telemetry_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telemetry")


def _init_posthog_client():
    """Initialize a singleton PostHog client using hardcoded key/host."""
    api_key = POSTHOG_API_KEY
    host = POSTHOG_HOST
    if not api_key or Posthog is None:
        return None
    try:
        return Posthog(api_key, host=host)
    except Exception:
        logger.exception("Failed to initialize PostHog client")
        return None


_posthog = _init_posthog_client()


def _do_capture(
    distinct_id: str,
    event: str,
    properties: dict,
    timestamp: Optional[datetime],
    groups: Optional[dict],
) -> None:
    """Blocking PostHog capture - runs in thread pool."""
    try:
        _posthog.capture(
            distinct_id=distinct_id,
            event=event,
            properties=properties,
            timestamp=timestamp,
            groups=groups,
        )
    except Exception:
        logger.exception("telemetry._do_capture failed")


def _do_identify(distinct_id: str, properties: dict) -> None:
    """Blocking PostHog identify - runs in thread pool."""
    try:
        _posthog.identify(distinct_id=distinct_id, properties=properties)
    except Exception:
        logger.exception("telemetry._do_identify failed")


class Telemetry:
    """Minimal server-side telemetry helper backed by PostHog.

    All calls are fire-and-forget background tasks that never block.
    If disabled, methods are no-ops. Errors never surface to callers.
    """

    @staticmethod
    def _enabled() -> bool:
        try:
            return bool(getattr(settings.bow_config, "telemetry", None) and settings.bow_config.telemetry.enabled)
        except Exception:
            return False

    @classmethod
    async def capture(
        cls,
        event: str,
        properties: Optional[Mapping[str, Any]] = None,
        user_id: Optional[str] = None,
        org_id: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
    ) -> None:
        """Fire-and-forget telemetry capture. Never blocks the caller."""
        if not (cls._enabled() and _posthog is not None):
            return
        try:
            props = dict(properties or {})
            if org_id is not None:
                props["org_id"] = str(org_id)

            loop = asyncio.get_running_loop()
            # Submit to thread pool and don't await - fire and forget
            loop.run_in_executor(
                _telemetry_executor,
                _do_capture,
                str(user_id or "anonymous"),
                event,
                props,
                occurred_at,
                {"organization": str(org_id)} if org_id else None,
            )
        except Exception:
            logger.exception("telemetry.capture failed")

    @classmethod
    async def identify(
        cls,
        user_id: str,
        traits: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Fire-and-forget telemetry identify. Never blocks the caller."""
        if not (cls._enabled() and _posthog is not None):
            return
        try:
            loop = asyncio.get_running_loop()
            # Submit to thread pool and don't await - fire and forget
            loop.run_in_executor(
                _telemetry_executor,
                _do_identify,
                str(user_id),
                dict(traits or {}),
            )
        except Exception:
            logger.exception("telemetry.identify failed")


# Convenience alias for imports: from app.core.telemetry import telemetry
telemetry = Telemetry