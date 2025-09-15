from openai import AzureOpenAI, AsyncAzureOpenAI
from typing import AsyncGenerator
from app.ai.llm.clients.base import LLMClient


class AzureClient(LLMClient):
    def __init__(self, api_key: str, endpoint_url: str, api_version: str | None = None):
        # endpoint_url should be the Azure OpenAI resource endpoint, e.g. https://<resource>.openai.azure.com
        effective_api_version = api_version or "2024-10-21"
        self.client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint_url,
            api_version=effective_api_version,
        )
        self.async_client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint_url,
            api_version=effective_api_version,
        )

    def inference(self, model_id: str, prompt: str) -> str:
        # For Azure, model_id is the deployment (deployment name)
        temperature = 0.3
        if "gpt-5" in model_id:
            temperature = 1.0

        chat_completion = self.client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            model=model_id,
            temperature=temperature,
        )
        return chat_completion.choices[0].message.content
    
    async def inference_stream(self, model_id: str, prompt: str) -> AsyncGenerator[str, None]:
        # For Azure, model_id is the deployment (deployment name)
        temperature = 0.3
        if "gpt-5" in model_id:
            temperature = 1.0

        stream = await self.async_client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            model=model_id,
            temperature=temperature,
            stream=True
        )
        
        async for chunk in stream:
            if not chunk.choices:
                continue  # skip heartbeat/control packets
            
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    def test_connection(self):
        return True 