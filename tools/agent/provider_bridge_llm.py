#!/usr/bin/env python3
"""Local Bedrock / Gemini endpoint backed by the real Anthropic API.

Lets a sandbox exercise the app's REAL ``BedrockClient`` and ``Google``
clients (request translation, image handling, streaming parse) end to end
without AWS or Google credentials: the backend is pointed here with

    AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://127.0.0.1:9312   (boto3)
    GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:9312             (google-genai)

Each provider request is translated to an Anthropic Messages call
(``BRIDGE_MODEL``, default claude-haiku-4-5-20251001), and the answer is
re-encoded in the provider's own wire format — AWS event-stream frames for
ConverseStream, SSE / JSON for Gemini. What the model sees is exactly what the
app put in the provider request: an image the client dropped never reaches it.

``GET /stats`` reports, per provider, the requests served and the images each
carried (``images_per_request``) — the deterministic half of the evidence.

Two model backends (``BRIDGE_MODE``):

- ``anthropic`` — forward to the real Anthropic API (needs ANTHROPIC_API_KEY).
- ``scripted`` (default) — a deterministic agent with real eyes: it lists the
  files of ``BRIDGE_SOURCE`` (an agent / connection name), reads the first
  attachment with ``read_file(as_images=true)``, then OCRs (tesseract) every
  image that actually reached it and answers with what it read. When the tool
  catalog it was handed has no ``read_file`` it says so instead
  (``NO_READ_FILE_TOOL``) — the Auto-mode tool gap, observed from the model's
  side. No credentials needed.

Run:  cd backend && uv run python ../tools/agent/provider_bridge_llm.py
Env:  BRIDGE_PORT (default 9312), BRIDGE_MODE, BRIDGE_SOURCE, BRIDGE_MODEL,
      ANTHROPIC_API_KEY
"""
import base64
import binascii
import json
import os
import re
import struct
import subprocess
import tempfile
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("BRIDGE_MODEL", "claude-haiku-4-5-20251001")
API_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_KEY") or ""

MODE = os.environ.get("BRIDGE_MODE", "scripted")
SOURCE = os.environ.get("BRIDGE_SOURCE", "ServiceNow KB")

app = FastAPI()
STATS: dict = {"bedrock": {"requests": 0, "images_per_request": []},
               "gemini": {"requests": 0, "images_per_request": []},
               "planner_tool_names": [], "answers": []}


def _record(provider: str, messages: list) -> None:
    n = sum(1 for m in messages for b in (m["content"] if isinstance(m["content"], list) else [])
            if b.get("type") == "image")
    STATS[provider]["requests"] += 1
    STATS[provider]["images_per_request"].append(n)


def _merge_roles(messages: list) -> list:
    """Anthropic needs alternating roles; providers allow repeats."""
    out: list = []
    for m in messages:
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        content = [b for b in content if not (b.get("type") == "text" and not b.get("text"))]
        if not content:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": m["role"], "content": list(content)})
    for m in out:  # tool_result blocks must lead a user turn
        if m["role"] == "user":
            m["content"].sort(key=lambda b: 0 if b.get("type") == "tool_result" else 1)
    return out


def _lower_types(schema):
    if isinstance(schema, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_types(v))
                for k, v in schema.items() if k not in ("nullable",)}
    if isinstance(schema, list):
        return [_lower_types(v) for v in schema]
    return schema


def _ocr(b64: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(base64.b64decode(b64))
        f.flush()
        out = subprocess.run(["tesseract", f.name, "-", "--psm", "11"], capture_output=True, text=True, timeout=60)
    return " ".join(out.stdout.split())


def _text_of(block) -> str:
    c = block.get("content")
    return c if isinstance(c, str) else json.dumps(c)


def _scripted(messages: list, tools: list) -> dict:
    """list_files -> read_file(as_images) -> answer from OCR of received images."""
    def reply(content, stop="end_turn"):
        return {"content": content, "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 10}}

    def call(name, args):
        return reply([{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}", "name": name, "input": args}],
                     "tool_use")

    names = [t["name"] for t in tools]
    if not names:  # titles, summaries: no tool loop
        return reply([{"type": "text", "text": "KB article review"}])
    STATS["planner_tool_names"].append(sorted(names))
    uses = {b["id"]: b["name"] for m in messages if m["role"] == "assistant"
            for b in m["content"] if b.get("type") == "tool_use"}
    results = [(uses.get(b["tool_use_id"]), _text_of(b)) for m in messages if m["role"] == "user"
               for b in m["content"] if b.get("type") == "tool_result"]
    done = [n for n, _ in results]
    if "read_file" not in names:
        answer = "NO_READ_FILE_TOOL: read_file is not in my tool list (" + ", ".join(sorted(names)) + ")."
        STATS["answers"].append(answer)
        return reply([{"type": "text", "text": answer}])
    listings = [t for n, t in results if n == "list_files"]
    if not listings and "list_files" in names:
        return call("list_files", {"connection_id": SOURCE, "title": "Listing KB files"})
    if "read_file" not in done:
        last = (listings[-1] if listings else "").replace("\\n", "\n")  # JSON-escaped newlines
        STATS["last_listing"] = last[:4000]
        # "name [id=...]" entries; a file has an extension, anything else is a folder.
        entries = re.findall(r"([^\n\[\]]+?)\s*\[id=([^\]]+)\]", last)
        files = [(n.strip(), i) for n, i in entries if re.search(r"\.\w{2,5}$", n.strip())]
        if files:
            return call("read_file", {"connection_id": SOURCE, "file_id": files[0][1],
                                      "as_images": True, "title": "Reading the KB document"})
        if entries and len(listings) < 4:
            return call("list_files", {"connection_id": SOURCE, "folder_id": entries[0][1],
                                       "title": "Opening the KB folder"})
        return call("read_file", {"connection_id": SOURCE, "file_id": "KB0013962",
                                  "as_images": True, "title": "Reading the KB document"})
    images = [b["source"]["data"] for m in messages for b in m["content"]
              if b.get("type") == "image" and b.get("source", {}).get("type") == "base64"]
    seen = " | ".join(_ocr(d) for d in images) or "(none)"
    answer = f"IMAGES_RECEIVED={len(images)}. OCR of the page images: {seen}"
    STATS["answers"].append(answer)
    return reply([{"type": "text", "text": answer}])


async def _model(system: str, messages: list, tools: list, max_tokens: int) -> dict:
    if MODE == "scripted":
        return _scripted(messages, tools)
    return await _anthropic(system, messages, tools, max_tokens)


async def _anthropic(system: str, messages: list, tools: list, max_tokens: int) -> dict:
    body = {"model": MODEL, "max_tokens": min(max_tokens or 4096, 8192), "messages": messages}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(ANTHROPIC_URL, json=body, headers={
            "x-api-key": API_KEY, "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        raise RuntimeError(f"anthropic {r.status_code}: {r.text[:500]}")
    return r.json()


# ── Bedrock Converse ────────────────────────────────────────────────────────

def _bedrock_to_anthropic(req: dict):
    messages = []
    for m in req.get("messages", []):
        blocks = []
        for b in m.get("content", []):
            if "text" in b:
                blocks.append({"type": "text", "text": b["text"]})
            elif "image" in b:
                fmt = b["image"].get("format", "png")
                data = b["image"]["source"]["bytes"]  # base64 on the JSON wire
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": f"image/{'jpeg' if fmt == 'jpg' else fmt}", "data": data}})
            elif "toolUse" in b:
                tu = b["toolUse"]
                blocks.append({"type": "tool_use", "id": tu["toolUseId"], "name": tu["name"],
                               "input": tu.get("input") or {}})
            elif "toolResult" in b:
                tr = b["toolResult"]
                text = "\n".join(c.get("text", "") for c in tr.get("content", []) if "text" in c)
                blocks.append({"type": "tool_result", "tool_use_id": tr["toolUseId"], "content": text})
        messages.append({"role": m["role"], "content": blocks})
    system = "\n\n".join(s["text"] for s in req.get("system", []) if "text" in s)
    tools = [
        {"name": t["toolSpec"]["name"], "description": t["toolSpec"].get("description", ""),
         "input_schema": t["toolSpec"]["inputSchema"]["json"]}
        for t in (req.get("toolConfig") or {}).get("tools", []) if "toolSpec" in t
    ]
    max_tokens = (req.get("inferenceConfig") or {}).get("maxTokens") or 4096
    return system, messages, tools, max_tokens


def _frame(event_type: str, payload: dict) -> bytes:
    """One AWS event-stream message (the vnd.amazon.eventstream framing)."""
    headers = b""
    for name, value in ((":event-type", event_type), (":content-type", "application/json"),
                        (":message-type", "event")):
        nb, vb = name.encode(), value.encode()
        headers += struct.pack("B", len(nb)) + nb + b"\x07" + struct.pack(">H", len(vb)) + vb
    body = json.dumps(payload).encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    msg = prelude + headers + body
    return msg + struct.pack(">I", binascii.crc32(msg) & 0xFFFFFFFF)


_STOP = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens", "stop_sequence": "stop_sequence"}


@app.post("/model/{model_id:path}/converse-stream")
async def converse_stream(model_id: str, request: Request):
    system, messages, tools, max_tokens = _bedrock_to_anthropic(await request.json())
    messages = _merge_roles(messages)
    _record("bedrock", messages)
    resp = await _model(system, messages, tools, max_tokens)
    frames = [_frame("messageStart", {"role": "assistant"})]
    for i, b in enumerate(resp["content"]):
        if b["type"] == "text":
            frames.append(_frame("contentBlockDelta", {"contentBlockIndex": i, "delta": {"text": b["text"]}}))
        elif b["type"] == "tool_use":
            frames.append(_frame("contentBlockStart", {"contentBlockIndex": i, "start": {
                "toolUse": {"toolUseId": b["id"], "name": b["name"]}}}))
            frames.append(_frame("contentBlockDelta", {"contentBlockIndex": i, "delta": {
                "toolUse": {"input": json.dumps(b["input"])}}}))
        frames.append(_frame("contentBlockStop", {"contentBlockIndex": i}))
    frames.append(_frame("messageStop", {"stopReason": _STOP.get(resp.get("stop_reason"), "end_turn")}))
    u = resp.get("usage") or {}
    frames.append(_frame("metadata", {"usage": {
        "inputTokens": u.get("input_tokens", 0), "outputTokens": u.get("output_tokens", 0),
        "totalTokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)}, "metrics": {"latencyMs": 1}}))
    return Response(b"".join(frames), media_type="application/vnd.amazon.eventstream")


@app.post("/model/{model_id:path}/converse")
async def converse(model_id: str, request: Request):
    system, messages, tools, max_tokens = _bedrock_to_anthropic(await request.json())
    messages = _merge_roles(messages)
    _record("bedrock", messages)
    resp = await _model(system, messages, tools, max_tokens)
    content = []
    for b in resp["content"]:
        if b["type"] == "text":
            content.append({"text": b["text"]})
        elif b["type"] == "tool_use":
            content.append({"toolUse": {"toolUseId": b["id"], "name": b["name"], "input": b["input"]}})
    u = resp.get("usage") or {}
    return {"output": {"message": {"role": "assistant", "content": content}},
            "stopReason": _STOP.get(resp.get("stop_reason"), "end_turn"),
            "usage": {"inputTokens": u.get("input_tokens", 0), "outputTokens": u.get("output_tokens", 0),
                      "totalTokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)},
            "metrics": {"latencyMs": 1}}


# ── Gemini generateContent ──────────────────────────────────────────────────

def _gemini_to_anthropic(req: dict):
    messages, pending = [], []  # pending: ids of the last model turn's calls, in order
    for c in req.get("contents", []):
        role = "assistant" if c.get("role") == "model" else "user"
        blocks, calls = [], []
        for p in c.get("parts", []):
            if p.get("text") is not None and not p.get("thought"):
                blocks.append({"type": "text", "text": p["text"]})
            elif "inlineData" in p or "inline_data" in p:
                d = p.get("inlineData") or p.get("inline_data")
                raw = d["data"].replace("-", "+").replace("_", "/")  # SDK sends url-safe, unpadded
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": d.get("mimeType") or d.get("mime_type"),
                    "data": raw + "=" * (-len(raw) % 4)}})
            elif "functionCall" in p or "function_call" in p:
                fc = p.get("functionCall") or p.get("function_call")
                cid = f"toolu_{uuid.uuid4().hex[:20]}"
                calls.append((fc["name"], cid))
                blocks.append({"type": "tool_use", "id": cid, "name": fc["name"], "input": fc.get("args") or {}})
            elif "functionResponse" in p or "function_response" in p:
                fr = p.get("functionResponse") or p.get("function_response")
                idx = next((i for i, (n, _) in enumerate(pending) if n == fr["name"]), 0 if pending else None)
                cid = pending.pop(idx)[1] if idx is not None else f"toolu_{uuid.uuid4().hex[:20]}"
                out = fr.get("response") or {}
                blocks.append({"type": "tool_result", "tool_use_id": cid,
                               "content": out.get("output") if isinstance(out.get("output"), str) else json.dumps(out)})
        if role == "assistant":
            pending = calls
        messages.append({"role": role, "content": blocks})
    si = req.get("systemInstruction") or req.get("system_instruction") or {}
    system = "\n\n".join(p.get("text", "") for p in si.get("parts", []))
    tools = []
    for t in req.get("tools", []) or []:
        for fd in t.get("functionDeclarations") or t.get("function_declarations") or []:
            schema = fd.get("parameters") or fd.get("parametersJsonSchema") or {"type": "object", "properties": {}}
            tools.append({"name": fd["name"], "description": fd.get("description", ""),
                          "input_schema": _lower_types(schema)})
    gc = req.get("generationConfig") or {}
    return system, messages, tools, gc.get("maxOutputTokens") or 4096


def _gemini_response(resp: dict) -> dict:
    parts = []
    for b in resp["content"]:
        if b["type"] == "text":
            parts.append({"text": b["text"]})
        elif b["type"] == "tool_use":
            parts.append({"functionCall": {"name": b["name"], "args": b["input"]}})
    u = resp.get("usage") or {}
    return {"candidates": [{"content": {"role": "model", "parts": parts or [{"text": ""}]},
                            "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": u.get("input_tokens", 0),
                              "candidatesTokenCount": u.get("output_tokens", 0),
                              "totalTokenCount": u.get("input_tokens", 0) + u.get("output_tokens", 0)},
            "modelVersion": MODEL}


@app.post("/{version}/models/{model_and_method:path}")
async def gemini(version: str, model_and_method: str, request: Request):
    model, _, method = model_and_method.partition(":")
    if method not in ("generateContent", "streamGenerateContent"):
        return JSONResponse({"error": {"message": f"unsupported {method}"}}, status_code=404)
    system, messages, tools, max_tokens = _gemini_to_anthropic(await request.json())
    messages = _merge_roles(messages)
    _record("gemini", messages)
    out = _gemini_response(await _model(system, messages, tools, max_tokens))
    if method == "generateContent":
        return out
    return StreamingResponse(iter([f"data: {json.dumps(out)}\r\n\r\n"]), media_type="text/event-stream")


@app.get("/stats")
async def stats():
    return STATS


@app.post("/reset")
async def reset():
    for k in ("bedrock", "gemini"):
        STATS[k]["requests"], STATS[k]["images_per_request"] = 0, []
    STATS["planner_tool_names"], STATS["answers"] = [], []
    return STATS


if __name__ == "__main__":
    import uvicorn

    if MODE == "anthropic" and not API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY (or ANTHROPIC_KEY) is required")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("BRIDGE_PORT", "9312")), log_level="warning")
