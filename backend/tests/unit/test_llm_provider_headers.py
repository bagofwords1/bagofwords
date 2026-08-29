"""Unit tests for LLM provider custom headers + identity forwarding + Entra auth.

Covers the pure header-resolution module, the LLM facade wiring headers into
each client (verified on the constructed SDK clients — no network), and the
Azure Entra ID auth-mode plumbing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.ai.llm.header_injection import (
    build_provider_headers,
    reset_llm_identity,
    set_llm_identity,
    validate_header_config,
)
from app.ai.llm.llm import LLM
from app.services.mcp_context_injection import IdentityContext
from app.settings.config import settings


class FakeProvider(SimpleNamespace):
    """Duck-typed LLMProvider: enough surface for LLM.__init__."""

    def decrypt_credentials(self):
        fernet = Fernet(settings.bow_config.encryption_key)
        return (
            json.loads(fernet.decrypt(self.api_key.encode()).decode()),
            json.loads(fernet.decrypt(self.api_secret.encode()).decode()),
        )


def make_model(provider_type: str, additional_config: dict, api_key="sk-test", api_secret=None):
    fernet = Fernet(settings.bow_config.encryption_key)
    provider = FakeProvider(
        provider_type=provider_type,
        additional_config=additional_config,
        api_key=fernet.encrypt(json.dumps(api_key).encode()).decode(),
        api_secret=fernet.encrypt(json.dumps(api_secret).encode()).decode(),
    )
    return SimpleNamespace(
        model_id="test-model",
        provider=provider,
        organization_id="org-1",
        config=None,
        supports_vision=False,
    )


@pytest.fixture
def identity():
    token = set_llm_identity(
        IdentityContext(
            email="analyst@corp.com",
            name="Ada Analyst",
            user_id="u-123",
            role="admin",
            attributes={"department": "Finance", "employeeId": "E42"},
        )
    )
    yield
    reset_llm_identity(token)


# ---------------------------------------------------------------------------
# build_provider_headers
# ---------------------------------------------------------------------------

def test_static_headers_only():
    assert build_provider_headers({"headers": {"X-Team": "data"}}) == {"X-Team": "data"}


def test_injection_without_identity_is_omitted():
    cfg = {"header_injection": [{"header": "X-User-Email", "source": "user.email"}]}
    assert build_provider_headers(cfg) == {}


def test_injection_resolves_identity(identity):
    cfg = {
        "headers": {"X-Static": "s"},
        "header_injection": [
            {"header": "X-User-Email", "source": "user.email"},
            {"header": "X-Role", "source": "membership.role"},
            {"header": "X-Dept", "source": "membership.attr:department"},
            {"header": "X-Composite", "source": "static:corp\\{membership.attr:employeeId}"},
            {"header": "X-Missing", "source": "membership.attr:nope"},
        ],
    }
    assert build_provider_headers(cfg) == {
        "X-Static": "s",
        "X-User-Email": "analyst@corp.com",
        "X-Role": "admin",
        "X-Dept": "Finance",
        "X-Composite": "corp\\E42",
    }


def test_dynamic_wins_over_static(identity):
    cfg = {
        "headers": {"X-User-Email": "static@corp.com"},
        "header_injection": [{"header": "X-User-Email", "source": "user.email"}],
    }
    assert build_provider_headers(cfg) == {"X-User-Email": "analyst@corp.com"}


def test_header_value_newlines_stripped():
    token = set_llm_identity(IdentityContext(email="a@b.com\r\nX-Evil: 1"))
    try:
        cfg = {"header_injection": [{"header": "X-User-Email", "source": "user.email"}]}
        resolved = build_provider_headers(cfg)["X-User-Email"]
        assert "\r" not in resolved and "\n" not in resolved
        assert resolved.startswith("a@b.com")
    finally:
        reset_llm_identity(token)


def test_invalid_header_names_dropped_at_build():
    cfg = {"headers": {"Bad Name": "x", "X-Ok": "y"}}
    assert build_provider_headers(cfg) == {"X-Ok": "y"}


def test_empty_config():
    assert build_provider_headers(None) == {}
    assert build_provider_headers({}) == {}


# ---------------------------------------------------------------------------
# validate_header_config (storage-time validation)
# ---------------------------------------------------------------------------

def test_validate_normalizes():
    headers, rules = validate_header_config(
        {"X-Ok": " v ", "": "skip", "X-Empty": ""},
        [{"header": "X-U", "source": "user.email"}, {"header": "", "source": ""}],
    )
    assert headers == {"X-Ok": "v"}
    assert rules == [{"header": "X-U", "source": "user.email"}]


def test_validate_rejects_bad_name():
    with pytest.raises(ValueError, match="Invalid header name"):
        validate_header_config({"Bad Name": "1"}, None)
    with pytest.raises(ValueError, match="Invalid header name"):
        validate_header_config(None, [{"header": "X:Y", "source": "user.email"}])


def test_validate_rejects_missing_source():
    with pytest.raises(ValueError, match="no source"):
        validate_header_config(None, [{"header": "X-U", "source": ""}])


def test_validate_rejects_too_many():
    with pytest.raises(ValueError, match="Too many"):
        validate_header_config({f"X-{i}": "v" for i in range(30)}, None)


# ---------------------------------------------------------------------------
# LLM facade → client wiring
# ---------------------------------------------------------------------------

HEADER_CFG = {
    "headers": {"X-Static": "s"},
    "header_injection": [{"header": "X-User-Email", "source": "user.email"}],
}


def test_custom_provider_headers_reach_openai_clients(identity):
    model = make_model("custom", {"base_url": "http://localhost:9/v1", **HEADER_CFG})
    llm = LLM(model)
    for client in (llm.client.client, llm.client.async_client):
        headers = dict(client.default_headers)
        assert headers["X-Static"] == "s"
        assert headers["X-User-Email"] == "analyst@corp.com"


def test_openai_provider_headers_reach_responses_client(identity):
    model = make_model("openai", dict(HEADER_CFG))
    llm = LLM(model)
    headers = dict(llm.client.client.default_headers)
    assert headers["X-Static"] == "s"
    assert headers["X-User-Email"] == "analyst@corp.com"


def test_anthropic_provider_headers(identity):
    model = make_model("anthropic", dict(HEADER_CFG))
    llm = LLM(model)
    headers = dict(llm.client.client.default_headers)
    assert headers["X-Static"] == "s"
    assert headers["X-User-Email"] == "analyst@corp.com"


def test_azure_api_key_headers(identity):
    model = make_model(
        "azure", {"endpoint_url": "https://res.openai.azure.com", **HEADER_CFG}
    )
    llm = LLM(model)
    headers = dict(llm.client.client.default_headers)
    assert headers["X-Static"] == "s"
    assert headers["X-User-Email"] == "analyst@corp.com"


def test_no_headers_config_leaves_clients_clean():
    model = make_model("custom", {"base_url": "http://localhost:9/v1"})
    llm = LLM(model)
    headers = dict(llm.client.client.default_headers)
    assert "X-Static" not in headers


def test_bedrock_extra_headers_injected_on_request():
    from botocore.awsrequest import AWSRequest

    model = make_model(
        "bedrock", {"region": "us-east-1", "auth_mode": "api_key", **HEADER_CFG},
        api_key="bedrock-key",
    )
    token = set_llm_identity(IdentityContext(email="analyst@corp.com"))
    try:
        llm = LLM(model)
    finally:
        reset_llm_identity(token)
    request = AWSRequest(method="POST", url="https://bedrock-runtime.us-east-1.amazonaws.com/")
    llm.client.client.meta.events.emit(
        "request-created.bedrock-runtime.Converse", request=request, operation_name="Converse"
    )
    assert request.headers["X-Static"] == "s"
    assert request.headers["X-User-Email"] == "analyst@corp.com"
    # Bearer auth from api_key mode must survive alongside custom headers.
    assert request.headers["Authorization"] == "Bearer bedrock-key"


# ---------------------------------------------------------------------------
# Azure Entra ID auth modes
# ---------------------------------------------------------------------------

def test_azure_entra_client_secret_builds_token_provider(identity):
    model = make_model(
        "azure",
        {
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_client_secret",
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "client_id": "22222222-2222-2222-2222-222222222222",
            **HEADER_CFG,
        },
        api_key=None,
        api_secret="entra-client-secret",
    )
    llm = LLM(model)
    from app.ai.llm.clients.azure_client import AzureClient

    assert isinstance(llm.client, AzureClient)
    headers = dict(llm.client.client.default_headers)
    assert headers["X-User-Email"] == "analyst@corp.com"


def test_azure_entra_client_secret_requires_fields():
    model = make_model(
        "azure",
        {
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_client_secret",
            "tenant_id": "t",
            # client_id missing
        },
        api_key=None,
        api_secret="secret",
    )
    with pytest.raises(ValueError, match="entra_client_secret"):
        LLM(model)


def test_azure_entra_default_no_stored_credentials():
    fernet_free = FakeProvider(
        provider_type="azure",
        additional_config={
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_default",
        },
        api_key=None,
        api_secret=None,
    )
    model = SimpleNamespace(
        model_id="gpt-4o", provider=fernet_free, organization_id="org-1",
        config=None, supports_vision=False,
    )
    llm = LLM(model)  # must not raise on decrypt failure
    from app.ai.llm.clients.azure_client import AzureClient

    assert isinstance(llm.client, AzureClient)


def test_azure_entra_ignores_responses_api(identity):
    model = make_model(
        "azure",
        {
            "endpoint_url": "https://res.openai.azure.com",
            "auth_mode": "entra_client_secret",
            "tenant_id": "t",
            "client_id": "c",
            "use_responses_api": True,
        },
        api_key=None,
        api_secret="secret",
    )
    llm = LLM(model)
    from app.ai.llm.clients.azure_client import AzureClient

    assert isinstance(llm.client, AzureClient)
