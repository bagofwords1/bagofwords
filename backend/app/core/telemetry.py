import asyncio
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Mapping, Optional

from app.settings.config import settings


logger = logging.getLogger(__name__)

try:
    from posthog import Posthog  # type: ignore
except Exception:  # pragma: no cover - safe import guard
    Posthog = None  # type: ignore

# PostHog "phc_*" ingest keys are write-only project keys intended to ship in
# client code (see https://posthog.com/docs/api). Override via env for self-
# hosted PostHog deployments. Default is the bagofwords cloud project.
POSTHOG_API_KEY = os.environ.get(
    "BOW_POSTHOG_KEY",
    "phc_aWBVqSFPK846NT5XRUm9NmiiX0ElKNDJwA97lZ3DfGq",
)
POSTHOG_HOST = os.environ.get("BOW_POSTHOG_HOST", "https://us.i.posthog.com")

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


def _do_group_identify(group_type: str, group_key: str, properties: dict) -> None:
    """Blocking PostHog group_identify - runs in thread pool."""
    try:
        _posthog.group_identify(group_type, group_key, properties)
    except Exception:
        logger.exception("telemetry._do_group_identify failed")


def _default_event_properties() -> dict:
    """Cheap, process-local properties merged into every captured event.

    Both lookups are in-memory/cached (no I/O): settings.version reads a
    module-level string, and get_license_info() caches after its first call.
    """
    props: dict = {}
    try:
        props["app_version"] = settings.version
    except Exception:
        pass
    try:
        from app.ee.license import get_license_info
        props["license_tier"] = get_license_info().tier
    except Exception:
        pass
    return props


class Telemetry:
    """Minimal server-side telemetry helper backed by PostHog.

    All calls are fire-and-forget background tasks that never block.
    If disabled, methods are no-ops. Errors never surface to callers.
    """

    @staticmethod
    def _enabled() -> bool:
        try:
            # Disable telemetry in test mode
            if settings.TESTING:
                return False
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
            props = _default_event_properties()
            props.update(properties or {})
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

    @classmethod
    async def group_identify(
        cls,
        group_type: str,
        group_key: str,
        properties: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Fire-and-forget PostHog group trait update. Never blocks the caller.

        Sets traits (e.g. an org's email domain) on a PostHog group once, so
        every event tagged with that group's key shows the trait without
        resending it per-event.
        """
        if not (cls._enabled() and _posthog is not None):
            return
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                _telemetry_executor,
                _do_group_identify,
                group_type,
                str(group_key),
                dict(properties or {}),
            )
        except Exception:
            logger.exception("telemetry.group_identify failed")


# Convenience alias for imports: from app.core.telemetry import telemetry
telemetry = Telemetry

# Model families reported by name in telemetry. Order matters only where
# one family is a prefix of another ("gpt-oss" before "gpt").
_MODEL_FAMILIES = (
    "gpt-oss", "gpt", "o1", "o3", "o4", "claude", "gemini", "gemma",
    "deepseek", "llama", "mistral", "mixtral", "ministral", "magistral",
    "codestral", "devstral", "qwen", "qwq", "grok", "kimi", "glm", "phi",
    "command", "nova", "nemotron", "jamba", "minimax",
)
_FAMILY_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(f) for f in _MODEL_FAMILIES) + r")(?![a-z])"
)
# Tokens allowed after the family name: versions, sizes and published
# variant words. The first token outside this set ends the id, so a
# customer suffix ("gpt-6-sol-stmarys") never reaches telemetry.
_VERSION_TOKEN_RE = re.compile(
    r"^(v?\d+(\.\d+)*[a-z]?|r\d+|\d+(\.\d+)?[bkm]|a\d+b|\d+x\d+b|\d{8})$"
)
_VARIANT_TOKENS = {
    # OpenAI
    "astra", "sol", "terra", "luna", "mini", "nano", "pro", "codex", "chat",
    "image", "realtime", "turbo", "max", "latest", "preview", "oss", "audio",
    "sunburst", "flare", "cyber",
    # Anthropic
    "fable", "mythos", "opus", "sonnet", "haiku",
    # Google
    "flash", "lite", "exp", "thinking",
    # Open-weight families
    "instruct", "coder", "reasoner", "vl", "distill", "maverick", "scout",
    "large", "medium", "small", "tiny", "plus", "fast", "code", "it", "a",
}


def telemetry_model_id(model_id: Optional[str]) -> Optional[str]:
    """Reduce a model_id to its public model name for telemetry.

    Model ids are free text (Azure deployment names, Bedrock/Ollama ids,
    self-hosted models) and can carry a customer's name. Only a known model
    family plus the version/variant tokens after it leave the process:
    "us.anthropic.claude-sonnet-5-5-v1:0" -> "claude-sonnet-5-5",
    "stmarys-gpt-6-sol-prod" -> "gpt-6-sol", "deepseek-r1:70b" ->
    "deepseek-r1-70b". Anything without a known family reports "custom".
    """
    if not model_id:
        return None
    normalized = model_id.strip().lower()
    # Bedrock revision suffix ("-v1:0") is packaging, not the model name.
    normalized = re.sub(r"-v\d+(:\d+)?$", "", normalized)
    # Wrappers can repeat the family ("meta-llama/llama-4-..."): keep the
    # most specific reading.
    names = [_model_name_at(normalized, m) for m in _FAMILY_RE.finditer(normalized)]
    return max(names, key=len)[:64] if names else "custom"


def _model_name_at(normalized: str, match: "re.Match[str]") -> str:
    parts = [match.group(1)]
    rest = normalized[match.end():]
    # Ollama glues the version to the family ("llama3.3", "qwen2.5").
    glued = re.match(r"^\d+(\.\d+)*", rest)
    if glued:
        parts[0] += glued.group(0)
        rest = rest[glued.end():]
    for token in re.split(r"[-_:/ ]+", rest.lstrip("-_:/ ")):
        if not token or not (token in _VARIANT_TOKENS or _VERSION_TOKEN_RE.match(token)):
            break
        parts.append(token)
    return "-".join(parts)


# Free/consumer email providers excluded from org domain attribution — a
# domain shared by millions of unrelated signups isn't a useful org signal
# and would misrepresent unrelated orgs as the same "company".
_FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "live.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "proton.me", "gmx.com", "yandex.com", "mail.com", "zoho.com",
}


def derive_org_domain(email: Optional[str]) -> Optional[str]:
    """Derive a privacy-safe org identifier from a user's email.

    Returns only the domain (never the local part or the full address), and
    None for free/consumer providers so personal-email orgs aren't tagged
    with a domain that doesn't identify a company.
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain or domain in _FREE_EMAIL_DOMAINS:
        return None
    return domain