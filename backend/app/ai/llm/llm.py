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
            self.client = OpenAi(api_key=self.api_key)
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
        response = self.client.inference(model_id=self.model_id, prompt=prompt)
        print(f"response: {response}")
        return response
    
    async def inference_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        print(f"Model: {self.model_id}, prompt: {prompt}")
        async for chunk in self.client.inference_stream(model_id=self.model_id, prompt=prompt):
            yield chunk
