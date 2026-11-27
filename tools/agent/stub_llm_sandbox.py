#!/usr/bin/env python3
"""Scripted OpenAI-compatible stub LLM for the agent sandbox live run.

Stands in for a real model when none is available, so the whole path from
the agent loop to a real sandbox can still be exercised. It only decides
*which tool call to make*; everything after that is the real product.

Planner requests (body has `tools`):
  * call `run_in_sandbox` if it is offered, otherwise answer "SANDBOX TOOL
    NOT OFFERED". The arguments depend on the user's requests: the first one
    installs `cowsay` and writes a PNG, a request mentioning "still here
    after the stop" imports it again without installing anything.
  * once the newest tool result shows the expected output: answer
    "SANDBOX RESULT: <first 400 chars of it>".
Every planner request's tool names are appended to STUB_LOG (JSON lines).

    cd backend && STUB_PORT=9312 uv run --no-sync python ../tools/agent/stub_llm_sandbox.py

Register it with setup_stub_llm.py --stub-url http://127.0.0.1:9312/v1.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LOG = os.environ.get("STUB_LOG", "/tmp/bow-agent/stub_llm_sandbox.jsonl")
RESUME_MARK = "still here after the stop"
FIRST_MARK = "hello from the sandbox"
MAX_CALLS = 4  # never loop forever when a run keeps failing

FIRST_RUN = {
    "language": "python",
    "packages": ["cowsay", "matplotlib"],
    "code": (
        "import os\n"
        "import cowsay\n"
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "print(cowsay.get_output_string('cow', 'hello from the sandbox'))\n"
        "os.makedirs('/workspace/outputs', exist_ok=True)\n"
        "plt.bar(['a', 'b', 'c'], [3, 7, 5])\n"
        "plt.savefig('/workspace/outputs/chart.png')\n"
        "print('saved chart.png')\n"
    ),
}
RESUME_RUN = {
    "language": "python",
    "code": "import cowsay\nprint(cowsay.get_output_string('cow', 'still here after the stop'))\n",
}

app = FastAPI()


def _chunk(delta: dict, finish=None) -> str:
    return "data: " + json.dumps({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
        "created": int(time.time()), "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **({"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}} if finish else {}),
    }) + "\n\n"


def _reply(content: str | None, call: dict | None, stream: bool):
    if not stream:
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content or ""},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })

    def gen():
        yield _chunk({"role": "assistant"})
        if content:
            yield _chunk({"content": content})
        if call:
            yield _chunk({"tool_calls": [{"index": 0, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                          "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}]})
        yield _chunk({}, finish="tool_calls" if call else "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text")
    return "\n".join(parts)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if not body.get("tools"):
        return _reply("ok", None, stream)

    names = [(t.get("function") or {}).get("name") or t.get("name") for t in body["tools"]]
    messages = body.get("messages", [])
    results = []
    for m in messages:
        if m.get("role") == "tool":
            c = m.get("content")
            results.append(c if isinstance(c, str) else json.dumps(c))
    resume = RESUME_MARK in _text({"messages": [m for m in messages if m.get("role") == "user"]})
    # The turn is over once the newest tool result carries the output the
    # current request asked for. History holds earlier turns' results and the
    # planner adds its own user-role notes, so counting messages is not enough.
    wanted = RESUME_MARK if resume else FIRST_MARK
    done = bool(results) and wanted in results[-1]
    with open(LOG, "a") as f:
        f.write(json.dumps({"tools": names, "tool_results": len(results), "resume": resume, "done": done}) + "\n")

    if done or len(results) >= MAX_CALLS:
        return _reply(f"SANDBOX RESULT: {results[-1][:400]}", None, stream)
    if "run_in_sandbox" not in names:
        return _reply("SANDBOX TOOL NOT OFFERED", None, stream)
    return _reply(None, {"name": "run_in_sandbox", "arguments": RESUME_RUN if resume else FIRST_RUN}, stream)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9312")), log_level="warning")
