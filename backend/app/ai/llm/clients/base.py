from abc import ABC, abstractmethod
from typing import Optional

from app.ai.llm.types import LLMUsage, ImageInput


class LLMClient(ABC):
    def __init__(self):
        self._last_usage = LLMUsage()

    @abstractmethod
    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None):
        pass

    @abstractmethod
    def inference_stream(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None):
        pass

    def _set_last_usage(self, usage: LLMUsage):
        self._last_usage = usage or LLMUsage()

    def pop_last_usage(self) -> LLMUsage:
        usage = self._last_usage
        self._last_usage = LLMUsage()
        return usage
