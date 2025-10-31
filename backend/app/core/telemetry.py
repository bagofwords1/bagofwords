import asyncio
import logging
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


class Telemetry:
    """Minimal server-side telemetry helper backed by PostHog.

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
        if not (cls._enabled() and _posthog is not None):
            return
        try:
            props = dict(properties or {})
            if org_id is not None:
                props["org_id"] = str(org_id)

            _posthog.capture(
                distinct_id=str(user_id or "anonymous"),
                event=event,
                properties=props,
                timestamp=occurred_at,
                groups={"organization": str(org_id)} if org_id else None,
            )
        except Exception:
            logger.exception("telemetry.capture failed")

    @classmethod
    async def identify(
        cls,
        user_id: str,
        traits: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not (cls._enabled() and _posthog is not None):
            return
        try:
            _posthog.identify(distinct_id=str(user_id), properties=dict(traits or {}))
        except Exception:
            logger.exception("telemetry.identify failed")


# Convenience alias for imports: from app.core.telemetry import telemetry
telemetry = Telemetry