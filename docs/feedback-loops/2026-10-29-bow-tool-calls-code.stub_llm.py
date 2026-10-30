#!/usr/bin/env python3
"""Scripted OpenAI-compatible model for the bow.tool_calls `code` loop.

Planner (request has tools): one create_data per report, then a final answer.
  - a message containing CODECHECK targets builtin:bow / bow.tool_calls;
  - anything else targets the Music Store demo agent (STUB_DS_ID).
Codegen (prompt mentions generate_df): SQL on Track/Genre, or a bow query that
requests the `code` column. Every codegen prompt is appended to STUB_LOG so the
loop can check what the coder was told about the column.

    cd backend && STUB_DS_ID=<music store id> STUB_LOG=/tmp/stub.log \
        uv run python ../docs/feedback-loops/2026-10-29-bow-tool-calls-code.stub_llm.py
"""
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
DS_ID = os.environ.get("STUB_DS_ID", "")
LOG = os.environ.get("STUB_LOG", "/tmp/stub_codegen.log")

CHAT_CODE = '''def generate_df(ds_clients, excel_files):
    key = [k for k in ds_clients if k != "bow"][0]
    return ds_clients[key].execute_query(
        "SELECT g.Name AS genre, COUNT(*) AS tracks FROM Track t "
        "JOIN Genre g ON g.GenreId = t.GenreId GROUP BY g.Name ORDER BY tracks DESC")
'''
BOW_CODE = '''def generate_df(ds_clients, excel_files):
    return ds_clients["bow"].execute_query({
        "dataset": "tool_calls", "query": "tool:create_data", "time_range": {"relative": "1d"},
        "columns": ["call_id", "run_id", "created_at", "tool", "status", "step_id", "code"]})
'''


def _text(body):
    out = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text"]
    return "\n".join(out)


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "stub", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content, tool_calls=None):
    def gen():
        sse = _sse
        yield sse(_chunk({"role": "assistant"}))
        for i in range(0, len(content or ""), 40):
            yield sse(_chunk({"content": content[i:i + 40]}))
        for idx, tc in enumerate(tool_calls or []):
            yield sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                              "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}]}))
        final = _chunk({}, finish="tool_calls" if tool_calls else "stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield sse(final)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content):
    return JSONResponse({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    text, stream = _text(body), bool(body.get("stream"))
    if body.get("tools"):
        if any(m.get("role") == "tool" for m in body.get("messages", [])) or '"execution_number"' in text:
            return _stream("Done.") if stream else _json("Done.")
        if "CODECHECK" in text:
            args = {"title": "create_data calls with code", "interpreted_prompt":
                    "CODECHECK: bow.tool_calls, tool:create_data, last 1 day; return call_id, run_id, status, step_id and code.",
                    "tables_by_source": [{"data_source_id": "builtin:bow", "tables": ["bow.tool_calls"]}]}
        else:
            args = {"title": "Tracks by genre", "interpreted_prompt": "Count tracks per genre from Track joined to Genre.",
                    "tables_by_source": [{"data_source_id": DS_ID, "tables": ["Track", "Genre"]}]}
        calls = [{"name": "create_data", "arguments": args}]
        return _stream("Creating the table.", calls) if stream else _json("Creating the table.")
    if "generate_df" in text:
        bow = "CODECHECK" in text
        with open(LOG, "a") as f:
            f.write(f"=== codegen bow={bow}\n{text}\n")
        code = BOW_CODE if bow else CHAT_CODE
        return _stream(code) if stream else _json(code)
    return _stream("Stub.") if stream else _json("Stub.")


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
