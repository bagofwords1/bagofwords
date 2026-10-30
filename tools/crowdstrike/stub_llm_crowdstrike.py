#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM that drives the CrowdStrike
connector end to end (planner → create_data → generated code →
CrowdStrikeClient.execute_query → widget + answer) when no funded LLM key is
available. Only the model's choices are scripted; the agent runtime, code
sandbox, connector, Falcon API calls and UI are all real.

Ids and client names are read from the prompts the app sends — never guessed —
and the final answer quotes the rows the tool observations returned, so what
the UI shows came from the Falcon API through the real pipeline. The codegen
step also asserts that the connector's own prompt (FQL/spec guide) reached the
model: if it is missing, the generated code says so instead of querying.

Run:  cd backend && uv run python ../tools/crowdstrike/stub_llm_crowdstrike.py   (STUB_PORT, default 9098)
Register: uv run python ../tools/agent/setup_stub_llm.py --email <admin> --stub-url http://127.0.0.1:9098/v1
STUB_DUMP=/dir writes the last planner / codegen prompts for debugging.
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

# Each step: (title, tables, visualization, codegen marker, code body using `c`).
STEPS = [
    ("Open alerts by MITRE tactic (last 7 days)", ["alerts"], "bar_chart", "CS_STEP_ALERTS",
     "    since = (pd.Timestamp.utcnow() - pd.Timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')\n"
     "    spec = {'table': 'alerts', 'filter': \"status:['new','in_progress','reopened']+created_timestamp:>='\" + since + \"'\",\n"
     "            'fields': ['composite_id', 'severity_name', 'tactic', 'device.hostname'], 'limit': 5000}\n"
     "    df = c.execute_query(json.dumps(spec))\n"
     "    out = df.groupby('tactic').agg(alerts=('composite_id', 'count'),\n"
     "                                   hosts=('device.hostname', 'nunique')).reset_index()\n"
     "    return out.sort_values('alerts', ascending=False)\n"),
    ("Open incidents ranked by score", ["incidents"], "table", "CS_STEP_INCIDENTS",
     "    df = c.execute_query(json.dumps({'table': 'incidents', 'filter': \"state:'open'\",\n"
     "                                     'sort': 'fine_score|desc'}))\n"
     "    df['score'] = df['fine_score'] / 10\n"
     "    df['hosts'] = df['host_ids'].apply(len)\n"
     "    return df[['name', 'score', 'status', 'hosts', 'start']]\n"),
]


def _text(body):
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        parts.append(c if isinstance(c, str) else "\n".join(
            i.get("text", "") for i in (c or []) if isinstance(i, dict) and i.get("type") == "text"))
    return "\n".join(parts)


def _dump(name, text):
    d = os.environ.get("STUB_DUMP")
    if d:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            fh.write(text)


def _turn(text):
    m = re.search(r"<user_prompt>(.*?)</user_prompt>", text, re.S)
    return (m.group(1).strip(), text[m.end():]) if m else ("", text)


def _agent_id(text):
    """The data-source (agent) id the planner offers for the crowdstrike connection."""
    for pat in (r'<data_source[^>]*\bid="([0-9a-f-]{36})"[^>]*>(?:(?!</data_source>).)*?crowdstrike',
                r'data_source_id["\']?\s*[:=]\s*["\']([0-9a-f-]{36})'):
        m = re.search(pat, text, re.S | re.I)
        if m:
            return m.group(1)
    m = re.search(r'\bid="([0-9a-f-]{36})"', text)
    return m.group(1) if m else None


def _plan(body):
    text = _text(body)
    _dump("planner.txt", text)
    _, tail = _turn(text)
    # A step is done once this turn's observations report its data created.
    done = next((i for i, st in enumerate(STEPS) if f"Created data '{st[0]}'" not in tail), len(STEPS))
    ds = _agent_id(text)
    if not ds:
        return None, "I could not find the CrowdStrike agent in this report's scope."
    if done < len(STEPS):
        title, tables, viz, marker, _ = STEPS[done]
        return [{"name": "create_data", "arguments": {
            "title": title, "user_prompt": f"{title} [{marker}]",
            "interpreted_prompt": f"{title} from CrowdStrike Falcon [{marker}]",
            "tables_by_source": [{"data_source_id": ds, "tables": tables}],
            "visualization_type": viz,
        }}], f"Querying CrowdStrike Falcon: {title.lower()}."
    alerts = re.findall(r'\{"tactic": "([^"]+)", "alerts": (\d+), "hosts": (\d+)\}', tail)
    incidents = re.findall(r'\{"name": "([^"]+)", "score": ([\d.]+)', tail)
    lines = ["SOC briefing from CrowdStrike Falcon (last 7 days)."]
    if alerts:
        lines.append("Open alerts by MITRE tactic: " + "; ".join(f"{t} {a} alerts on {h} hosts" for t, a, h in alerts[:8]) + ".")
    if incidents:
        lines.append("Open incidents by score: " + "; ".join(f"{n} ({sc})" for n, sc in incidents[:6]) + ".")
    return None, "\n\n".join(lines)


def _client_name(text):
    """The `client_key:` whose description is the CrowdStrike client's."""
    for m in re.finditer(r"client_key:\s*(.+)\n\s*description:\s*CrowdStrike", text):
        return m.group(1).strip()
    return None


def _codegen(text):
    _dump("codegen.txt", text)
    # The current task is stated first; past observations later repeat
    # earlier steps' markers, so the earliest marker is this step's.
    present = [s for s in STEPS if s[3] in text]
    step = min(present, key=lambda s: text.find(s[3])) if present else STEPS[0]
    if "CrowdStrike Falcon Integration" not in text:
        return ("def generate_df(ds_clients, excel_files):\n"
                "    raise RuntimeError('connector prompt missing from codegen context')\n")
    name = _client_name(text)
    pick = f"ds_clients[{name!r}]" if name else "next(iter(ds_clients.values()))"
    return ("import json\nimport pandas as pd\n\n"
            "def generate_df(ds_clients, excel_files):\n"
            f"    c = {pick}\n" + step[4])


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


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
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                         "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    stream = bool(body.get("stream"))
    if body.get("tools"):
        calls, content = _plan(body)
        return _stream(content, calls) if stream else _json(content, calls)
    text = _text(body)
    if "generate_df" in text:
        code = _codegen(text)
        return _stream(code, None) if stream else _json(code)
    _dump("other.txt", text)
    generic = "CrowdStrike SOC briefing" if re.search(r"title", text, re.I) else "OK."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9098")), log_level="warning")
