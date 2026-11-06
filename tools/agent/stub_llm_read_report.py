#!/usr/bin/env python3
"""Scripted OpenAI-compatible stub planner for verifying read_report pagination.

It plays an agent asked to recall facts from an old, long report. Each planner
round it decides the next read_report call ONLY from what the real planner
prompt shows it (the rendered tool observations) — so if pagination fields
(page.prev_offset, message index/truncated/next_content_offset) don't reach
the model, it cannot make progress and the loop fails.

Strategy, like a careful agent:
  1. read_report(report_id)                      -> latest page
  2. a message came back truncated              -> read it on with content_offset
  3. else earlier messages exist                -> read_report(offset=prev_offset)
  4. all facts found / nothing left             -> final answer

Env: STUB_REPORT_ID (required), STUB_PORT (9099), STUB_DUMP (jsonl of planner prompts)
Run: cd backend && STUB_REPORT_ID=... uv run python ../tools/agent/stub_llm_read_report.py
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
REPORT_ID = os.environ.get("STUB_REPORT_ID", "")
DUMP = os.environ.get("STUB_DUMP")
MAX_READS = 12
FACTS = {"codename": r"BLUEHERON", "threshold": r"17\.4%", "discount": r"12\.5% retention discount to tier GAMMA"}


def _all_text(body):
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text"]
        for tc in m.get("tool_calls") or []:
            parts.append(json.dumps(tc.get("function", {})))
    return "\n".join(parts)


def _num(field, seg):
    m = re.findall(rf'"?{field}"?\s*[:=]\s*(-?\d+|null|None)', seg)
    if not m or m[-1] in ("null", "None"):
        return None
    return int(m[-1])


def _decide(text):
    reads = len(re.findall(r'"name":\s*"read_report"', text))
    # Fact search only over read_report results — the user's question names them too.
    obs_start = text.find("read_report", text.find("<past_observations") if "<past_observations" in text else 0)
    seen = text[obs_start:] if obs_start >= 0 else ""
    found = {k: bool(re.search(p, seen)) for k, p in FACTS.items()}
    if all(found.values()):
        return None, (
            "From your report: codename **BLUEHERON**, churn threshold **17.4%** (set in the very "
            "first message), and the final recommendation was a **12.5% retention discount for tier GAMMA**."
        )
    if reads == 0:
        return {"report_id": REPORT_ID}, "Reading the report's latest page."
    if reads >= MAX_READS:
        missing = [k for k, v in found.items() if not v]
        return None, f"STUB GAVE UP after {reads} reads; could not find: {missing}"
    # Look at the LAST read_report observation only.
    last = text[text.rfind("read_report"):]
    trunc = re.search(r'"index"\s*:\s*(\d+)[^{}]*?"truncated"\s*:\s*true[^{}]*?"next_content_offset"\s*:\s*(\d+)', last)
    if trunc and not found["discount"]:
        return ({"report_id": REPORT_ID, "offset": int(trunc.group(1)), "limit": 1,
                 "max_content_chars": 8000, "content_offset": int(trunc.group(2))},
                f"Message {trunc.group(1)} was truncated — reading the rest of it.")
    prev = _num("prev_offset", last)
    if prev is not None:
        # After a single-message read, resume paging below the lowest page seen.
        offsets = [int(x) for x in re.findall(r'"offset"\s*:\s*(\d+)', text)]
        nxt = prev if '"limit": 1' not in last else max(0, min(offsets) - 20)
        return {"report_id": REPORT_ID, "offset": nxt, "limit": 20}, f"Paging back to messages from {nxt}."
    missing = [k for k, v in found.items() if not v]
    return None, f"STUB REACHED THE START without finding: {missing}"


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _stream(content, tool_args):
    def gen():
        yield f"data: {json.dumps(_chunk({'role': 'assistant'}))}\n\n"
        for i in range(0, len(content or ""), 40):
            yield f"data: {json.dumps(_chunk({'content': content[i:i+40]}))}\n\n"
        if tool_args is not None:
            yield f"data: {json.dumps(_chunk({'tool_calls': [{'index': 0, 'id': 'call_' + uuid.uuid4().hex[:8], 'type': 'function', 'function': {'name': 'read_report', 'arguments': json.dumps(tool_args)}}]}))}\n\n"
        fin = _chunk({}, finish="tool_calls" if tool_args is not None else "stop")
        fin["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield f"data: {json.dumps(fin)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    text = _all_text(body)
    if body.get("tools"):
        args, content = _decide(text)
        if DUMP:
            with open(DUMP, "a") as f:
                f.write(json.dumps({"decision": args, "content": content, "prompt": text}) + "\n")
    else:
        args, content = None, "Recalled facts from an earlier report."
    if body.get("stream"):
        return _stream(content, args)
    return JSONResponse({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
