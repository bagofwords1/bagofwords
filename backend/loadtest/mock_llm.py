#!/usr/bin/env python3
"""
OpenAI-compatible mock LLM for load testing.

Why this exists: at 60-100 concurrent completions, real Anthropic calls
contribute rate-limit 429s and multi-second latency variance that *dominate*
the measurement. You end up measuring the provider, not BoW. This server
replaces the provider with a deterministic one that has a *realistic latency
shape* (time-to-first-token + per-token cadence), so every concurrency level
sees an identical LLM and the only thing that varies is BoW's own contention.

Wired into the app as a `custom` LLM provider with
`additional_config.base_url = http://127.0.0.1:9001/v1`, which routes through
`app/ai/llm/clients/openai_client.py` (Chat Completions + streaming).

Agent loop it drives (matches the real shape of a planner turn):
  iteration 1..N : assistant text  ->  create_note tool call   (finish=tool_calls)
  iteration N+1  : assistant text  ->  end                     (finish=stop)

`create_note` is deliberately chosen: 2 string args, exercises the real
tool-execution + completion-block + streaming-event + DB-write path, but
spawns no nested sub-agent (which would make LLM call counts non-deterministic).

Self-instrumentation: /stats reports in-flight and peak concurrency, so the
report can prove the mock never became the bottleneck it was meant to remove.
"""
import asyncio
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()

# --- Latency model -----------------------------------------------------------
# Defaults approximate Claude Haiku on a short planner turn: ~0.6s to first
# token, then ~60 tok at ~12ms/tok => ~1.3s per call, ~3 calls per completion.
TTFT_MS = float(os.getenv("MOCK_LLM_TTFT_MS", "600"))
TOKEN_MS = float(os.getenv("MOCK_LLM_TOKEN_MS", "12"))
N_TOKENS = int(os.getenv("MOCK_LLM_TOKENS", "60"))
TOOL_ITERS = int(os.getenv("MOCK_LLM_TOOL_ITERS", "2"))
# Optional per-request trace, used to verify every completion asked the LLM to
# do the same amount of work at every concurrency level. Without this, a
# slowdown at L100 could be extra agent iterations rather than contention.
_TRACE = os.getenv("MOCK_LLM_TRACE", "")

# --- Self-instrumentation ----------------------------------------------------
_stats = {
    "requests": 0,
    "in_flight": 0,
    "peak_in_flight": 0,
    "completed": 0,
    "total_latency_s": 0.0,
    # Slow-path counter: how often the mock's own scheduling drifted past the
    # latency it was asked to emit. Non-zero here means the mock is saturated
    # and its numbers are suspect.
    "overrun": 0,
}
_lock = asyncio.Lock()

FILLER = (
    "Looking at the request now. I will check the available tables, note the "
    "plan, and then summarise what the data shows for the requested period. "
    "This keeps a record of the reasoning so later turns can build on it. "
)
_WORDS = FILLER.split()


def _tokens(n: int) -> list[str]:
    """Deterministic token stream — same content for every call at every level."""
    return [_WORDS[i % len(_WORDS)] + " " for i in range(n)]


def _count_prior_tool_calls(messages: list) -> int:
    """How many assistant tool-call turns are already in this conversation.

    Drives loop termination without any server-side session state, so the mock
    stays stateless and safe to run multi-worker.
    """
    n = 0
    for m in messages or []:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            n += len(m["tool_calls"])
    return n


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _chunk(cid: str, model: str, delta: dict, finish=None) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


@app.get("/stats")
async def stats():
    s = dict(_stats)
    s["mean_latency_s"] = (
        s["total_latency_s"] / s["completed"] if s["completed"] else 0.0
    )
    return JSONResponse(s)


@app.post("/stats/reset")
async def stats_reset():
    # Preserve in_flight: zeroing it while requests are still open (the UI
    # cohort keeps running across a level boundary) makes their decrements
    # drive the counter negative and understates peak concurrency.
    inflight = _stats["in_flight"]
    for k in _stats:
        _stats[k] = 0 if k != "total_latency_s" else 0.0
    _stats["in_flight"] = inflight
    _stats["peak_in_flight"] = inflight
    return JSONResponse({"ok": True})


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-fast", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "mock-fast")
    messages = body.get("messages", [])
    tools = body.get("tools") or []
    stream = body.get("stream", False)

    async with _lock:
        _stats["requests"] += 1
        _stats["in_flight"] += 1
        _stats["peak_in_flight"] = max(_stats["peak_in_flight"], _stats["in_flight"])

    t0 = time.monotonic()
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    prior = _count_prior_tool_calls(messages)
    tool_names = {t.get("function", {}).get("name") for t in tools}
    if _TRACE:
        with open(_TRACE, "a") as fh:
            fh.write(json.dumps({
                "t": time.time(), "prior": prior, "n_messages": len(messages),
                "n_tools": len(tools), "offers_create_note": "create_note" in tool_names,
                "in_flight": _stats["in_flight"],
            }) + "\n")
    # Only drive the tool loop when the caller actually offers the tool we
    # emit. Sub-agents that call with no tools (or a different set) get plain
    # text, which is the correct shape for them.
    want_tool = ("create_note" in tool_names) and (prior < TOOL_ITERS)

    async def gen():
        try:
            await asyncio.sleep(TTFT_MS / 1000.0)
            yield _sse(_chunk(cid, model, {"role": "assistant", "content": ""}))

            deadline = time.monotonic()
            for tok in _tokens(N_TOKENS):
                deadline += TOKEN_MS / 1000.0
                nap = deadline - time.monotonic()
                if nap > 0:
                    await asyncio.sleep(nap)
                else:
                    _stats["overrun"] += 1
                yield _sse(_chunk(cid, model, {"content": tok}))

            if want_tool:
                tc_id = f"call_{uuid.uuid4().hex[:8]}"
                yield _sse(_chunk(cid, model, {"tool_calls": [{
                    "index": 0, "id": tc_id, "type": "function",
                    "function": {"name": "create_note", "arguments": ""},
                }]}))
                args = json.dumps({
                    "title": f"Load-test note {prior + 1}",
                    "content": (
                        f"- [x] Reviewed request (step {prior + 1})\n"
                        "- [ ] Summarise findings\n\n"
                        "Working note written by the load-test mock LLM."
                    ),
                })
                # Stream the args in fragments, like a real provider does —
                # this exercises the ToolUseInputDeltaEvent path, not just the
                # completed-call path.
                for i in range(0, len(args), 24):
                    await asyncio.sleep(TOKEN_MS / 1000.0)
                    yield _sse(_chunk(cid, model, {"tool_calls": [{
                        "index": 0,
                        "function": {"arguments": args[i:i + 24]},
                    }]}))
                yield _sse(_chunk(cid, model, {}, finish="tool_calls"))
            else:
                yield _sse(_chunk(cid, model, {}, finish="stop"))

            # Final usage chunk (stream_options.include_usage)
            yield _sse({
                "id": cid, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model, "choices": [],
                "usage": {
                    "prompt_tokens": 1200 + 40 * prior,
                    "completion_tokens": N_TOKENS,
                    "total_tokens": 1260 + 40 * prior,
                },
            })
            yield "data: [DONE]\n\n"
        finally:
            async with _lock:
                _stats["in_flight"] -= 1
                _stats["completed"] += 1
                _stats["total_latency_s"] += time.monotonic() - t0

    if stream:
        return StreamingResponse(gen(), media_type="text/event-stream")

    # Non-streaming fallback (some sub-agents use it)
    await asyncio.sleep(TTFT_MS / 1000.0 + N_TOKENS * TOKEN_MS / 1000.0)
    async with _lock:
        _stats["in_flight"] -= 1
        _stats["completed"] += 1
        _stats["total_latency_s"] += time.monotonic() - t0
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "".join(_tokens(N_TOKENS))
        }, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": N_TOKENS,
                  "total_tokens": 1260},
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("MOCK_LLM_PORT", "9001")),
                log_level="warning")
