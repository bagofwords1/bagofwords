#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM that drives the Spectrum connector
through the REAL agent runtime (planner → create_data → code execution →
SpectrumClient → mock OneClick) when no funded LLM key is available.

What it scripts, for a prompt mentioning alarms / root cause / impact:
  round 1  create_data  "Critical and major alarms"      → alarms (min_severity 2)
  round 2  create_data  "Incident timeline"              → events around the LINK DOWN
  round 3  create_data  "Neighbours of the core router"  → connectivity
  final    a written answer that quotes the observed rows
Codegen calls (the coder prompt mentions generate_df) return Python that calls
`ds_clients["<key>"].execute_query('{"table": …}')` — the key is read from the
coder prompt's connection/client mapping, never guessed.

Env: STUB_PORT (9099), STUB_DS_ID (data source id for tables_by_source),
     STUB_T0_MS (incident start, epoch ms; default: GET SPECTRUM_MOCK_URL/__mock/scenario),
     STUB_DUMP=/dir to write the last planner/coder prompts for debugging.
Run:  cd backend && uv run python ../tools/agent/stub_llm_spectrum.py
"""
import json
import os
import re
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
DS_ID = os.environ.get("STUB_DS_ID", "")
MOCK_URL = os.environ.get("SPECTRUM_MOCK_URL", "http://127.0.0.1:8880")


def _t0() -> int:
    if os.environ.get("STUB_T0_MS"):
        return int(os.environ["STUB_T0_MS"])
    try:
        return int(httpx.get(f"{MOCK_URL}/__mock/scenario", timeout=5).json()["incident_start_ms"])
    except Exception:
        return int(time.time() * 1000) - 3600_000


T0 = _t0()
MIN = 60_000

STEPS = [
    {"title": "Critical and major alarms (root cause vs impact)", "tables": ["alarms"],
     "prompt": "List current critical and major alarms with their cause/symptom links.",
     "query": {"table": "alarms", "min_severity": 2, "limit": 100}},
    {"title": "Incident timeline — rtr-core-01 and fw-edge-01", "tables": ["events"],
     "prompt": "Events on rtr-core-01 (with its interfaces) and fw-edge-01 around the LINK DOWN, polls excluded.",
     "query": {"table": "events", "device": ["rtr-core-01", "fw-edge-01"], "include_interfaces": True,
               "start_time": T0 - 30 * MIN, "end_time": T0 + 10 * MIN, "exclude_types": ["0x10d26"], "limit": 200}},
    {"title": "Neighbours of rtr-core-01 (blast radius)", "tables": ["connectivity"],
     "prompt": "Layer-2/3 neighbours of rtr-core-01 per local port with port condition.",
     "query": {"table": "connectivity", "name": "rtr-core-01"}},
]


def _messages_text(body):
    parts, last_user = [], ""
    for m in body.get("messages", []):
        c = m.get("content")
        text = c if isinstance(c, str) else "\n".join(i.get("text", "") for i in (c or []) if isinstance(i, dict) and i.get("type") == "text")
        parts.append(text)
        if m.get("role") == "user":
            last_user = text
    return "\n".join(parts), last_user


def _current_turn(text, user):
    m = re.search(r"<user_prompt>(.*?)</user_prompt>", text, re.S)
    if m:
        return m.group(1).strip(), text[m.end():]
    return user, text


_DUMP_N = {"planner": 0, "coder": 0, "other": 0}


def _dump(kind, text):
    d = os.environ.get("STUB_DUMP")
    if d:
        os.makedirs(d, exist_ok=True)
        _DUMP_N[kind] = _DUMP_N.get(kind, 0) + 1
        for name in (f"{kind}.txt", f"{kind}-{_DUMP_N[kind]}.txt"):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)


def _ds_id(text):
    if DS_ID:
        return DS_ID
    m = re.search(r'type="spectrum"[^>]*\bid="([^"]+)"', text) or re.search(r'\bid="([0-9a-f-]{36})"[^>]*type="spectrum"', text)
    return m.group(1) if m else None


def _observations(tail):
    return tail.count('"execution_number"') + len(re.findall(r"<observation\b", tail)) if '"execution_number"' in tail or "<observation" in tail \
        else len(re.findall(r'\{"summary":', tail))


def _plan(body):
    text, user = _messages_text(body)
    user, tail = _current_turn(text, user)
    _dump("planner", text)
    ds = _ds_id(text)
    if not ds:
        return None, "I could not find a Spectrum data source in this agent's scope."
    obs = _observations(tail)
    if obs < len(STEPS):
        step = STEPS[obs]
        return [{"name": "create_data", "arguments": {
            "title": step["title"], "user_prompt": step["prompt"], "interpreted_prompt": step["prompt"],
            "tables_by_source": [{"data_source_id": ds, "tables": step["tables"]}],
        }}], f"Step {obs + 1}/{len(STEPS)}: {step['title']}."
    return None, (
        "**Root cause (from Spectrum):** the WAN uplink `rtr-core-01_Gi0/1` went **LINK DOWN** at "
        f"{time.strftime('%H:%M UTC', time.gmtime(T0 / 1000))} (alarm 0x1000003, Critical, primary). The same "
        "interface had carried a Major **HIGH UTILIZATION** alarm for the 15 minutes before — utilisation climbed "
        "92→94 % (threshold 85 %) and then the link dropped.\n\n"
        "**Impact (symptom alarms linked by `cause_alarm_ids`):** fw-edge-01 stopped responding to polls two minutes "
        "later, and sw-acc-11 / sw-acc-12 behind the firewall lost their upstream (three DEVICE HAS STOPPED "
        "RESPONDING TO POLLS alarms, all non-primary, all caused by 0x1000003). rtr-core-01 also raised a Major CPU "
        "alarm during OSPF reconvergence; rtr-core-02 took over the default route.\n\n"
        "**Unrelated:** sw-acc-21 (DC2) has a Minor fan alarm, acknowledged and ticketed (INC0042311).\n\n"
        "The three tracked tables above hold the alarms, the event timeline and rtr-core-01's neighbours (its Gi0/1 "
        "port shows condition 6 = Critical towards fw-edge-01)."
    )


def _client_key(text):
    """The coder prompt lists every queryable client under <connection_clients>
    as `client_key: <Agent name>:<connection name>`; take the one whose
    connection is the spectrum connection (never guess, never the built-in
    `bow` client)."""
    conn = re.search(r'<connection name="([^"]+)" type="spectrum"', text)
    keys = [k.strip() for k in re.findall(r"client_key:\s*(.+)", text)]
    keys = [k for k in keys if k and k != "bow"]
    if conn:
        for k in keys:
            if k.endswith(":" + conn.group(1)):
                return k
    for k in keys:
        if ":" in k:
            return k
    return keys[0] if keys else "spectrum"


def _codegen(body):
    text, _ = _messages_text(body)
    _dump("coder", text)
    key = _client_key(text)
    # The coder prompt carries THIS step's create_data user_prompt in
    # <user_prompt>; earlier steps' prompts also appear further down (step
    # history), so match inside that block only.
    m = re.search(r"<user_prompt>(.*?)</user_prompt>", text, re.S)
    current = m.group(1).strip() if m else text
    step = next((s for s in STEPS if s["prompt"] in current), STEPS[0])
    q = json.dumps(step["query"])
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    df = ds_clients[{key!r}].execute_query({q!r})\n"
        "    print('rows:', len(df))\n"
        "    return df\n"
    )


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


def _stream(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 60):
                yield _sse(_chunk({"content": content[i:i + 60]}))
        for idx, tc in enumerate(tool_calls or []):
            yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": tc["name"], "arguments": ""}}]}))
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
        msg["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": t["name"], "arguments": json.dumps(t["arguments"])}} for t in tool_calls]
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if body.get("tools"):
        tool_calls, content = _plan(body)
        return _stream(content, tool_calls) if stream else _json(content, tool_calls)
    text, _ = _messages_text(body)
    if "generate_df" in text:
        code = _codegen(body)
        return _stream(code, None) if stream else _json(code)
    _dump("other", text)
    generic = "Spectrum incident: WAN uplink down" if re.search(r"title", text, re.I) else "OK."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")),
                log_level=os.environ.get("STUB_LOG", "warning"))
