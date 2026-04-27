from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from app.ai.llm.types import (
    ImageInput,
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

    def _set_last_usage(self, usage: LLMUsage):
        self._last_usage = usage or LLMUsage()

    def pop_last_usage(self) -> LLMUsage:
        usage = self._last_usage
        self._last_usage = LLMUsage()
        return usage
