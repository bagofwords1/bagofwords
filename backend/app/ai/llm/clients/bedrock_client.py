import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, Optional

import boto3

from app.ai.llm.clients.base import LLMClient
from app.ai.llm.types import LLMResponse, LLMUsage, ImageInput

_STREAM_EXECUTOR = ThreadPoolExecutor(max_workers=4)


# Map MIME types to Bedrock image format strings
_MIME_TO_FORMAT = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


class BedrockClient(LLMClient):
    """
    AWS Bedrock client using the native Converse API (boto3).

    Auth: IAM only — uses the standard AWS credential chain
    (IRSA, env vars, instance role, etc.)

    Supports application inference profiles — pass the profile ARN as model_id.
    """

    # TODO: Add api_key auth mode support.
    # When boto3 supports passing Bedrock API keys as a client parameter
    # (see https://github.com/boto/boto3/issues/4723), add api_key auth here.
    # Current workaround would be os.environ["AWS_BEARER_TOKEN_BEDROCK"] = api_key
    # but that's process-global and unsafe for multi-tenant setups.

    def __init__(self, region: str, auth_mode: str = "iam", api_key: Optional[str] = None):
        super().__init__()
        if auth_mode != "iam":
            raise ValueError(
                f"Unsupported auth_mode '{auth_mode}'. Only 'iam' is currently supported."
            )

        self.client = boto3.client("bedrock-runtime", region_name=region)
        self._region = region
        self._auth_mode = auth_mode

    @staticmethod
    def _build_content(prompt: str, images: Optional[list[ImageInput]] = None) -> list[dict]:
        """Build Bedrock message content blocks."""
        content: list[dict] = []

        if images:
            for img in images:
                if img.source_type == "url":
                    # Bedrock converse only supports bytes/S3 for images, skip URLs
                    continue
                fmt = _MIME_TO_FORMAT.get(img.media_type, "png")
                image_bytes = base64.b64decode(img.data)
                content.append({
                    "image": {
                        "format": fmt,
                        "source": {"bytes": image_bytes},
                    }
                })

        content.append({"text": prompt.strip()})
        return content

    def inference(self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None) -> LLMResponse:
        response = self.client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": self._build_content(prompt, images)}],
        )

        # Extract text from response
        output_message = response["output"]["message"]
        text = ""
        for block in output_message.get("content", []):
            if "text" in block:
                text += block["text"]

        # Extract usage
        usage_data = response.get("usage", {})
        usage = LLMUsage(
            prompt_tokens=usage_data.get("inputTokens", 0),
            completion_tokens=usage_data.get("outputTokens", 0),
        )
        self._set_last_usage(usage)
        return LLMResponse(text=text, usage=usage)

    async def inference_stream(
        self, model_id: str, prompt: str, images: Optional[list[ImageInput]] = None
    ) -> AsyncGenerator[str, None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        usage_holder: dict = {"inputTokens": 0, "outputTokens": 0}

        def _sync_stream():
            """Run the blocking boto3 stream in a worker thread."""
            response = self.client.converse_stream(
                modelId=model_id,
                messages=[{"role": "user", "content": self._build_content(prompt, images)}],
            )
            for event in response["stream"]:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    text = delta.get("text")
                    if text:
                        loop.call_soon_threadsafe(queue.put_nowait, text)

                if "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    usage_holder["inputTokens"] = usage.get("inputTokens", usage_holder["inputTokens"])
                    usage_holder["outputTokens"] = usage.get("outputTokens", usage_holder["outputTokens"])

            # Signal end of stream
            loop.call_soon_threadsafe(queue.put_nowait, None)

        future = loop.run_in_executor(_STREAM_EXECUTOR, _sync_stream)

        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

        # Ensure the thread has finished and propagate any exceptions
        await future

        self._set_last_usage(
            LLMUsage(
                prompt_tokens=usage_holder["inputTokens"],
                completion_tokens=usage_holder["outputTokens"],
            )
        )
