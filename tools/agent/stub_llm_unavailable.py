#!/usr/bin/env python3
"""Scripted OpenAI-compatible LLM for the "agent the run cannot load" loop
(docs/feedback-loops/2026-11-21-agent-unavailable-followups.md).

Stands in for a real model when no funded LLM key is available. It decides
ONLY from what the planner prompt shows it, so it exercises the prompt
contract rather than bypassing it. The agent a question is about is the one
whose name shares a word with the question ("sales" -> Sales, "mail" ->
Mailbox, "budget"/"finance" -> Finance):

  * that agent is listed in <agents_unreachable>  -> it says the agent is
    unreachable right now and that an admin has to check the connection;
  * that agent is listed in <agents_needing_signin> -> request_agent_signin;
  * that agent's table is in the schema or <connectors> (the user is
    connected) -> create_data over it, then a short final answer;
  * none of these -> it says no agent it can see has that data (what a model
    answering from its context would have to conclude).

    STUB_PORT (default 9099)
    cd backend && uv run python ../tools/agent/stub_llm_unavailable.py
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
SIGNIN = re.compile(r"<agents_needing_signin[^>]*>(.*?)</agents_needing_signin>", re.S)
DOWN = re.compile(r"<agents_unreachable[^>]*>(.*?)</agents_unreachable>", re.S)
AGENT = re.compile(r'<agent id="([^"]+)" name="([^"]*)"')
# What each agent of the loop answers, once it is usable.
TOPICS = {
    "Mailbox": {"words": ("mail", "inbox"), "table": "public.messages", "title": "Unread mail",
                "sql": "SELECT sender, subject, received_at FROM messages WHERE unread ORDER BY received_at DESC"},
    "Finance": {"words": ("budget", "finance"), "table": "public.budget", "title": "Budget by department",
                "sql": "SELECT department, amount FROM budget ORDER BY amount DESC"},
    "Sales": {"words": ("sales", "revenue"), "table": "Invoice", "title": "Sales by country",
              "sql": "SELECT BillingCountry AS country, SUM(Total) AS total_sales FROM Invoice GROUP BY 1 ORDER BY 2 DESC"},
}


def _all_text(body: dict) -> str:
    parts = [body.get("system") or ""]
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [i.get("text", "") for i in c if isinstance(i, dict) and i.get("type") == "text"]
    return "\n".join(parts)


def _turn(body: dict):
    """``(question, tool results since it)`` for the turn being answered."""
    msgs = body.get("messages", [])
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"
                     and "<user_prompt>" in json.dumps(m.get("content"))), default=-1)
    question = ""
    if last_user >= 0:
        found = re.findall(r"<user_prompt>(.*?)</user_prompt>", json.dumps(msgs[last_user].get("content")), re.S)
        question = found[-1] if found else ""
    results = [json.dumps(m.get("content")) for m in msgs[last_user + 1:] if m.get("role") == "tool"]
    return question.lower(), results


def _topic(question: str):
    for name, topic in TOPICS.items():
        if any(w in question for w in topic["words"]):
            return name, topic
    return None, None


def _planner(text: str, body: dict):
    question, done = _turn(body)
    name, topic = _topic(question)
    if not name:
        return None, "I can't tell which agent this is about."
    if done:
        if re.search(r"error|No active tables|failed", done[-1], re.I):
            return None, f"The query against the {name} agent failed."
        return None, f"Here is what the {name} agent returned."
    for block in DOWN.findall(text):
        if any(n == name for _id, n in AGENT.findall(block)):
            return None, (f"That comes from the **{name}** agent, which is unreachable right now, so I can't "
                          "answer it at the moment. An admin needs to check its connection; ask again once it is back.")
    for block in SIGNIN.findall(text):
        for aid, n in AGENT.findall(block):
            if n == name:
                return [{"name": "request_agent_signin", "arguments": {
                    "agent_ids": [aid],
                    "title": f"Asking to connect {name}",
                    "message": (f"That comes from **{name}**, which you haven't connected yet. "
                                "Connect it below, then ask again."),
                }}], None
    outside = DOWN.sub("", SIGNIN.sub("", text))
    ids = re.findall(r'id="([0-9a-f-]{36})"[^>]*name="' + re.escape(name) + r'"', outside) or \
        re.findall(r'name="' + re.escape(name) + r'"[^>]*id="([0-9a-f-]{36})"', outside)
    if ids and topic["table"] in outside:
        return [{"name": "create_data", "arguments": {
            "title": topic["title"],
            "user_prompt": question,
            "interpreted_prompt": topic["title"],
            "tables_by_source": [{"data_source_id": ids[0], "tables": [topic["table"]]}],
            "visualization_type": "table",
        }}], None
    return None, f"None of the agents available to me has {topic['words'][0]} data, so I can't answer this."


def _codegen(text: str) -> str:
    name, topic = next(((n, t) for n, t in TOPICS.items() if t["title"] in text), ("Sales", TOPICS["Sales"]))
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    client = next(v for k, v in ds_clients.items() if k == '{name}' or k.startswith('{name}:'))\n"
        f"    return client.execute_query(\"{topic['sql']}\")\n"
    )


def _chunk(delta, finish=None):
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": "stub",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _sse(p):
    return f"data: {json.dumps(p)}\n\n"


def _stream(content, tool_calls):
    def gen():
        yield _sse(_chunk({"role": "assistant"}))
        for i in range(0, len(content or ""), 40):
            yield _sse(_chunk({"content": content[i:i + 40]}))
        for idx, tc in enumerate(tool_calls or []):
            yield _sse(_chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:8]}",
                                               "type": "function", "function": {"name": tc["name"], "arguments": ""}}]}))
            yield _sse(_chunk({"tool_calls": [{"index": idx, "function": {"arguments": json.dumps(tc["arguments"])}}]}))
        final = _chunk({}, finish="tool_calls" if tool_calls else "stop")
        final["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        yield _sse(final)
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _json(content):
    return JSONResponse({"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                         "created": int(time.time()), "model": "stub",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    text = _all_text(body)
    stream = bool(body.get("stream"))
    if body.get("tools"):
        if os.environ.get("STUB_DUMP"):  # the planner prompt, for inspecting what the model was shown
            with open(os.environ["STUB_DUMP"], "w") as fh:
                fh.write(text)
        calls, content = _planner(text, body)
        return _stream(content, calls) if stream else _json(content or "")
    if "generate_df" in text:
        code = _codegen(text)
        return _stream(code, None) if stream else _json(code)
    reply = "Agent question"
    return _stream(reply, None) if stream else _json(reply)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "stub", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
