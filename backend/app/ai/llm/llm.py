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
        # Some providers (e.g., Anthropic) occasionally wrap JSON in markdown fences or a stray 'json' tag.
        # Sanitize streamed chunks to remove opening ```json, language-only tags like 'json\n', and any closing ```.
        first_chunk = True
        started_payload = False
        import re
        try:
            async for chunk in self.client.inference_stream(model_id=self.model_id, prompt=prompt):
                if first_chunk:
                    # Strip an opening fence like ```json, ```python or ```
                    chunk = re.sub(r"^\s*```(?:json|JSON|python|PYTHON)?\s*", "", chunk)
                    # Also handle cases where only a leading language tag remains (e.g., 'json\n')
                    chunk = re.sub(r"^\s*(?:json|JSON|python|PYTHON)\s*\n", "", chunk)
                    # If the chunk is only the language tag without newline yet (e.g., 'json'), skip it
                    if re.fullmatch(r"\s*(?:json|JSON|python|PYTHON)\s*", chunk or ""):
                        continue
                    first_chunk = False
                # Remove any stray fence closers mid-stream or at the end
                if "```" in chunk:
                    chunk = chunk.replace("```", "")
                # Before payload actually starts, tolerate a stray leading 'json\n' or 'python\n'
                if not started_payload:
                    orig = chunk
                    chunk = re.sub(r"^\s*(?:json|JSON|python|PYTHON)\s*\n", "", chunk)
                    # Mark payload as started once we see a likely JSON start character
                    if any(ch in chunk for ch in ['{', '[']):
                        started_payload = True
                    # If nothing left after removing the tag, skip yielding
                    if not chunk and orig:
                        continue
                yield chunk
        except Exception as e:
            raise RuntimeError(f"LLM streaming failed (provider={self.provider}, model={self.model_id}): {e}") from e
