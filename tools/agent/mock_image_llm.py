#!/usr/bin/env python3
"""Deterministic OpenAI-compatible MOCK for image generation + artifact codegen.

Serves the subset of the OpenAI API the image-generation feature exercises, so
Loop A runs with NO real credentials:

- POST /v1/images/generations  -> returns a real base64 PNG. The prompt text is
  drawn onto a gradient so screenshots visibly reflect the request.
- POST /v1/chat/completions     -> for create_artifact codegen (prompt mentions
  "frontend developer" + BowFile): returns a <script type="text/babel"> app that
  renders every file id found in the prompt via <BowFile>. Otherwise short text.
- GET  /v1/models               -> minimal listing.

Run:  uv run python ../tools/agent/mock_image_llm.py   (from backend/, for deps)
Env:  MOCK_IMAGE_PORT (default 9098)
"""
import base64
import io
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image, ImageDraw

app = FastAPI()


def _png_for_prompt(prompt: str, size: str | None) -> bytes:
    w, h = 1024, 1024
    if size and "x" in size:
        try:
            w, h = (int(x) for x in size.lower().split("x")[:2])
        except Exception:
            w, h = 1024, 1024
    img = Image.new("RGB", (w, h), "#0f172a")
    draw = ImageDraw.Draw(img)
    # Vertical gradient (indigo -> teal) so the asset looks generated, not blank.
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(0x4f + (0x0d - 0x4f) * t)
        g = int(0x46 + (0x94 - 0x46) * t)
        b = int(0xe5 + (0x88 - 0xe5) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    # Decorative circles.
    draw.ellipse([w * 0.55, h * 0.1, w * 0.95, h * 0.5], outline="#ffffff", width=6)
    draw.ellipse([w * 0.1, h * 0.55, w * 0.4, h * 0.85], fill="#fbbf24")
    # Prompt text, wrapped.
    words = (prompt or "generated image").split()
    lines, cur = [], ""
    for word in words:
        if len(cur) + len(word) + 1 > 26:
            lines.append(cur); cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        lines.append(cur)
    lines = lines[:8]
    y = h * 0.36
    for ln in lines:
        draw.text((w * 0.08, y), ln, fill="#ffffff")
        y += h * 0.045
    draw.text((w * 0.08, h * 0.9), "MOCK gpt-image-1", fill="#e2e8f0")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    size = body.get("size")
    png = _png_for_prompt(prompt, size)
    b64 = base64.b64encode(png).decode("ascii")
    return JSONResponse({
        "created": int(time.time()),
        "data": [{"b64_json": b64, "revised_prompt": prompt}],
        "usage": {"input_tokens": 20, "output_tokens": 200, "total_tokens": 220},
    })


def _all_text(body: dict) -> str:
    parts = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
    return "\n".join(parts)


def _bowfile_app(file_ids: list[str], title: str) -> str:
    tiles = "\n".join(
        f'          <SectionCard title="Generated asset {i + 1}">\n'
        f'            <BowFile id="{fid}" fit="contain" style={{{{ maxHeight: 460 }}}}>\n'
        f'              <div className="absolute" style={{{{ left: "6%", top: "6%" }}}}>\n'
        f'                <span className="bg-yellow-300 text-slate-900 rounded px-2 py-1 text-xs font-semibold">AI generated</span>\n'
        f'              </div>\n'
        f'            </BowFile>\n'
        f'          </SectionCard>'
        for i, fid in enumerate(file_ids)
    )
    return (
        '<script type="text/babel">\n'
        'function App() {\n'
        '  const data = useArtifactData();\n'
        '  if (!data) return <LoadingSpinner size={32} />;\n'
        '  return (\n'
        '    <div className="min-h-screen bg-slate-50 p-8">\n'
        f'      <h1 className="text-2xl font-semibold text-slate-800 mb-6">{title}</h1>\n'
        '      <div className="grid grid-cols-1 gap-6">\n'
        f'{tiles}\n'
        '      </div>\n'
        '    </div>\n'
        '  );\n'
        '}\n'
        'ReactDOM.createRoot(document.getElementById("root")).render(<App />);\n'
        '</script>'
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    text = _all_text(body)
    stream = bool(body.get("stream"))

    is_codegen = "frontend developer" in text.lower() or "BowFile" in text
    if is_codegen:
        file_ids = re.findall(r'id="([0-9a-fA-F-]{36})"', text)
        # Dedupe preserving order.
        seen, ordered = set(), []
        for fid in file_ids:
            if fid not in seen:
                seen.add(fid); ordered.append(fid)
        if not ordered:
            ordered = []
        content = _bowfile_app(ordered, "Generated Image Gallery")
    else:
        content = "Here is your generated image, embedded in a dashboard."

    if stream:
        def gen():
            def chunk(delta, finish=None):
                return {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body.get("model", "mock"),
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            yield f"data: {json.dumps(chunk({'role': 'assistant'}))}\n\n"
            for i in range(0, len(content), 120):
                yield f"data: {json.dumps(chunk({'content': content[i:i + 120]}))}\n\n"
            final = chunk({}, finish="stop")
            final["usage"] = {"prompt_tokens": 200, "completion_tokens": 300, "total_tokens": 500}
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 300, "total_tokens": 500},
    })


@app.get("/v1/models")
async def models():
    return JSONResponse({"object": "list", "data": [
        {"id": "gpt-image-1", "object": "model"},
        {"id": "gpt-5.6-luna", "object": "model"},
    ]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MOCK_IMAGE_PORT", "9098")), log_level="warning")
