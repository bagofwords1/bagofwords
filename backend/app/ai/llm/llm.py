from typing import List, Tuple, AsyncGenerator

from .clients.openai_client import OpenAi
from .clients.google_client import Google
from .clients.anthropic_client import Anthropic
from .clients.bow_client import Bow
from .clients.azure_client import AzureClient
from app.models.llm_model import LLMModel

class LLM:
    def __init__(self, model: LLMModel):
        self.model = model
        self.model_id = model.model_id
        self.provider = model.provider.provider_type
        self.api_key = self.model.provider.decrypt_credentials()[0]


        if self.provider == "openai":
            base_url = None
            if self.model.provider.additional_config:
                base_url = self.model.provider.additional_config.get("base_url")
            self.client = OpenAi(api_key=self.api_key, base_url=base_url or "https://api.openai.com/v1")
        elif self.provider == "anthropic":
            self.client = Anthropic(api_key=self.api_key)
        elif self.provider == "google":
            self.client = Google()
        elif self.provider == "bow":
            self.client = Bow(api_key=self.api_key)
        elif self.provider == "azure":
            # Get endpoint URL from provider's additional_config
            endpoint_url = self.model.provider.additional_config.get("endpoint_url") if self.model.provider.additional_config else None
            if not endpoint_url:
                raise ValueError("Azure provider requires endpoint_url in additional_config")
            self.client = AzureClient(api_key=self.api_key, endpoint_url=endpoint_url)
        else:
            raise ValueError(f"Provider {self.provider} not supported")
 
    def inference(self, prompt: str) -> str:
        print(f"Model: {self.model_id}, prompt: {prompt}")
        try:
            response = self.client.inference(model_id=self.model_id, prompt=prompt)
        except Exception as e:
            raise RuntimeError(f"LLM inference failed (provider={self.provider}, model={self.model_id}): {e}") from e
        print(f"response: {response}")
        try:
            response = response.replace("```json", "").replace("```", "")
            response = response.replace("```python", "").replace("```", "")
        except Exception:
            # If response is not a string, rethrow with context
            raise RuntimeError("LLM inference returned a non-string response that could not be sanitized")
        return response
    
    async def inference_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        print(f"Model: {self.model_id}, prompt: {prompt}")
        import re
        started_payload = False
        prefix = ""
        try:
            async for chunk in self.client.inference_stream(model_id=self.model_id, prompt=prompt):
                # Skip None/heartbeat or empty chunks before payload [[memory:5773628]]
                if chunk is None:
                    continue
                if not isinstance(chunk, str):
                    try:
                        chunk = str(chunk)
                    except Exception:
                        continue

                # Strip any fence closers anywhere
                if "```" in chunk:
                    chunk = chunk.replace("```", "")

                if not started_payload:
                    # Accumulate prelude and strip language fences/tags even if split across chunks
                    prefix += chunk

                    # Remove leading opening fences like ```json, ```python, or plain ```
                    prefix = re.sub(r"^\s*```(?:[A-Za-z]+)?\s*", "", prefix)

                    # Remove leading language tag (json/python) with optional newline(s)
                    prefix = re.sub(r"^\s*(?:json|JSON|python|PYTHON)\s*\r?\n", "", prefix)

                    # If buffer is only a language tag fragment (no newline yet), keep waiting
                    if re.fullmatch(r"\s*(?:json|JSON|python|PYTHON)\s*", prefix or ""):
                        continue

                    # Trim remaining leading whitespace
                    prefix = re.sub(r"^\s+", "", prefix)

                    # Find first JSON start and begin streaming from there
                    m = re.search(r"[\{\[]", prefix)
                    if not m:
                        # Still haven't seen JSON start; keep accumulating
                        continue

                    started_payload = True
                    yield prefix[m.start():]
                    prefix = ""  # release buffer
                else:
                    # After payload starts, still guard against stray closers mid-stream
                    if "```" in chunk:
                        chunk = chunk.replace("```", "")
                    yield chunk
        except Exception as e:
            raise RuntimeError(f"LLM streaming failed (provider={self.provider}, model={self.model_id}): {e}") from e