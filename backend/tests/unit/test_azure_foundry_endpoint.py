"""Azure AI Foundry endpoints must route to the OpenAI-compatible client.

Reported by a customer: pasting a Foundry endpoint into the Azure provider
fails, because the Azure SDK builds a deployment-scoped URL
(``/openai/deployments/{name}/chat/completions?api-version=``) that only an
Azure OpenAI resource serves. Foundry resources serve the OpenAI-compatible
``/openai/v1`` surface instead — which is also the only surface that reaches
non-OpenAI catalog deployments (Llama, DeepSeek, Grok).

These tests pin the routing decision, and guard the Azure OpenAI path against
regressing while it changes.
"""
import types

import pytest

from app.ai.llm.llm import (
    LLM,
    _azure_foundry_anthropic_base_url,
    _is_anthropic_model_id,
    _resolve_azure_endpoint_mode,
)
from app.ai.llm.clients.anthropic_client import Anthropic
from app.ai.llm.clients.azure_client import AzureClient
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient


def _azure_model(model_id: str = "gpt-4o", **additional_config) -> types.SimpleNamespace:
    """An LLMModel stand-in for an Azure provider with the given config."""
    provider = types.SimpleNamespace(
        id="p1",
        provider_type="azure",
        additional_config={"endpoint_url": "https://r.openai.azure.com", **additional_config},
        decrypt_credentials=lambda: ("test-key", None),
    )
    return types.SimpleNamespace(
        model_id=model_id,
        provider=provider,
        organization_id="o1",
        config={},
    )


class TestEndpointModeResolution:
    """Inference is hostname-based; an explicit mode always wins."""

    @pytest.mark.parametrize("url", [
        "https://myres.services.ai.azure.com",
        "https://myres.services.ai.azure.com/",
        "https://MyRes.Services.AI.Azure.Com",
        "https://myres.services.ai.azure.com/openai/v1",
    ])
    def test_foundry_hosts_infer_foundry(self, url):
        assert _resolve_azure_endpoint_mode(url, None) == "foundry"

    @pytest.mark.parametrize("url", [
        "https://myres.openai.azure.com",
        "https://myres.openai.azure.com/",
    ])
    def test_azure_openai_hosts_infer_azure_openai(self, url):
        assert _resolve_azure_endpoint_mode(url, None) == "azure_openai"

    def test_v1_path_infers_foundry_on_any_host(self):
        # An admin who pasted a v1 base has told us which surface they mean,
        # whatever the hostname (private DNS, gateway, Private Link).
        assert _resolve_azure_endpoint_mode("https://llm.corp.internal/openai/v1", None) == "foundry"

    def test_explicit_mode_overrides_inference(self):
        # The Private Link case: the hostname reveals nothing, so the admin's
        # choice has to beat the hostname heuristic in both directions.
        assert _resolve_azure_endpoint_mode("https://llm.corp.internal", "foundry") == "foundry"
        assert _resolve_azure_endpoint_mode(
            "https://myres.services.ai.azure.com", "azure_openai"
        ) == "azure_openai"

    def test_auto_and_junk_fall_back_to_inference(self):
        assert _resolve_azure_endpoint_mode("https://myres.services.ai.azure.com", "auto") == "foundry"
        assert _resolve_azure_endpoint_mode("https://myres.services.ai.azure.com", "") == "foundry"
        assert _resolve_azure_endpoint_mode("https://r.openai.azure.com", "nonsense") == "azure_openai"


class TestClientSelection:
    def test_foundry_endpoint_uses_openai_compatible_client(self):
        llm = LLM(_azure_model(endpoint_url="https://myres.services.ai.azure.com"))
        assert isinstance(llm.client, OpenAi)
        assert str(llm.client.client.base_url) == "https://myres.services.ai.azure.com/openai/v1/"

    def test_foundry_endpoint_pasted_as_v1_base_is_not_doubled(self):
        llm = LLM(_azure_model(endpoint_url="https://myres.services.ai.azure.com/openai/v1"))
        assert str(llm.client.client.base_url) == "https://myres.services.ai.azure.com/openai/v1/"

    @pytest.mark.parametrize("pasted", [
        # What the Foundry portal shows most prominently — the project endpoint,
        # not the inference root. Appending /openai/v1 to it produces a path
        # that exists nowhere, and APIM answers with a 401 that blames the key.
        "https://myres.services.ai.azure.com/api/projects/my-project",
        "https://myres.services.ai.azure.com/api/projects/my-project/",
        # The older Azure AI Inference route.
        "https://myres.services.ai.azure.com/models",
        # Stray whitespace from a copy-paste.
        "  https://myres.services.ai.azure.com  ",
    ])
    def test_portal_urls_normalize_to_the_inference_root(self, pasted):
        llm = LLM(_azure_model(endpoint_url=pasted))
        assert str(llm.client.client.base_url) == "https://myres.services.ai.azure.com/openai/v1/"

    def test_azure_openai_endpoint_still_uses_azure_client(self):
        # Regression guard: existing providers must not move off the Azure SDK.
        llm = LLM(_azure_model())
        assert isinstance(llm.client, AzureClient)

    def test_explicit_foundry_mode_routes_private_endpoint(self):
        llm = LLM(_azure_model(
            endpoint_url="https://llm.corp.internal", endpoint_mode="foundry"
        ))
        assert isinstance(llm.client, OpenAi)
        assert str(llm.client.client.base_url) == "https://llm.corp.internal/openai/v1/"

    def test_responses_opt_in_wins_on_both_surfaces(self):
        # Responses lives at the same v1 base on either surface, so the opt-in
        # keeps working — it is not silently dropped by the new Foundry branch.
        for url in ("https://r.openai.azure.com", "https://myres.services.ai.azure.com"):
            llm = LLM(_azure_model(endpoint_url=url, use_responses_api=True))
            assert isinstance(llm.client, OpenAIResponsesClient)

    def test_api_version_pin_reaches_azure_client(self):
        llm = LLM(_azure_model(api_version="2025-01-01-preview"))
        assert "2025-01-01-preview" in str(llm.client.client._api_version)

    def test_missing_endpoint_url_still_raises(self):
        model = _azure_model()
        model.provider.additional_config = {}
        with pytest.raises(ValueError, match="endpoint_url"):
            LLM(model)


class TestAnthropicOnFoundry:
    """Foundry fronts two APIs on one resource, split by model family.

    Anthropic deployments answer only on ``/anthropic/v1/messages``; the
    OpenAI-compatible surface rejects them with ``404 api_not_supported``.
    Verified against a live Foundry resource.
    """

    @pytest.mark.parametrize("model_id,expected", [
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-5", True),
        ("Claude-Opus-4-5", True),
        ("  claude-opus-5  ", True),
        # Renamed deployments still carry the family in practice.
        ("prod-claude-haiku", True),
        ("anthropic-fast", True),
        ("gpt-5.4-mini", False),
        ("Llama-3.3-70B-Instruct", False),
        ("DeepSeek-V3", False),
        ("", False),
        (None, False),
    ])
    def test_model_family_detection(self, model_id, expected):
        assert _is_anthropic_model_id(model_id) is expected

    @pytest.mark.parametrize("pasted", [
        "https://myres.services.ai.azure.com",
        "https://myres.services.ai.azure.com/",
        "https://myres.services.ai.azure.com/api/projects/my-project",
        "https://myres.services.ai.azure.com/openai/v1",
    ])
    def test_anthropic_base_url_normalization(self, pasted):
        # The Anthropic SDK appends its own /v1/messages, so the base must stop
        # at /anthropic — /anthropic/v1 yields /anthropic/v1/v1/messages, which
        # the endpoint answers with the same 404 as no routing at all.
        assert _azure_foundry_anthropic_base_url(pasted) == "https://myres.services.ai.azure.com/anthropic"

    def test_anthropic_deployment_uses_anthropic_client(self):
        llm = LLM(_azure_model(
            model_id="claude-haiku-4-5",
            endpoint_url="https://myres.services.ai.azure.com",
        ))
        assert isinstance(llm.client, Anthropic)
        assert str(llm.client.async_client.base_url).rstrip("/") == "https://myres.services.ai.azure.com/anthropic"

    def test_the_sdk_builds_the_url_that_actually_works(self):
        # Guards the /v1/v1 regression at the layer that produces the request.
        llm = LLM(_azure_model(
            model_id="claude-haiku-4-5",
            endpoint_url="https://myres.services.ai.azure.com",
        ))
        url = llm.client.async_client._prepare_url("/v1/messages")
        assert str(url) == "https://myres.services.ai.azure.com/anthropic/v1/messages"

    def test_openai_deployment_on_foundry_is_unaffected(self):
        llm = LLM(_azure_model(
            model_id="gpt-5.4-mini",
            endpoint_url="https://myres.services.ai.azure.com",
        ))
        assert isinstance(llm.client, OpenAi)

    def test_anthropic_model_on_azure_openai_is_not_rerouted(self):
        # Only Foundry serves the Anthropic surface. An Azure OpenAI resource
        # keeps the historical client whatever the deployment is named.
        llm = LLM(_azure_model(
            model_id="claude-haiku-4-5",
            endpoint_url="https://r.openai.azure.com",
        ))
        assert isinstance(llm.client, AzureClient)


class TestAnthropicClientBaseUrl:
    """base_url was accepted and silently dropped, pinning every caller to
    api.anthropic.com — the exact hook Foundry routing needs."""

    def test_base_url_is_honored(self):
        c = Anthropic(api_key="k", base_url="https://myres.services.ai.azure.com/anthropic")
        assert str(c.client.base_url).rstrip("/") == "https://myres.services.ai.azure.com/anthropic"
        assert str(c.async_client.base_url).rstrip("/") == "https://myres.services.ai.azure.com/anthropic"

    def test_default_is_unchanged_when_omitted(self):
        c = Anthropic(api_key="k")
        assert "api.anthropic.com" in str(c.async_client.base_url)
