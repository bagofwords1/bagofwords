#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM that drives the Configuration
Manager (SCCM / MECM) connector through the real agent runtime when no funded
LLM key is available.

Planner turns (request has `tools`), keyword-routed on the current
<user_prompt>:
  round 1: one `create_data` call targeting the MECM agent's classes
  final:   a short answer that quotes the rows the tool observation returned

Codegen turns (prompt mentions generate_df): returns Python that calls
`ds_clients[<mecm client_key>].execute_query("<Class>?<OData>")` — the client
key is read from the prompt's <connection_clients> block, never guessed.

Every codegen prompt is checked for the connector's OData guide (the
`description` of MecmClient). If it is missing the stub answers with code that
raises, so a broken prompt wiring fails the run visibly instead of passing.

Run:  cd backend && uv run python ../tools/agent/stub_llm_mecm.py   (STUB_PORT, default 9099)
Register: uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
Env:  STUB_DUMP=/dir  write every prompt it receives (planner/codegen) there.
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
GUIDE_MARKER = "Configuration Manager (SCCM / MECM) Query Guide"

# (trigger on user prompt, title, classes, interpreted prompt, visualization, code body)
SCENARIOS = [
    (r"\bos\b|operating system|windows (10|11) .*(split|share|count)", "Devices by operating system",
     ["SMS_G_System_OPERATING_SYSTEM"],
     "Count Configuration Manager clients per operating system caption and build from SMS_G_System_OPERATING_SYSTEM.",
     "bar_chart",
     """    os_df = client.execute_query("SMS_G_System_OPERATING_SYSTEM?$select=ResourceID,Caption,BuildNumber")
    print('inventory rows:', len(os_df))
    os_df['os_version'] = os_df['Caption'].str.replace('Microsoft ', '', regex=False) + ' (build ' + os_df['BuildNumber'] + ')'
    out = (os_df.groupby('os_version').size().reset_index(name='devices')
           .sort_values('devices', ascending=False).reset_index(drop=True))
    return out
"""),
    (r"patch|update|complian", "Missing software updates",
     ["SMS_SoftwareUpdate", "SMS_UpdateComplianceStatus", "SMS_R_System"],
     "For each software update, count devices where it is required but not installed "
     "(SMS_UpdateComplianceStatus.Status = 2), with article, title and severity from SMS_SoftwareUpdate, "
     "and the AD site of the affected devices from SMS_R_System.",
     "table",
     """    missing = client.execute_query("SMS_UpdateComplianceStatus?$filter=Status eq 2&$select=CI_ID,MachineID")
    print('missing (device, update) pairs:', len(missing))
    updates = client.execute_query("SMS_SoftwareUpdate?$select=CI_ID,ArticleID,LocalizedDisplayName,SeverityName")
    devices = client.execute_query("SMS_R_System?$select=ResourceId,Name,ADSiteName")
    df = missing.merge(updates, on='CI_ID').merge(devices, left_on='MachineID', right_on='ResourceId')
    out = (df.groupby(['ArticleID', 'LocalizedDisplayName', 'SeverityName'])
             .agg(missing_devices=('MachineID', 'nunique'),
                  sites=('ADSiteName', lambda s: ', '.join(sorted(set(s)))))
             .reset_index().sort_values('missing_devices', ascending=False))
    return out
"""),
    (r"disk|space|storage", "Devices low on disk space",
     ["SMS_G_System_LOGICAL_DISK", "SMS_R_System"],
     "Devices whose C: drive has less than 10% free space, with device name and AD site.",
     "table",
     """    disks = client.execute_query("SMS_G_System_LOGICAL_DISK?$filter=DeviceID eq 'C:'&$select=ResourceID,Size,FreeSpace")
    devices = client.execute_query("SMS_R_System?$select=ResourceId,Name,ADSiteName")
    df = disks.merge(devices, left_on='ResourceID', right_on='ResourceId')
    df['free_pct'] = (df['FreeSpace'] / df['Size'] * 100).round(1)
    out = df[df['free_pct'] < 10][['Name', 'ADSiteName', 'Size', 'FreeSpace', 'free_pct']].sort_values('free_pct')
    return out.reset_index(drop=True)
"""),
    (r"collection", "Collection membership",
     ["SMS_Collection"],
     "List device collections with member counts from SMS_Collection (CollectionType 2 = device).",
     "bar_chart",
     """    out = client.execute_query("SMS_Collection?$filter=CollectionType eq 2&$select=CollectionID,Name,MemberCount&$orderby=MemberCount desc")
    return out
"""),
]


def _messages_text(body):
    parts, last_user = [], ""
    for m in body.get("messages", []):
        c = m.get("content")
        text = c if isinstance(c, str) else "\n".join(
            i.get("text", "") for i in (c or []) if isinstance(i, dict) and i.get("type") == "text")
        parts.append(text)
        if m.get("role") == "user":
            last_user = text
    return "\n".join(parts), last_user


def _dump(kind, text):
    d = os.environ.get("STUB_DUMP")
    if d:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{kind}-{int(time.time() * 1000)}.txt"), "w") as fh:
            fh.write(text)


def _current_turn(text, user):
    matches = list(re.finditer(r"<user_prompt>(.*?)</user_prompt>", text, re.S))
    m = matches[-1] if matches else None
    if m:
        return m.group(1).strip(), text[m.end():]
    return user, text


def _mecm_source_id(text):
    """The data source (agent) id whose connection is the MECM one."""
    for m in re.finditer(r'<data_source\b[^>]*>', text):
        tag = m.group(0)
        window = text[m.start(): m.start() + 4000]
        if 'type="mecm"' in window.split("</data_source>")[0] or "mecm" in tag.lower():
            idm = re.search(r'\bid="([0-9a-f-]{36})"', tag)
            if idm:
                return idm.group(1)
    m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[^\n]{0,200}mecm', text, re.I)
    return m.group(1) if m else None


def _scenario(user):
    return next((s for s in SCENARIOS if re.search(s[0], user, re.I)), None)


def _plan(body):
    text, user = _messages_text(body)
    _dump("planner", text)
    user, tail = _current_turn(text, user)
    scenario = _scenario(user)
    if scenario is None:
        return None, ("I can answer Configuration Manager questions about operating systems, missing updates, "
                      "low disk space and collections.")
    # This turn's tool observations render one per line as `{"summary": ...}`;
    # failures carry a `[failed]`-style prefix.
    observations = [(st or "success", blob) for st, blob in
                    re.findall(r'^(?:\[([a-z_]+)\]\s*)?(\{"summary".*)$', tail, re.M)]
    if not observations:
        ds = _mecm_source_id(text)
        if not ds:
            return None, "I could not find a Configuration Manager agent in this report's scope."
        _, title, classes, interpreted, viz, _code = scenario
        args = {"title": title, "interpreted_prompt": interpreted,
                "tables_by_source": [{"data_source_id": ds, "tables": classes}],
                "visualization_type": viz}
        return [{"name": "create_data", "arguments": args}], f"Querying Configuration Manager: {title.lower()}."
    status, blob = observations[-1]
    try:
        obs = json.loads(blob)
    except ValueError:
        obs = {"summary": blob[:200]}
    summary = obs.get("summary", "")
    if status != "success":
        return None, f"The Configuration Manager query failed ({status}): {summary}"
    return None, (f"{scenario[1]} — from live Configuration Manager AdminService data "
                  f"({', '.join(scenario[2])}).\n\n{_preview_table(obs)}")


def _preview_table(obs):
    """Quote the rows the tool actually returned, as a markdown table."""
    prev = obs.get("data_preview") or {}
    cols = [c.get("field") for c in prev.get("columns") or [] if c.get("field")]
    rows = prev.get("rows") or []
    if not cols or not rows:
        return obs.get("summary", "")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows[:12]:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(lines)


def _codegen(text):
    _dump("codegen", text)
    if GUIDE_MARKER not in text:
        return ("def generate_df(ds_clients, excel_files):\n"
                "    raise RuntimeError('STUB: MECM query guide missing from the codegen prompt')\n")
    m = re.search(r"client_key:[ \t]*([^\n]+?)[ \t]*\ndescription:[ \t]*Microsoft Configuration Manager", text)
    if not m:
        return ("def generate_df(ds_clients, excel_files):\n"
                "    raise RuntimeError('STUB: no MECM client_key in <connection_clients>')\n")
    key = m.group(1)
    user = ""
    for um in re.finditer(r"<user_prompt>(.*?)</user_prompt>", text, re.S):
        user = um.group(1)
    scenario = _scenario(user) or _scenario(text)
    body = scenario[5] if scenario else SCENARIOS[0][5]
    return ("def generate_df(ds_clients, excel_files):\n"
            f"    client = ds_clients[{key!r}]\n" + body)


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": "stub", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


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
                         "model": "stub", "choices": [{"index": 0, "message": msg,
                                                       "finish_reason": "tool_calls" if tool_calls else "stop"}],
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
        code = _codegen(text)
        return _stream(code, None) if stream else _json(code)
    _dump("other", text)
    generic = "Configuration Manager estate" if re.search(r"title", text, re.I) else "OK."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
