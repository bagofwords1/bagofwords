from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from app.ai.llm.types import (
    ImageInput,
    ImageOutput,
    LLMStreamEvent,
    LLMUsage,
    Message,
    ToolSpec,
)


class LLMClient(ABC):
    def __init__(self):
        self._last_usage = LLMUsage()

    @abstractmethod
    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None):
        pass

    @abstractmethod
    def inference_stream(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None):
        pass

    async def inference_stream_v2(
        self,
        model_id: str,
        messages: list[Message],
        system: Optional[str] = None,
        tools: Optional[list[ToolSpec]] = None,
        images: Optional[list[ImageInput]] = None,
        thinking: Optional[dict] = None,
        disable_parallel_tools: bool = True,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Streaming inference with native tool_use support.

        Default raises NotImplementedError so providers can opt in incrementally.
        Yields :class:`LLMStreamEvent` instances.

        ``thinking`` is a provider-shaped dict opting into extended/adaptive
        thinking. Only Anthropic honors it for now; other providers receive
        the kwarg but ignore it. Shapes:
          - {"type": "adaptive"}                          # Anthropic 4.6+
          - {"type": "enabled", "budget_tokens": 5000}    # explicit budget
        ``display`` defaults to "summarized" so the UI gets readable text.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement inference_stream_v2"
        )
        # Make this an async generator for static type checkers
        if False:  # pragma: no cover
            yield  # type: ignore[misc]

    async def generate_image(
        self,
        model_id: str,
        prompt: str,
        *,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        images: Optional[list[ImageInput]] = None,
    ) -> ImageOutput:
        """Generate an image from a text prompt (and optional reference images).

        Default raises NotImplementedError so only image-capable providers opt in
        (mirrors ``inference_stream_v2``). ``images`` enables image-to-image /
        edit flows on providers that support them. Returns an :class:`ImageOutput`
        carrying base64 bytes; the caller persists it as a File.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement generate_image"
        )

    def _set_last_usage(self, usage: LLMUsage):
        self._last_usage = usage or LLMUsage()

    def pop_last_usage(self) -> LLMUsage:
        usage = self._last_usage
        self._last_usage = LLMUsage()
        return usage
