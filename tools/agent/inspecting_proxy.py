#!/usr/bin/env python3
"""Mock content-inspecting middlebox (corporate web gateway / TLS inspection /
antivirus / DLP) for reproducing "tokens arrive all at once" streaming bugs.

Sits between the browser and a BOW deployment and forwards every request.
What it does with the response depends on --policy:

  compressed  (default) Holds the whole response when it carries a
              Content-Encoding (gzip, zstd, br …): a scanner has to decode the
              full body before it can inspect it, so it releases nothing until
              the upstream response ends. Uncompressed responses pass through
              chunk by chunk. This is how many AV/DLP gateways behave, and it
              is why a gzipped SSE stream arrives in one burst.
  all         Holds every response until it is complete (a box that scans
              everything). No response header can fix this one; it needs a
              delivery fallback such as short polling.
  none        Transparent pass-through (control).

Like a real proxy it adds `Via: 1.1 bow-inspecting-proxy`, so the app's
stream diagnostics can name the intermediary.

    cd backend && uv run python ../tools/agent/inspecting_proxy.py \
        --listen 8088 --upstream http://127.0.0.1:8080 --policy compressed
"""
import argparse
import logging
import time

import aiohttp
from aiohttp import web

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade", "content-length",
}
log = logging.getLogger("inspecting-proxy")


def _holds(policy: str, resp: aiohttp.ClientResponse) -> bool:
    if policy == "all":
        return True
    if policy == "compressed":
        enc = resp.headers.get("Content-Encoding", "").strip().lower()
        return enc not in ("", "identity")
    return False


def make_app(upstream: str, policy: str) -> web.Application:
    session: dict = {}

    async def on_startup(app):
        # auto_decompress=False: forward the encoded bytes untouched, as a
        # proxy that re-emits the original body after scanning would.
        session["s"] = aiohttp.ClientSession(
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
        )

    async def on_cleanup(app):
        await session["s"].close()

    async def handler(request: web.Request) -> web.StreamResponse:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        body = await request.read()
        started = time.monotonic()
        async with session["s"].request(
            request.method, upstream + request.rel_url.raw_path_qs,
            headers=headers, data=body or None, allow_redirects=False,
        ) as up:
            out_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP_BY_HOP}
            out_headers["Via"] = "1.1 bow-inspecting-proxy"
            if _holds(policy, up):
                payload = await up.read()
                log.info("HELD %s %s %s %d bytes for %.1fs (Content-Encoding=%s)",
                         request.method, request.path, up.status, len(payload),
                         time.monotonic() - started, up.headers.get("Content-Encoding", "-"))
                return web.Response(status=up.status, headers=out_headers, body=payload)
            resp = web.StreamResponse(status=up.status, headers=out_headers)
            await resp.prepare(request)
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp

    app = web.Application(client_max_size=1024 ** 3)
    app.router.add_route("*", "/{tail:.*}", handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--listen", type=int, default=8088)
    p.add_argument("--upstream", default="http://127.0.0.1:8080")
    p.add_argument("--policy", choices=["compressed", "all", "none"], default="compressed")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log.info("listening :%d -> %s (policy=%s)", a.listen, a.upstream, a.policy)
    web.run_app(make_app(a.upstream.rstrip("/"), a.policy), host="127.0.0.1", port=a.listen, print=None)
