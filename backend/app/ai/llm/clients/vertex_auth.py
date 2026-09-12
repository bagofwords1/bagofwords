"""Google Cloud auth and endpoint derivation for the Vertex AI provider.

Vertex is not one API — it is three wire protocols behind one credential:

  * ``claude-*``      the Anthropic Messages API (``publishers/anthropic``)
  * ``gemini-*``      google-genai (``publishers/google``)
  * everything else   OpenAI-compatible Chat Completions
                      (``endpoints/openapi``, e.g. ``xai/grok-4.6``,
                      ``zai-org/glm-5.2-maas``, ``meta/llama-*``)

Each protocol is already implemented by an existing client, so this module
only supplies what all three need and none of them own: an OAuth credential
that refreshes itself, and the right host to talk to.

Auth modes mirror the Bedrock provider's:

  * ``adc``             Application Default Credentials from the environment —
                        GKE Workload Identity, GCE metadata, a mounted
                        ``GOOGLE_APPLICATION_CREDENTIALS`` key, or a developer's
                        ``gcloud auth application-default login``.
  * ``service_account`` an explicit service-account key JSON, stored encrypted
                        on the provider row.

Credentials are cached per (auth_mode, project, key fingerprint) because an
``LLM`` instance is constructed per run and per call site: without the cache a
busy org re-parses an RSA key and re-mints a token on every single call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from typing import Any, Callable, Optional

import httpx

from app.settings.logging_config import get_logger

logger = get_logger(__name__)

SUPPORTED_AUTH_MODES = ("adc", "service_account")

# The only scope Vertex inference needs. Narrower scopes (e.g. the
# aiplatform-specific ones) are not accepted for publisher-model calls.
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

# Multi-region and global endpoints do not follow the ``{region}-`` pattern.
# The pinned anthropic SDK (0.40.x) interpolates unconditionally and so builds
# ``https://global-aiplatform.googleapis.com`` for the global endpoint, which
# does not resolve to an API at all — it answers with an HTML 404 page. Every
# base URL is therefore derived here and passed to the SDKs explicitly.
_SPECIAL_HOSTS = {
    "global": "aiplatform.googleapis.com",
    "us": "aiplatform.us.rep.googleapis.com",
    "eu": "aiplatform.eu.rep.googleapis.com",
}

# Third-party Model-as-a-Service publishers (xAI, Z.ai, Meta, DeepSeek,
# Qwen…) are served ONLY through the global endpoint. Vertex rejects them in a
# regional location with a 400 FAILED_PRECONDITION that names the constraint
# ("only available via global endpoint"), so the OpenAI-compatible surface
# ignores the provider's configured location rather than forwarding a request
# that cannot succeed.
MAAS_LOCATION = "global"

_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def normalize_location(location: Optional[str]) -> str:
    """A provider's configured Vertex location, defaulting to ``global``."""
    return (location or "").strip().lower() or "global"


def api_host(location: Optional[str]) -> str:
    """The aiplatform hostname serving a location."""
    loc = normalize_location(location)
    return _SPECIAL_HOSTS.get(loc, f"{loc}-aiplatform.googleapis.com")


def anthropic_base_url(location: Optional[str]) -> str:
    """Base URL for the Anthropic Messages API on Vertex.

    The Anthropic SDK appends ``/projects/{id}/locations/{loc}/publishers/...``
    itself, so this is only the versioned root.
    """
    return f"https://{api_host(location)}/v1"


def openai_base_url(project_id: str, location: Optional[str] = MAAS_LOCATION) -> str:
    """Base URL for Vertex's OpenAI-compatible surface.

    The OpenAI SDK appends ``/chat/completions``.
    """
    loc = normalize_location(location)
    return (
        f"https://{api_host(loc)}/v1/projects/{project_id}"
        f"/locations/{loc}/endpoints/openapi"
    )


def _fingerprint(auth_mode: str, project_id: Optional[str], key_json: Optional[str]) -> str:
    digest = hashlib.sha256((key_json or "").encode()).hexdigest()[:16] if key_json else "-"
    return f"{auth_mode}:{project_id or '-'}:{digest}"


def resolve_credentials(
    auth_mode: str = "adc",
    service_account_json: Optional[str] = None,
    project_id: Optional[str] = None,
):
    """Return (google credentials, project_id) for a provider's auth mode.

    ``project_id`` from the provider row wins over whatever the credential
    itself carries: one service account may legitimately be granted
    ``roles/aiplatform.user`` on several projects, and the admin's choice is
    the authoritative one.
    """
    if auth_mode not in SUPPORTED_AUTH_MODES:
        raise ValueError(
            f"Unsupported Vertex auth_mode '{auth_mode}'. "
            f"Supported modes: {', '.join(SUPPORTED_AUTH_MODES)}."
        )

    cache_key = _fingerprint(auth_mode, project_id, service_account_json)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    if auth_mode == "service_account":
        if not service_account_json:
            raise ValueError(
                "Vertex auth_mode 'service_account' requires a service account key JSON."
            )
        from google.oauth2 import service_account as _sa

        try:
            info = json.loads(service_account_json)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Vertex service account key is not valid JSON: {exc}") from exc
        if not isinstance(info, dict) or "private_key" not in info:
            raise ValueError(
                "Vertex service account key JSON is missing 'private_key' — paste the "
                "whole key file, not just part of it."
            )
        credentials = _sa.Credentials.from_service_account_info(info, scopes=list(SCOPES))
        resolved_project = project_id or info.get("project_id")
    else:
        import google.auth

        try:
            credentials, adc_project = google.auth.default(scopes=list(SCOPES))
        except Exception as exc:
            raise ValueError(
                "Vertex auth_mode 'adc' found no Application Default Credentials. "
                "Attach a Workload Identity service account, set "
                "GOOGLE_APPLICATION_CREDENTIALS, or use the 'service_account' auth "
                f"mode instead. ({exc})"
            ) from exc
        resolved_project = project_id or adc_project

    if not resolved_project:
        raise ValueError(
            "Vertex provider requires project_id (the credential did not carry one)."
        )

    result = (credentials, resolved_project)
    with _CACHE_LOCK:
        _CACHE[cache_key] = result
    return result


def refresh_if_needed(credentials) -> str:
    """Mint or renew the credential's access token and return it.

    google-auth tokens live about an hour, which is well inside a single long
    agent run, so every call path has to be able to renew rather than capture
    a token once at client construction.
    """
    if not credentials.valid:
        from google.auth.transport.requests import Request

        credentials.refresh(Request())
    return credentials.token


def token_provider(credentials) -> Callable[[], str]:
    """A callable returning a currently-valid access token."""

    def _provide() -> str:
        return refresh_if_needed(credentials)

    return _provide


class GoogleBearerAuth(httpx.Auth):
    """Inject a fresh Google OAuth bearer token on every request.

    Used for Vertex's OpenAI-compatible surface, where the OpenAI SDK only
    accepts a static ``api_key`` string. Going through an httpx auth flow keeps
    a client usable past the token's ~1h lifetime instead of failing mid-run.

    Both flows are implemented: the async one refreshes in a worker thread,
    since google-auth's refresh performs blocking HTTP and would otherwise
    stall the event loop for every concurrent request.
    """

    def __init__(self, provider: Callable[[], str]):
        self._provider = provider

    def sync_auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._provider()}"
        yield request

    async def async_auth_flow(self, request):
        token = await asyncio.to_thread(self._provider)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def reset_cache() -> None:
    """Drop cached credentials. For tests, and for credential rotation."""
    with _CACHE_LOCK:
        _CACHE.clear()
