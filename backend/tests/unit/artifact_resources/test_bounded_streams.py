"""Bounded provider streams must close on cancellation and reject truncation."""

from types import SimpleNamespace as NS
import pytest
from app.ai.llm.clients.openai_client import OpenAi
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient
from app.ai.llm.clients.azure_client import AzureClient
from app.ai.llm.clients.anthropic_client import Anthropic


class Transport:
    def __init__(self, chunks):
        self.chunks, self.closed, self.params = chunks, False, None
        self.chat = NS(completions=self)
        self.messages = self

    def with_options(self, **options):
        assert options["max_retries"] == 0
        return self

    async def create(self, **params):
        self.params = params
        return self

    def __aiter__(self):
        return self.events()

    async def events(self):
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "responses", "azure", "anthropic"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_bounded_stream_rejects_truncation_and_closes_transport(kind, cancel):
    if kind == "anthropic":
        client = Anthropic(api_key="local-test")
        chunks = [
            NS(type="content_block_delta", delta=NS(text="Partial output")),
            NS(type="message_delta", delta=NS(stop_reason="max_tokens")),
        ]
    else:
        if kind == "chat":
            client = OpenAi(api_key="local-test")
        elif kind == "responses":
            client = OpenAIResponsesClient(api_key="local-test")
        else:
            client = AzureClient(api_key="local-test", endpoint_url="https://local-test.openai.azure.com")
        chunks = [
            NS(choices=[NS(delta=NS(content="Partial output"), finish_reason=None)]),
            NS(choices=[NS(delta=NS(content=None), finish_reason="length")]),
        ]
    transport = Transport(chunks)
    client.async_client = transport  # Only the external SDK transport is replaced.
    stream = client.inference_stream("test-model", "Synthetic prompt", max_output_tokens=128)
    assert await anext(stream) == "Partial output"
    if cancel:
        await stream.aclose()
    else:
        with pytest.raises((ValueError, RuntimeError)):
            await anext(stream)
    assert transport.closed
    assert transport.params.get("max_completion_tokens", transport.params.get("max_tokens")) == 128


@pytest.mark.asyncio
async def test_private_provider_logging_does_not_hide_other_concurrent_calls(caplog, monkeypatch):
    import asyncio
    import logging
    from app.ai.llm.private_stream import private_provider_logs

    logger = logging.getLogger("openai._base_client")
    # Migration logging configuration disables existing third-party loggers.
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    caplog.set_level(logging.WARNING, logger=logger.name)
    ready, release = asyncio.Event(), asyncio.Event()

    async def private_call():
        with private_provider_logs(True):
            logger.warning("private synthetic document content")
            ready.set()
            await release.wait()
            logger.warning("private synthetic completion content")

    task = asyncio.create_task(private_call())
    await ready.wait()
    logger.warning("ordinary diagnostic remains visible")
    release.set()
    await task
    assert "private synthetic" not in caplog.text
    assert "ordinary diagnostic remains visible" in caplog.text
