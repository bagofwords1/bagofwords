"""Building an LLM provider client must not build a new TLS context.

The agent builds a planner and a reporter client per message, each with a
sync and an async HTTP client. Loading the CA bundle for each one cost
~150-300 ms per message before the first model call. Clients now share one
process-wide context; verification stays on, and ``verify_ssl=False`` stays off.
"""
from __future__ import annotations

import ssl

import pytest


@pytest.fixture
def context_count(monkeypatch):
    calls = {"n": 0}
    real = ssl.create_default_context

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting)
    return calls


def _anthropic():
    from app.ai.llm.clients.anthropic_client import Anthropic
    return Anthropic(api_key="sk-test")


def _openai():
    from app.ai.llm.clients.openai_client import OpenAi
    return OpenAi(api_key="sk-test")


def _openai_custom_base():
    from app.ai.llm.clients.openai_client import OpenAi
    return OpenAi(api_key="sk-test", base_url="https://example.com/v1", timeout=30)


def _openai_responses():
    from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
    return OpenAIResponsesClient(api_key="sk-test")


@pytest.mark.parametrize("build", [_anthropic, _openai, _openai_custom_base, _openai_responses])
def test_repeated_client_construction_reuses_one_tls_context(build, context_count):
    build()  # the first may create the shared context
    before = context_count["n"]
    for _ in range(3):
        build()
    assert context_count["n"] == before


def _inner_transport(t):
    """The pooled transport behind a shared one (app.core.llm_transport)."""
    import asyncio
    from app.core.llm_transport import _SharedAsync, _SharedSync
    if isinstance(t, _SharedSync):
        return t.transport()
    if isinstance(t, _SharedAsync):
        async def _get():
            return t.transport()
        return asyncio.run(_get())
    return t


@pytest.mark.parametrize("build", [_anthropic, _openai, _openai_responses])
def test_shared_context_still_verifies_certificates(build):
    client = build()
    for http in (client.client._client, client.async_client._client):
        ctx = _inner_transport(http._transport)._pool._ssl_context
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True


def test_verify_ssl_false_is_still_honored():
    from app.ai.llm.clients.openai_client import OpenAi

    client = OpenAi(api_key="sk-test", base_url="https://self-signed.local/v1", verify_ssl=False)
    ctx = client.client._client._transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_NONE
