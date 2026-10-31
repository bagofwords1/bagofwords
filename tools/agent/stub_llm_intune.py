#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM that drives the Microsoft Intune
connector through the real agent runtime when no funded LLM key is available.

One scripted turn: planner → `create_data` on the Intune agent's
`managed_devices` table → codegen returns a `generate_df` that calls the Intune
client's `execute_query` with an OData filter and aggregates in pandas → final
answer that quotes the observed result. The agent/table ids are read from the
planner prompt (never guessed), so what the UI shows came from Intune (the
Graph stand-in) through the real create_data → codegen → sandbox pipeline.

Run:  cd backend && uv run python ../tools/agent/stub_llm_intune.py   (port STUB_PORT, default 9099)
Register:  uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
Set STUB_DUMP=/dir to write each request's prompt text for debugging.
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
_seq = {"n": 0}

CODE = '''def generate_df(ds_clients, excel_files):
    import json
    client = ds_clients[{name!r}]
    df = client.execute_query(json.dumps({{
        "table": "managed_devices",
        "filter": "complianceState eq 'noncompliant'",
        "select": ["deviceName", "operatingSystem", "userPrincipalName"],
    }}))
    out = (df.groupby("operatingSystem").size()
             .reset_index(name="noncompliant_devices")
             .sort_values("noncompliant_devices", ascending=False))
    print("noncompliant rows:", len(df))
    return out
'''


def _text(body):
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        parts.append(c if isinstance(c, str) else "\n".join(
            i.get("text", "") for i in (c or []) if isinstance(i, dict) and i.get("type") == "text"))
    return "\n".join(parts)


def _dump(kind, text):
    d = os.environ.get("STUB_DUMP")
    if d:
        os.makedirs(d, exist_ok=True)
        _seq["n"] += 1
        with open(os.path.join(d, f"{_seq['n']:02d}-{kind}.txt"), "w") as fh:
            fh.write(text)


def _intune_source(text):
    """(data_source_id, client name) of the Intune agent, from the prompt."""
    for m in re.finditer(r"<data_source\b[^>]*>", text):
        tag = m.group(0)
        if 'type="intune"' in tag or "intune" in tag.lower():
            sid = re.search(r'\bid="([^"]+)"', tag)
            name = re.search(r'\bname="([^"]+)"', tag)
            if sid:
                return sid.group(1), (name.group(1) if name else None)
    m = re.search(r'id="([0-9a-f-]{36})"[^>]*>\s*[^<]*managed_devices', text)
    return (m.group(1), None) if m else (None, None)


def _plan(text):
    tail = text[text.rfind("<user_prompt>"):] if "<user_prompt>" in text else text
    sid, _ = _intune_source(text)
    if not sid:
        return None, "I could not find the Intune agent in this report's scope."
    # This turn's create_data observation (it follows the current <user_prompt>).
    m = re.search(r'\{"summary": "Created data.*', tail)
    if not m:
        return [{"name": "create_data", "arguments": {
            "title": "Non-compliant Intune devices by OS",
            "user_prompt": "How many Intune devices are non-compliant, by operating system?",
            "interpreted_prompt": "Count managed devices whose complianceState is noncompliant, grouped by operatingSystem.",
            "tables_by_source": [{"data_source_id": sid, "tables": ["managed_devices"]}],
            "visualization_type": "bar_chart",
        }}], "Querying Intune managed devices for non-compliant devices."
    # Final answer: quote the rows the tool actually returned.
    try:
        obs = json.loads(m.group(0).splitlines()[0])
        rows = obs["data_preview"]["rows"]
        total = sum(int(r["noncompliant_devices"]) for r in rows)
        lines = "\n".join(f"- {r['operatingSystem']}: {r['noncompliant_devices']}" for r in rows)
        return None, f"{total} Intune devices are non-compliant:\n{lines}"
    except (ValueError, KeyError, TypeError):
        return None, "The non-compliant device breakdown is in the chart above."


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "stub", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(p):
    return f"data: {json.dumps(p)}\n\n"


def _stream(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 60):
                yield _sse(_chunk({"content": content[i:i + 60]}))
        for idx, tc in enumerate(tool_calls or []):
            yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                               "function": {"name": tc["name"], "arguments": ""}}]}))
            args = json.dumps(tc["arguments"])
            for i in range(0, len(args), 80):
                yield _sse(_chunk({"tool_calls": [{"index": idx, "function": {"arguments": args[i:i + 80]}}]}))
        final = _chunk({}, finish="tool_calls" if tool_calls else "stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield _sse(final)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                              "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}} for t in tool_calls]
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
                         "model": "stub", "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = _text(body)
    stream = bool(body.get("stream"))
    if body.get("tools"):
        _dump("planner", text)
        tool_calls, content = _plan(text)
        return _stream(content, tool_calls) if stream else _json(content, tool_calls)
    if "generate_df" in text:
        _dump("codegen", text)
        names = re.findall(r"ds_clients\[['\"]([^'\"]+)['\"]\]", text)
        _, name = _intune_source(text)
        code = CODE.format(name=name or (names[0] if names else "Intune (Contoso)"))
        return _stream(code, None) if stream else _json(code)
    _dump("other", text)
    generic = "Intune compliance" if re.search(r"title", text, re.I) else "OK."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
