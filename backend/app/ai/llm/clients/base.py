from abc import ABC, abstractmethod

from app.ai.llm.types import LLMUsage


class LLMClient(ABC):
    def __init__(self):
        self._last_usage = LLMUsage()

    @abstractmethod
    def inference(self, prompt: str):
        pass

    @abstractmethod
    def inference_stream(self, prompt: str):
        pass

    def _set_last_usage(self, usage: LLMUsage):
        self._last_usage = usage or LLMUsage()

    def pop_last_usage(self) -> LLMUsage:
        usage = self._last_usage
        self._last_usage = LLMUsage()
        return usage
