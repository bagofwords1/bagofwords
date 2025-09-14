from abc import ABC, abstractmethod

class LLMClient(ABC):

    def __init__(self):
        pass

    @abstractmethod
    def inference(self, prompt: str):
        pass

    @abstractmethod
    def inference_stream(self, prompt: str):
        pass
