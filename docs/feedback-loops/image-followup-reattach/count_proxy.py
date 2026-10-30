"""Forward /v1/* to api.anthropic.com and log, per request, how many image
blocks it carries and in which message (index from the end; 0 = newest)."""
import json, os, sys, time
import httpx, uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response

LOG = sys.argv[1]
app = FastAPI()
up = httpx.AsyncClient(base_url="https://api.anthropic.com", timeout=300)

def _images(msgs):
    out = []
    for i, m in enumerate(msgs):
        c = m.get("content")
        if not isinstance(c, list): continue
        def walk(parts):
            n = 0
            for p in parts:
                if isinstance(p, dict):
                    if p.get("type") == "image": n += 1
                    if isinstance(p.get("content"), list): n += walk(p["content"])
            return n
        n = walk(c)
        if n: out.append({"msg_from_end": len(msgs) - 1 - i, "role": m.get("role"), "images": n})
    return out

@app.api_route("/{path:path}", methods=["POST", "GET"])
async def fwd(path: str, request: Request):
    body = await request.body()
    try:
        j = json.loads(body); rec = {"t": time.time(), "path": path, "images": _images(j.get("messages") or [])}
        with open(LOG, "a") as f: f.write(json.dumps(rec) + "\n")
    except Exception: pass
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length", "accept-encoding")}
    req = up.build_request(request.method, "/" + path, content=body, headers=headers, params=request.query_params)
    r = await up.send(req, stream=True)
    hop = {"content-length", "transfer-encoding", "content-encoding", "connection"}
    return StreamingResponse(r.aiter_bytes(), status_code=r.status_code,
                             headers={k: v for k, v in r.headers.items() if k.lower() not in hop},
                             background=None)

uvicorn.run(app, host="127.0.0.1", port=9099, log_level="warning")
