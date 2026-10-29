"""Local-only streaming provider boundary for UI QA; never contacts a model."""

import asyncio
import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.post("/v1/chat/completions")
async def completion():
    async def events():
        for chunk in (
            "Key points: ",
            "the document describes ",
            "a weekly planning process. ",
            "Review open actions, ",
            "assign owners, ",
            "and save decisions for follow-up.",
        ):
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": "local-fixture",
                        "object": "chat.completion.chunk",
                        "model": "local-fixture",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": chunk},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                + "\n\n"
            )
            await asyncio.sleep(0.3)
        yield (
            "data: "
            + json.dumps(
                {
                    "id": "local-fixture",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 30,
                        "total_tokens": 70,
                    },
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
