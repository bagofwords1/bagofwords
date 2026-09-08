#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub LLM that drives the Documentum file
tools (search_files → read_file → answer) so the connector can be exercised end
to end through the real UI/agent runtime when no funded LLM key is available.

The stub is keyword-routed on the current turn's <user_prompt> and derives the
connection id, the round and the file ids from the planner prompt itself — it
never guesses ids. The final answer quotes the observed document content, so
what the UI shows came from Documentum through the real tool pipeline.

Run:  cd backend && uv run python ../tools/agent/stub_llm_documentum.py   (port STUB_PORT, default 9099)
Register in BOW as an OpenAI provider with base_url http://127.0.0.1:9099/v1.
Set STUB_DUMP=/path to write the last planner prompt for debugging.
"""
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

SCENARIOS = [
    # (trigger regex on the user prompt, search query, target file names, answer intro)
    (r"revenue", "revenue", ["Q1_revenue.csv", "Q2_revenue.csv"], "Revenue by region from the Documentum CSVs (Q1 and Q2 2026):"),
    (r"travel", "travel", ["Travel_Policy.docx"], "Travel policy (from Documentum):"),
    (r"invoice|INV-1001", "invoice 1001", ["INV-1001.pdf"], "Invoice INV-1001 (from Documentum):"),
    (r"salary|band", "salary", ["Salary_Bands.csv"], "Salary bands (from Documentum):"),
    (r"target", "targets", ["regional_targets.xlsx"], "Regional targets (from Documentum):"),
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


def _connection_id(text):
    m = re.search(r'<connection[^>]*type="documentum"[^>]*\bid="([^"]+)"', text)
    if not m:
        m = re.search(r'\bid="([0-9a-f-]{36})"[^>]*type="documentum"', text)
    return m.group(1) if m else None


def _current_turn(text, user):
    """(prompt of this turn, text after it). The planner folds the whole
    conversation into one user message; the current question sits in
    <user_prompt> and this turn's observations follow it."""
    m = re.search(r"<user_prompt>(.*?)</user_prompt>", text, re.S)
    if m:
        return m.group(1).strip(), text[m.end():]
    return user, text


def _blobs(tail):
    """This turn's tool observations — one {"summary": ...} JSON object each."""
    out = []
    for m in re.finditer(r'\{"summary":.*?\}(?=\s*(?:\n|$|<))', tail, re.S):
        try:
            out.append(json.loads(m.group(0)))
        except ValueError:
            continue
    return out


def _found_files(tail, names):
    """(name → id) for target names in this turn's search/list observations.
    Details render one line per hit:
    ``Q1_revenue.csv — Finance/Reports/2026/Q1_revenue.csv [id=090180aa0000100a]``."""
    out = {}
    for name in names:
        m = re.search(re.escape(name) + r"[^\n\[]*\[id=([0-9a-f]{16})\]", tail)
        if m:
            out[name] = m.group(1)
    return out


def _plan(body):
    text, user = _messages_text(body)
    conn = _connection_id(text)
    user, tail = _current_turn(text, user)
    if os.environ.get("STUB_DUMP"):
        with open(os.environ["STUB_DUMP"], "w") as fh:
            fh.write(text)
    blobs = _blobs(tail)
    obs = len(blobs)
    if not conn:
        return None, "I could not find a Documentum connection in this agent's scope."
    if re.search(r"Invalid file-source selection|does not match any file source", tail):
        return None, ("The Documentum connection is not attached to this report's agent scope, so the file tools refused "
                      "the request. Select the Documentum agent in the prompt scope and ask again.")
    scenario = next((s for s in SCENARIOS if re.search(s[0], user, re.I)), None)
    if scenario is None or re.search(r"\blist\b|which files|what files|documents you can", user, re.I):
        if obs == 0:
            return [{"name": "list_files", "arguments": {"connection_id": conn, "recursive": True}}], "Listing the Documentum documents visible to you."
        names = re.findall(r"^([^\n—]+\.[a-z0-9]{2,5}) — ", "\n".join(str(b.get("details", "")) for b in blobs), re.M)
        uniq = sorted({n.strip() for n in names})
        return None, ("Documents visible to you in Documentum (" + str(len(uniq)) + "): " + ", ".join(uniq)) if uniq else "No documents are visible to you in Documentum."
    _, query, targets, intro = scenario
    if obs == 0:
        return [{"name": "search_files", "arguments": {"connection_id": conn, "query": query, "max_results": 20}}], f"Searching Documentum for '{query}'."
    found = _found_files(tail, targets)
    if obs == 1:
        if not found:
            return None, (f"No document matching '{query}' is visible to you in Documentum — either it does not exist or your "
                          "Documentum permissions do not grant access to it. Denied documents never appear in search results.")
        return [{"name": "read_file", "arguments": {"connection_id": conn, "file_id": fid, "max_rows": 200}} for fid in found.values()], "Reading the matching documents."
    # Final answer: quote what actually came back from Documentum.
    excerpt = []
    for blob in blobs:
        if re.match(r"(Found|Listed)", str(blob.get("summary", ""))):
            continue
        chunk = blob.get("details") or blob.get("content") or blob.get("text") or blob.get("summary") or ""
        if chunk and chunk not in excerpt:
            excerpt.append(str(chunk)[:2500])
    quoted = "\n\n".join(excerpt) or "(the document content was read; see the tool observations)"
    return None, intro + "\n\n" + quoted


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
    generic = "Documentum documents" if re.search(r"title", text, re.I) else "OK."
    return _stream(generic, None) if stream else _json(generic)


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [{"id": "gpt-5.4", "object": "model"}]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "9099")), log_level="warning")
