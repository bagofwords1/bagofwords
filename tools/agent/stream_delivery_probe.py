#!/usr/bin/env python3
"""Measure how a chat turn's token stream is DELIVERED through one network path.

Sends one chat turn exactly like the browser does (POST
/api/reports/{id}/completions, Accept: text/event-stream) through --base,
which can point at any hop (uvicorn, Caddy, a middlebox in front of Caddy),
and timestamps every network read. For each `block.delta.token` event it
compares the server's event timestamp with when the bytes arrived here.

`holdback_ms` is the longest time any token was held back on the way:
    max_i [ (server_ts_last - server_ts_i) - (arrival_last - arrival_i) ]
Clock skew between server and probe cancels out. A streamed turn has a
holdback near 0; a buffered one has a holdback close to the whole turn.

    cd backend && uv run python ../tools/agent/stream_delivery_probe.py \
        --base http://127.0.0.1:8088 --label "via middlebox"

Prints one JSON line; exit code 0 = streamed, 3 = buffered.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

import httpx


def _server_ms(ts: str) -> float:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default="Password123!")
    p.add_argument("--prompt", default="Write the streaming reliability report.")
    p.add_argument("--label", default="")
    p.add_argument("--buffered-ms", type=float, default=1500.0,
                   help="holdback above this counts as buffered")
    a = p.parse_args()

    # trust_env=False: never route the probe through the sandbox's own proxy.
    c = httpx.Client(base_url=a.base.rstrip("/"), timeout=httpx.Timeout(30, read=300), trust_env=False)
    tok = c.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
    report = c.post("/api/reports", json={"title": f"stream probe {a.label}".strip(), "data_sources": []}, headers=H).json()

    reads: list[float] = []          # arrival time (ms since request start) of each network read
    tokens: list[tuple[float, float]] = []  # (server_ms, arrival_ms) per token event
    t0 = time.monotonic()
    with c.stream(
        "POST", f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": a.prompt, "mentions": [], "mode": "chat"}, "stream": True},
        headers={**H, "Accept": "text/event-stream", "Cache-Control": "no-cache",
                 "Accept-Encoding": "gzip, deflate, br, zstd"},
    ) as r:
        hdrs = {k: r.headers.get(k) for k in ("content-type", "content-encoding", "cache-control", "via", "x-accel-buffering")}
        buf, event = "", None
        for chunk in r.iter_text():
            now = (time.monotonic() - t0) * 1000.0
            reads.append(now)
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "block.delta.token":
                    try:
                        tokens.append((_server_ms(json.loads(line[5:])["timestamp"]), now))
                    except Exception:
                        pass
    total = (time.monotonic() - t0) * 1000.0

    if len(tokens) < 2:
        print(json.dumps({"label": a.label, "error": "fewer than 2 token events", "headers": hdrs}))
        return 2
    s_last, a_last = tokens[-1]
    holdback = max((s_last - s) - (a_last - arr) for s, arr in tokens)
    token_reads = len({arr for _, arr in tokens})
    out = {
        "label": a.label,
        "base": a.base,
        "headers": hdrs,
        "turn_ms": round(total),
        "server_token_span_ms": round(s_last - tokens[0][0]),
        "first_token_arrival_ms": round(tokens[0][1]),
        "last_token_arrival_ms": round(a_last),
        "token_events": len(tokens),
        "network_reads_with_tokens": token_reads,
        "holdback_ms": round(holdback),
        "verdict": "BUFFERED" if holdback > a.buffered_ms else "STREAMED",
        "report_id": report["id"],
    }
    print(json.dumps(out))
    return 3 if out["verdict"] == "BUFFERED" else 0


if __name__ == "__main__":
    sys.exit(main())
