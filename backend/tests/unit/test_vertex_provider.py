"""Unit tests for the Vertex AI provider.

Covers the three things that are Vertex-specific and easy to regress:
endpoint derivation (the pinned anthropic SDK gets the non-regional hosts
wrong, so we derive them ourselves), transport selection by model id, and
credential/auth-mode handling. No Google APIs are called — a fake credential
object stands in, and client construction is asserted rather than requests.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.ai.llm.clients import vertex_auth
from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.clients.google_client import Google
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.llm import _is_anthropic_model_id, _is_gemini_model_id, _vertex_timeout

_PROJECT = "test-project-123"


class _FakeCredentials:
    """Stands in for a google.auth credential."""

    def __init__(self, valid: bool = True, token: str = "tok-initial"):
        self.valid = valid
        self.token = token
        self.refreshes = 0

    def refresh(self, request):  # noqa: ARG002 - signature mirrors google-auth
        self.refreshes += 1
        self.valid = True
        self.token = f"tok-refreshed-{self.refreshes}"


# --------------------------------------------------------------------------
# Endpoint derivation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "location,expected",
    [
        ("global", "aiplatform.googleapis.com"),
        ("us", "aiplatform.us.rep.googleapis.com"),
        ("eu", "aiplatform.eu.rep.googleapis.com"),
        ("us-east5", "us-east5-aiplatform.googleapis.com"),
        ("US-EAST5", "us-east5-aiplatform.googleapis.com"),
        (None, "aiplatform.googleapis.com"),
        ("", "aiplatform.googleapis.com"),
        ("  ", "aiplatform.googleapis.com"),
    ],
)
def test_api_host(location, expected):
    assert vertex_auth.api_host(location) == expected


def test_global_host_is_not_prefixed():
    """Regression guard for the bug the pinned anthropic SDK has.

    anthropic<=0.40 interpolates ``{region}-aiplatform.googleapis.com``
    unconditionally, producing ``global-aiplatform.googleapis.com`` — a host
    that serves an HTML 404 rather than an API. Every base URL must therefore
    come from this module, never from the SDK's own derivation.
    """
    assert "global-aiplatform" not in vertex_auth.anthropic_base_url("global")
    assert vertex_auth.anthropic_base_url("global") == "https://aiplatform.googleapis.com/v1"


def test_anthropic_base_url_is_versioned_root_only():
    # The SDK appends /projects/.../publishers/anthropic/... itself.
    url = vertex_auth.anthropic_base_url("us-east5")
    assert url == "https://us-east5-aiplatform.googleapis.com/v1"
    assert "publishers" not in url


def test_openai_base_url_shape():
    assert vertex_auth.openai_base_url(_PROJECT) == (
        "https://aiplatform.googleapis.com/v1/projects/test-project-123"
        "/locations/global/endpoints/openapi"
    )


def test_openai_base_url_regional():
    assert vertex_auth.openai_base_url(_PROJECT, "us-central1") == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/test-project-123"
        "/locations/us-central1/endpoints/openapi"
    )


# --------------------------------------------------------------------------
# Transport selection by model id
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model_id,surface",
    [
        ("claude-sonnet-5", "anthropic"),
        ("claude-opus-4-8", "anthropic"),
        ("claude-sonnet-4-5@20250929", "anthropic"),
        ("anthropic-claude-sonnet", "anthropic"),
        ("gemini-3.6-flash", "gemini"),
        ("gemini-2.5-flash", "gemini"),
        ("gemini-flash-latest", "gemini"),
        ("xai/grok-4.6", "openai"),
        ("zai-org/glm-5.2-maas", "openai"),
        ("meta/llama-4-scout", "openai"),
        ("deepseek-ai/deepseek-v3", "openai"),
    ],
)
def test_model_id_routes_to_expected_surface(model_id, surface):
    if surface == "anthropic":
        assert _is_anthropic_model_id(model_id)
        assert not _is_gemini_model_id(model_id)
    elif surface == "gemini":
        assert _is_gemini_model_id(model_id)
        assert not _is_anthropic_model_id(model_id)
    else:
        assert not _is_anthropic_model_id(model_id)
        assert not _is_gemini_model_id(model_id)


# --------------------------------------------------------------------------
# Credential resolution
# --------------------------------------------------------------------------

def test_unsupported_auth_mode_rejected():
    with pytest.raises(ValueError, match="Unsupported Vertex auth_mode"):
        vertex_auth.resolve_credentials(auth_mode="oauth", project_id=_PROJECT)


def test_service_account_mode_requires_key():
    vertex_auth.reset_cache()
    with pytest.raises(ValueError, match="requires a service account key"):
        vertex_auth.resolve_credentials(auth_mode="service_account", project_id=_PROJECT)


def test_service_account_mode_rejects_non_json():
    vertex_auth.reset_cache()
    with pytest.raises(ValueError, match="not valid JSON"):
        vertex_auth.resolve_credentials(
            auth_mode="service_account",
            service_account_json="-----BEGIN PRIVATE KEY-----",
            project_id=_PROJECT,
        )


def test_service_account_mode_rejects_key_without_private_key():
    vertex_auth.reset_cache()
    with pytest.raises(ValueError, match="missing 'private_key'"):
        vertex_auth.resolve_credentials(
            auth_mode="service_account",
            service_account_json=json.dumps({"type": "service_account", "project_id": "p"}),
            project_id=_PROJECT,
        )


def test_service_account_project_falls_back_to_key_project():
    """A key file names its own project; the admin may leave the field blank."""
    vertex_auth.reset_cache()
    key = json.dumps({
        "type": "service_account",
        "project_id": "project-from-key",
        "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
        "client_email": "sa@project-from-key.iam.gserviceaccount.com",
    })
    fake = _FakeCredentials()
    with patch("google.oauth2.service_account.Credentials.from_service_account_info",
               return_value=fake):
        creds, project = vertex_auth.resolve_credentials(
            auth_mode="service_account", service_account_json=key, project_id=None
        )
    assert creds is fake
    assert project == "project-from-key"


def test_explicit_project_wins_over_key_project():
    """One service account can be granted on several projects."""
    vertex_auth.reset_cache()
    key = json.dumps({
        "type": "service_account",
        "project_id": "project-from-key",
        "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
    })
    with patch("google.oauth2.service_account.Credentials.from_service_account_info",
               return_value=_FakeCredentials()):
        _, project = vertex_auth.resolve_credentials(
            auth_mode="service_account", service_account_json=key, project_id="chosen-project"
        )
    assert project == "chosen-project"


def test_adc_failure_message_names_the_alternatives():
    vertex_auth.reset_cache()
    with patch("google.auth.default", side_effect=Exception("no ADC here")):
        with pytest.raises(ValueError, match="Workload Identity"):
            vertex_auth.resolve_credentials(auth_mode="adc", project_id=_PROJECT)


def test_adc_requires_a_project_from_somewhere():
    vertex_auth.reset_cache()
    with patch("google.auth.default", return_value=(_FakeCredentials(), None)):
        with pytest.raises(ValueError, match="requires project_id"):
            vertex_auth.resolve_credentials(auth_mode="adc", project_id=None)


def test_credentials_are_cached_per_key():
    """An LLM instance is built per call site; re-parsing an RSA key each time
    would be a real cost on a busy org."""
    vertex_auth.reset_cache()
    key = json.dumps({
        "type": "service_account",
        "project_id": "p",
        "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
    })
    with patch("google.oauth2.service_account.Credentials.from_service_account_info",
               return_value=_FakeCredentials()) as mk:
        first, _ = vertex_auth.resolve_credentials(
            auth_mode="service_account", service_account_json=key, project_id="p")
        second, _ = vertex_auth.resolve_credentials(
            auth_mode="service_account", service_account_json=key, project_id="p")
    assert first is second
    assert mk.call_count == 1


def test_different_keys_are_cached_separately():
    vertex_auth.reset_cache()
    base = {"type": "service_account", "project_id": "p",
            "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"}
    key_a = json.dumps({**base, "private_key_id": "aaa"})
    key_b = json.dumps({**base, "private_key_id": "bbb"})
    with patch("google.oauth2.service_account.Credentials.from_service_account_info",
               side_effect=[_FakeCredentials(), _FakeCredentials()]) as mk:
        a, _ = vertex_auth.resolve_credentials(
            auth_mode="service_account", service_account_json=key_a, project_id="p")
        b, _ = vertex_auth.resolve_credentials(
            auth_mode="service_account", service_account_json=key_b, project_id="p")
    assert a is not b
    assert mk.call_count == 2


# --------------------------------------------------------------------------
# Token refresh
# --------------------------------------------------------------------------

def test_refresh_only_when_invalid():
    valid = _FakeCredentials(valid=True, token="still-good")
    assert vertex_auth.refresh_if_needed(valid) == "still-good"
    assert valid.refreshes == 0


def test_refresh_when_expired():
    expired = _FakeCredentials(valid=False)
    token = vertex_auth.refresh_if_needed(expired)
    assert expired.refreshes == 1
    assert token == "tok-refreshed-1"


def test_sync_auth_flow_sets_bearer_header():
    creds = _FakeCredentials(token="abc123")
    auth = vertex_auth.GoogleBearerAuth(vertex_auth.token_provider(creds))
    request = httpx.Request("POST", "https://example.com")
    next(auth.sync_auth_flow(request))
    assert request.headers["Authorization"] == "Bearer abc123"


def test_sync_auth_flow_refreshes_an_expired_token():
    """The whole point of the auth flow: a client outlives its token."""
    creds = _FakeCredentials(valid=False)
    auth = vertex_auth.GoogleBearerAuth(vertex_auth.token_provider(creds))
    request = httpx.Request("POST", "https://example.com")
    next(auth.sync_auth_flow(request))
    assert request.headers["Authorization"] == "Bearer tok-refreshed-1"


@pytest.mark.asyncio
async def test_async_auth_flow_sets_bearer_header():
    creds = _FakeCredentials(token="async-tok")
    auth = vertex_auth.GoogleBearerAuth(vertex_auth.token_provider(creds))
    request = httpx.Request("POST", "https://example.com")
    flow = auth.async_auth_flow(request)
    await flow.__anext__()
    assert request.headers["Authorization"] == "Bearer async-tok"


@pytest.mark.asyncio
async def test_async_auth_flow_refreshes_off_the_event_loop():
    """google-auth refresh does blocking HTTP; it must not stall the loop."""
    creds = _FakeCredentials(valid=False)
    auth = vertex_auth.GoogleBearerAuth(vertex_auth.token_provider(creds))
    request = httpx.Request("POST", "https://example.com")
    with patch("asyncio.to_thread", wraps=__import__("asyncio").to_thread) as to_thread:
        flow = auth.async_auth_flow(request)
        await flow.__anext__()
    assert to_thread.called
    assert request.headers["Authorization"] == "Bearer tok-refreshed-1"


# --------------------------------------------------------------------------
# Client construction
# --------------------------------------------------------------------------

def test_anthropic_client_uses_vertex_transport():
    from anthropic import AnthropicVertex, AsyncAnthropicVertex

    client = Anthropic(
        vertex={"project_id": _PROJECT, "region": "global", "credentials": _FakeCredentials()},
        base_url=vertex_auth.anthropic_base_url("global"),
    )
    assert isinstance(client.client, AnthropicVertex)
    assert isinstance(client.async_client, AsyncAnthropicVertex)
    assert "global-aiplatform" not in str(client.async_client.base_url)


def test_anthropic_client_without_vertex_stays_first_party():
    from anthropic import Anthropic as AnthropicAPI

    client = Anthropic(api_key="sk-test")
    assert isinstance(client.client, AnthropicAPI)


def test_google_client_uses_vertex_transport():
    client = Google(vertex={
        "project": _PROJECT, "location": "global", "credentials": _FakeCredentials(),
    })
    assert client.client.vertexai is True


def test_openai_client_accepts_auth_flow_and_timeout():
    auth = vertex_auth.GoogleBearerAuth(lambda: "tok")
    client = OpenAi(
        api_key="vertex-oauth",
        base_url=vertex_auth.openai_base_url(_PROJECT),
        auth=auth,
        timeout=_vertex_timeout(),
    )
    assert client.client._client.auth is auth
    assert client.async_client._client.auth is auth


def test_openai_client_without_auth_keeps_sdk_default_http_client():
    """Existing callers must not silently acquire a different timeout."""
    client = OpenAi(api_key="sk-test")
    assert client.client.timeout is not None


def test_vertex_timeout_profile():
    timeout = _vertex_timeout()
    # Generous read window (MaaS reasoning models have multi-minute outliers),
    # short connect so an unreachable endpoint still fails fast.
    assert timeout.read >= 120
    assert timeout.connect <= 30


def test_maas_is_pinned_to_global():
    assert vertex_auth.MAAS_LOCATION == "global"
