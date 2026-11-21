#!/usr/bin/env python3
"""OpenAI-compatible stub LLM that replays a "daily dashboard refresh" scheduled
task and RECORDS THE SIZE OF EVERY PROMPT it is sent.

Used by docs/feedback-loops/2026-11-21-scheduled-task-context-growth.md to
measure how planner input grows run over run when a scheduled prompt keeps
running in the same (host) report.

Each planner turn (request has `tools`) is scripted from the tool results of
the CURRENT turn only (role="tool" messages after the last user message):

    round 1: two parallel `create_data` calls (the customer's "2 queries/run")
    round 2: `create_artifact` rebuilding the dashboard from those 2 viz ids
             (replaces_artifact_id = the previous artifact found in the prompt)
    round 3: a plain-text analysis answer (~1.2K chars)

Codegen calls (prompt mentions generate_df) return a fixed query on orders_1.
Everything else gets a short generic text.

Every request is appended to $STUB_LOG as one JSON line:
    {"ts", "kind": planner|codegen|other, "chars", "est_tokens", "messages",
     "turn_tool_results", "round"}

Env: STUB_SOURCE_ID (data source id, required), STUB_SOURCE_NAME (client key,
default sqlite_source_1), STUB_LOG (default /tmp/sched_growth_stub.jsonl),
STUB_PORT (default 9099).

Run:  cd backend && uv run python ../tools/agent/scheduled_growth_stub_llm.py
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

SOURCE_ID = os.environ.get("STUB_SOURCE_ID", "")
SOURCE_NAME = os.environ.get("STUB_SOURCE_NAME", "sqlite_source_1")
LOG = os.environ.get("STUB_LOG", "/tmp/sched_growth_stub.jsonl")
# "rebuild": create_artifact(replaces_artifact_id) with only today's 2 viz (clean).
# "edit":    edit_artifact appending today's 2 viz; old ones stay on the artifact
#            (edit_artifact keeps existing visualization_ids automatically).
ARTIFACT_MODE = os.environ.get("STUB_ARTIFACT_MODE", "rebuild")
PANELS_MARKER = "{/*PANELS*/}"

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

ANALYSIS = (
    "Daily revenue check. Total revenue across regions is broadly stable versus the "
    "previous run. The North region leads on order count, while the South region "
    "has the highest average order value. No region moved by more than 5% day over "
    "day, so no alert is raised. The dashboard has been refreshed with today's two "
    "queries: orders by region and revenue by region. Watch item: West continues a "
    "slow decline in order count and should be reviewed if the trend holds for "
    "another week. Data freshness: the orders table was read at run time; all "
    "figures reflect the full table. Next run will compare against these numbers. "
) * 2


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text"
        )
    return ""


def _all_text(body: dict) -> str:
    parts = [_text_of(m.get("content")) for m in body.get("messages", [])]
    for m in body.get("messages", []):
        for tc in m.get("tool_calls") or []:
            parts.append(json.dumps(tc))
    return "\n".join(parts)


def _turn_bounds(body: dict):
    """Messages of the CURRENT turn: everything after the last user message that
    carries the task prompt. Earlier runs are replayed above it as real
    assistant/tool messages (durable transcript), and the planner may append a
    trailing user context block (<time>, <conversation>…) after tool results."""
    msgs = list(body.get("messages", []))
    start = max((i for i, m in enumerate(msgs) if m.get("role") == "user" and (
        "<scheduled_execution>" in _text_of(m.get("content"))
        or "<user_prompt>" in _text_of(m.get("content")))), default=-1)
    return [m for m in msgs[start + 1:] if m.get("role") != "user"]


def _turn_calls(body: dict) -> list[str]:
    return [tc.get("function", {}).get("name", "") for m in _turn_bounds(body)
            if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]


def _turn_tool_results(body: dict) -> list[str]:
    return [_text_of(m.get("content")) for m in _turn_bounds(body) if m.get("role") == "tool"]


def _log(rec: dict) -> None:
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _artifact_code(v1: str, v2: str, run_tag: str) -> str:
    return (
        '<script type="text/babel">\n'
        "function Panel({ id }) {\n"
        "  const viz = vizById(id);\n"
        "  const rows = (viz && viz.rows) || [];\n"
        "  return (\n"
        "    <div style={{ padding: 16, border: '1px solid #ddd', borderRadius: 8 }}>\n"
        "      <h3>{(viz && viz.title) || 'Loading'}</h3>\n"
        "      <table><tbody>{rows.map((r, i) => (\n"
        "        <tr key={i}>{Object.values(r).map((v, j) => <td key={j}>{String(v)}</td>)}</tr>\n"
        "      ))}</tbody></table>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
        "function App() {\n"
        "  return (\n"
        "    <div style={{ display: 'grid', gap: 16, padding: 24 }}>\n"
        f"      <h1>Daily revenue</h1><p>Refreshed {run_tag}</p>\n"
        f'      <Panel id="{v1}" />\n'
        f'      <Panel id="{v2}" />\n'
        f"      {PANELS_MARKER}\n"
        "    </div>\n"
        "  );\n"
        "}\n"
        "ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
        "</script>"
    )


def _new_viz(results: list[str]) -> list[str]:
    viz = []
    for r in results:
        for m in re.finditer(r'"created_visualization_ids"\s*:\s*\[([^\]]*)\]', r):
            for vid in re.findall(UUID_RE, m.group(1)):
                if vid not in viz:
                    viz.append(vid)
    return viz


def _planner(body: dict):
    results = _turn_tool_results(body)
    if not results:
        return 1, [
            {"name": "create_data", "arguments": {
                "title": title,
                "user_prompt": f"Daily refresh: {title.lower()}",
                "interpreted_prompt": f"Aggregate orders_1 grouped by region: {title.lower()}.",
                "tables_by_source": [{"data_source_id": SOURCE_ID, "tables": ["orders_1"]}],
            }}
            for title in ("Orders by region", "Revenue by region")
        ], "Refreshing today's two queries."
    calls_made = _turn_calls(body)
    current = re.search(r"<artifact_id>(" + UUID_RE + ")</artifact_id>", _all_text(body))
    if ARTIFACT_MODE == "edit" and current and not {"create_artifact", "edit_artifact"} & set(calls_made):
        viz = _new_viz(results)
        if len(viz) >= 2:
            return 2, [{"name": "edit_artifact", "arguments": {
                "artifact_id": current.group(1),
                "visualization_ids": viz[:2],
                "edits": [{"find": PANELS_MARKER, "replace":
                           f'<Panel id="{viz[0]}" />\n      <Panel id="{viz[1]}" />\n      {PANELS_MARKER}'}],
            }}], "Adding today's panels to the dashboard."
    if not {"create_artifact", "edit_artifact"} & set(calls_made):
        viz = _new_viz(results)
        if len(viz) >= 2:
            prev = re.findall(r'"?artifact_id"?\s*[:=]\s*"?(' + UUID_RE + ")", _all_text(body))
            args = {
                "prompt": "Daily revenue dashboard: two region panels, refreshed each run.",
                "mode": "page",
                "title": "Daily revenue",
                "visualization_ids": viz[:2],
                "code": _artifact_code(viz[0], viz[1], time.strftime("%Y-%m-%d %H:%M:%S")),
            }
            if prev:
                args["replaces_artifact_id"] = prev[-1]
            return 2, [{"name": "create_artifact", "arguments": args}], "Updating the dashboard."
    return 3, None, ANALYSIS


def _codegen() -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    df = ds_clients['{SOURCE_NAME}'].execute_query('SELECT region, COUNT(*) AS orders_count, "
        "SUM(amount) AS total_amount FROM orders_1 GROUP BY region ORDER BY region')\n"
        "    return df\n"
    )


def _chunk(delta: dict, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(p) -> str:
    return f"data: {json.dumps(p)}\n\n"


def _stream(content, tool_calls, prompt_tokens):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        if content:
            for i in range(0, len(content), 200):
                yield _sse(_chunk({"content": content[i:i + 200]}))
        for idx, tc in enumerate(tool_calls or []):
            yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}",
                                              "type": "function",
                                              "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}]}))
        final = _chunk({}, finish="tool_calls" if tool_calls else "stop")
        final["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": 50,
                          "total_tokens": prompt_tokens + 50}
        yield _sse(final)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content, prompt_tokens):
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                         "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 50,
                                   "total_tokens": prompt_tokens + 50}})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = _all_text(body)
    sys_and_tools = text + json.dumps(body.get("tools") or [])
    est = len(sys_and_tools) // 4
    stream = bool(body.get("stream"))
    msgs = body.get("messages", [])
    sys_chars = sum(len(_text_of(m.get("content"))) for m in msgs if m.get("role") == "system")
    tools_chars = len(json.dumps(body.get("tools") or []))
    rec = {"ts": time.time(), "chars": len(sys_and_tools), "est_tokens": est,
           "baseline_tokens": (sys_chars + tools_chars) // 4,
           "conversation_tokens": est - (sys_chars + tools_chars) // 4,
           "messages": len(body.get("messages", [])),
           "turn_tool_results": len(_turn_tool_results(body))}

    if body.get("tools"):
        if os.environ.get("STUB_DUMP"):
            with open(os.environ["STUB_DUMP"], "w") as f:
                json.dump(body.get("messages", []), f, indent=1)
            with open(os.environ["STUB_DUMP"] + ".tools.json", "w") as f:
                json.dump(body.get("tools") or [], f)
        rnd, calls, content = _planner(body)
        _log({**rec, "kind": "planner", "round": rnd})
        return _stream(content, calls, est) if stream else _json(content, est)
    if "generate_df" in text:
        _log({**rec, "kind": "codegen"})
        code = _codegen()
        return _stream(code, None, est) if stream else _json(code, est)
    _log({**rec, "kind": "other"})
    return _stream("Daily revenue check.", None, est) if stream else _json("Daily revenue check.", est)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
