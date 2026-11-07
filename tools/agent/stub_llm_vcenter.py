#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM that drives the vCenter connector
through the REAL agent runtime (planner -> create_data -> code execution ->
VCenterClient -> pyVmomi -> mock vCenter over HTTPS) when no funded LLM key
is available.

Only the *choice* of steps and the final prose are scripted. Every table the
UI shows is a real query result: codegen returns Python that calls
`ds_clients["<key>"].execute_query('{"table": ...}')`, and the key is read from
the coder prompt's connection/client mapping, never guessed. The final prose
restates the mock's seeded scenario and is labelled as written by the stub; it
is not evidence of reasoning (that is what the real-LLM run is for).

Scripted plan for a prompt like "prod-db-01 is slow, find the root cause":
  1 vms            placement + snapshot burden of everything
  2 metrics        disk latency of the VMs on ds-prod-01 + a control VM (60 min)
  3 datastores     capacity / overcommit
  4 events         warnings and errors in the last 3 hours
  5 tasks          what happened to backup-proxy-01 over 30 days
  6 alarms         what is red / yellow now

Env: STUB_PORT (9099), STUB_DS_ID (data source id), STUB_DUMP=/dir (prompts).
Run:  cd backend && uv run python ../tools/agent/stub_llm_vcenter.py
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
DS_ID = os.environ.get("STUB_DS_ID", "")

STEPS = [
    {"title": "VM placement and snapshot burden", "tables": ["vms"],
     "prompt": "List every VM with its host, datastores, power state, snapshot count/size and consolidation flag.",
     "query": {"table": "vms", "columns": ["name", "host", "cluster", "datastores", "power_state", "used_gb",
                                           "snapshot_count", "snapshot_size_gb", "oldest_snapshot_days",
                                           "consolidation_needed"]}},
    {"title": "Disk latency, last hour (VMs on ds-prod-01 vs prod-db-02)", "tables": ["metrics"],
     "prompt": "Max disk latency over the last hour for prod-db-01, app-01, app-02, backup-proxy-01 and prod-db-02.",
     "query": {"table": "metrics", "entity_type": "VirtualMachine",
               "names": ["prod-db-01", "app-01", "app-02", "backup-proxy-01", "prod-db-02"],
               "counters": ["disk.maxTotalLatency.latest"], "minutes": 60}},
    {"title": "Datastore capacity and overcommit", "tables": ["datastores"],
     "prompt": "Capacity, free space, used % and provisioned % of every datastore.",
     "query": {"table": "datastores", "columns": ["name", "type", "capacity_gb", "free_gb", "used_pct",
                                                  "provisioned_pct", "vm_count", "overall_status"]}},
    {"title": "Warnings and errors, last 3 hours", "tables": ["events"],
     "prompt": "vCenter events of category warning or error in the last 3 hours.",
     "query": {"table": "events", "hours": 3, "category": ["warning", "error"],
               "columns": ["created", "event_type", "category", "message", "host", "vm"]}},
    {"title": "Tasks on backup-proxy-01, last 30 days", "tables": ["tasks"],
     "prompt": "Every task on backup-proxy-01 in the last 30 days with state, user and error.",
     "query": {"table": "tasks", "hours": 720, "entity": "backup-proxy-01", "entity_type": "VirtualMachine",
               "columns": ["queued", "task", "state", "user", "error", "duration_seconds"]}},
    {"title": "Triggered alarms", "tables": ["alarms"],
     "prompt": "Alarms currently red or yellow.",
     "query": {"table": "alarms", "columns": ["alarm", "status", "entity", "entity_type", "triggered_time"]}},
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
    # The planner prompt nests connections inside their agent:
    #   <data_source name=".." id="<DS id>" ...> ... <connection ... type="vcenter" id="<connection id>"> ...
    # create_data needs the DATA SOURCE id, so take the block that holds a vcenter connection.
    blocks = list(re.finditer(r'<data_source\b[^>]*\bid="([^"]+)"', text))
    for i, b in enumerate(blocks):
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(text)
        if 'type="vcenter"' in text[b.end():end]:
            return b.group(1)
    return None


def _observations(tail):
    return tail.count('"execution_number"') + len(re.findall(r"<observation\b", tail)) if '"execution_number"' in tail or "<observation" in tail \
        else len(re.findall(r'\{"summary":', tail))


def _plan(body):
    text, user = _messages_text(body)
    user, tail = _current_turn(text, user)
    _dump("planner", text)
    ds = _ds_id(text)
    if not ds:
        return None, "I could not find a vCenter data source in this agent's scope."
    obs = _observations(tail)
    if obs < len(STEPS):
        step = STEPS[obs]
        return [{"name": "create_data", "arguments": {
            "title": step["title"], "user_prompt": step["prompt"], "interpreted_prompt": step["prompt"],
            "tables_by_source": [{"data_source_id": ds, "tables": step["tables"]}],
        }}], f"Step {obs + 1}/{len(STEPS)}: {step['title']}."
    return None, (
        "_Written by the scripted stub LLM; every table above is a live query result._\n\n"
        "**Root cause:** `ds-prod-01` is almost full and its write path is saturated. prod-db-01, app-01 and "
        "app-02 all live on it and all show disk latency spiking to tens of milliseconds in the last hour, while "
        "prod-db-02 on `ds-prod-02` stays at a few ms. The space and the I/O go to **backup-proxy-01**: it "
        "carries a 3-deep snapshot chain (oldest 21 days, by svc-backup) whose delta disks sit on ds-prod-01, and "
        "the consolidation that should have removed them **failed** ('Failed to lock the file'), leaving the VM "
        "flagged *consolidation needed*.\n\n"
        "**Timeline:** snapshots created T-21d/-14d/-7d → RemoveSnapshot_Task fails ~50 min ago → "
        "'Datastore usage on disk' goes red on ds-prod-01 → esx.problem.scsi.device.io.latency.high on "
        "esx-prod-03 → DRS moves app-02 off esx-prod-03.\n\n"
        "**Fix:** free the file lock (the backup proxy still has the disks hot-added), then consolidate "
        "backup-proxy-01; move one VM off ds-prod-01 until it is below 80%; alert on snapshots older than 3 days."
    )


def _client_key(text):
    """Pick the client key of the vcenter connection from <connection_clients>."""
    conn = re.search(r'<connection name="([^"]+)" type="vcenter"', text)
    keys = [k.strip() for k in re.findall(r"client_key:\s*(.+)", text)]
    keys = [k for k in keys if k and k != "bow"]
    if conn:
        for k in keys:
            if k.endswith(":" + conn.group(1)):
                return k
    for k in keys:
        if ":" in k:
            return k
    return keys[0] if keys else "vcenter"


def _codegen(body):
    text, _ = _messages_text(body)
    _dump("coder", text)
    key = _client_key(text)
    m = re.search(r"<user_prompt>(.*?)</user_prompt>", text, re.S)
    current = m.group(1).strip() if m else text
    step = next((s for s in STEPS if s["prompt"] in current), STEPS[0])
    q = json.dumps(step["query"])
    post = ""
    if step["query"]["table"] == "metrics":
        post = ("    df = (df.groupby('entity')['value'].agg(['min', 'mean', 'max']).round(1)\n"
                "            .rename(columns={'min': 'min_ms', 'mean': 'avg_ms', 'max': 'max_ms'})\n"
                "            .sort_values('max_ms', ascending=False).reset_index())\n")
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    df = ds_clients[{key!r}].execute_query({q!r})\n"
        f"{post}"
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
    generic = "vCenter RCA: prod-db-01 slow" if re.search(r"title", text, re.I) else "OK."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")),
                log_level=os.environ.get("STUB_LOG", "warning"))
