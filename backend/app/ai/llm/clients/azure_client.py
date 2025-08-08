from openai import AsyncOpenAI, OpenAI
from typing import AsyncGenerator
from app.ai.llm.clients.base import LLMClient


class AzureClient(LLMClient):
    def __init__(self, api_key: str, endpoint_url: str):
        self.client = OpenAI(
            api_key=api_key, 
            base_url=f"{endpoint_url}/openai/deployments"
        )
        self.async_client = AsyncOpenAI(
            api_key=api_key, 
            base_url=f"{endpoint_url}/openai/deployments"
        )

    def inference(self, model_id: str, prompt: str) -> str:
        # For Azure, model_id is the deployment name
        temprature = 0.7
        if model_id == "gpt-5":
            temprature = 1

        chat_completion = self.client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            model=model_id,
            temperature=temprature
        )
        return chat_completion.choices[0].message.content
    
    async def inference_stream(self, model_id: str, prompt: str) -> AsyncGenerator[str, None]:
        # For Azure, model_id is the deployment name
        temprature = 0.7
        if model_id == "gpt-5":
            temprature = 1

        stream = await self.async_client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": prompt.strip(),
                }
            ],
            model=model_id,
            temperature=temprature,
            stream=True
        )
        
        async for chunk in stream:
            content = chunk.choices[0].delta.content
            if content is not None:
                yield content

    def test_connection(self):
        return True 