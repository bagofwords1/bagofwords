# Feedback Loop — chat answers arrive all at once behind a corporate proxy

An on-prem customer (docker-compose with the shipped Caddy, Bedrock, a
high-security network with corporate proxies) reported that tokens do not
stream: the answer shows up in one piece at the end. Bedrock → BOW streams
fine; the problem is between BOW and the browser. Other customers with the
same Caddy setup are fine.

This loop reproduces that with a mock streaming LLM and a mock
content-inspecting proxy, fixes it, and verifies the fix at the HTTP and
browser level.

## Root cause (validated)

1. **Caddy gzips the chat stream — but only for long prompts.** Both shipped
   Caddyfiles have `encode gzip zstd`, and Caddy's default match list includes
   `text/*`, so `text/event-stream` qualifies. Caddy only decides to compress
   when its *first write* is longer than 512 bytes (`encode.go`
   `MinLength`). The first event of a chat stream, `completion.started`,
   echoes the user's prompt and is about 313 bytes without it. So a prompt
   longer than about **200 bytes** (≈200 English characters, ≈100 Hebrew
   characters) makes Caddy gzip the whole stream; shorter prompts stream
   uncompressed. This is why most customers never notice.
2. **Caddy alone still streams a gzipped answer** (it flushes the encoder on
   every write). The answer only gets stuck when a **content-inspecting
   middlebox** sits between Caddy and the browser — TLS inspection, an
   antivirus/DLP gateway, some load balancers. To scan a compressed body these
   boxes decode it in full first, so nothing is released until the stream
   ends.
3. **The app gave proxies nothing to go on.** Streams sent
   `Cache-Control: no-cache` and `X-Accel-Buffering: no` (nginx-only); no
   `no-transform`, and no declared encoding, so a proxy's compression applied.
4. **Nobody could see it.** The server wrote every token on time; only the
   user's screen showed the problem. Nothing in the logs.

## Setup

Production-shaped stack: the generated SPA served by uvicorn
(`SERVE_FRONTEND=1`), behind the repo's own Caddyfiles run by a real
Caddy 2.10.0 binary (same version as `docker-compose.yaml`), with a mock
middlebox in front.

```
browser ──► inspecting_proxy.py :8088 (--policy compressed) ─┐
browser ──► inspecting_proxy.py :8089 (--policy all) ────────┤
                                                             ▼
                      Caddy :8080 (deploy/airgap/Caddyfile)  Caddy :8081 (Caddyfile)
                                       │
                         uvicorn :3000 (SERVE_FRONTEND=1) ──► slow_stub_llm.py :9099
```

```bash
cd frontend && yarn install && yarn generate          # production SPA
cd ../backend && uv sync --extra dev
export BOW_DATABASE_URL=sqlite:////abs/backend/db/stream.db BOW_ENCRYPTION_KEY=<fernet> \
       SERVE_FRONTEND=1 FRONTEND_DIST_DIR=/abs/frontend/.output/public \
       ENVIRONMENT=production BOW_CONFIG_PATH=/abs/bow-config.yaml
uv run alembic upgrade head
uv run uvicorn main:app --port 3000 --http httptools --loop uvloop --workers 1 &
# Caddy 2.10.0 release binary; the Caddyfiles with app:3000 -> 127.0.0.1:3000
DOMAIN=":8080" caddy run --config <airgap Caddyfile copy> --adapter caddyfile &
STUB_PORT=9099 STUB_DURATION_S=10 uv run python ../tools/agent/slow_stub_llm.py &
uv run python ../tools/agent/inspecting_proxy.py --listen 8088 --upstream http://127.0.0.1:8080 --policy compressed &
uv run python ../tools/agent/inspecting_proxy.py --listen 8089 --upstream http://127.0.0.1:8080 --policy all &
uv run python ../tools/agent/seed_org.py --base-url http://127.0.0.1:3000 --db-path db/stream.db --demo
uv run python ../tools/agent/setup_stub_llm.py --base-url http://127.0.0.1:3000
```

New tools:

- `tools/agent/inspecting_proxy.py` — mock middlebox. `compressed` holds any
  response with a non-identity `Content-Encoding` until it ends (what AV/DLP
  scanners do); `all` holds every response; `none` passes through. Adds
  `Via: 1.1 bow-inspecting-proxy` like a real proxy.
- `tools/agent/stream_delivery_probe.py` — sends one chat turn exactly like
  the browser through any hop and measures the **hold-back**: the longest
  time any token spent between the server writing it and the probe reading it
  (`max_i[(s_last − s_i) − (a_last − a_i)]`, clock-skew free).
- `tools/agent/stream_delivery_ui.mjs` — Playwright: sends the prompt in the
  real UI and samples how many of the stub's "Progress marker" lines are
  visible every 200 ms; records a video. `RELOAD_AT_MS` reloads mid-answer to
  exercise the watch stream.
- `tools/agent/setup_stub_llm.py` — registers a stub as the default model.

## Reproduction (before)

HTTP probe, 10 s stub answer (~600 token events):

| Path | Prompt | Verdict | Hold-back ms | First token ms | Reads with tokens | Encoding |
|---|---|---|---|---|---|---|
| uvicorn direct | short | streamed | 1 | 841 | 603/603 | — |
| Caddy (airgap) | short | streamed | 6 | 175 | 600/600 | — |
| Caddy (root) | short | streamed | 2 | 160 | 602/602 | — |
| middlebox `compressed` → Caddy | short | streamed | 17 | 190 | 600/600 | — |
| Caddy (airgap) | 390 chars | streamed | 2 | 259 | 604/604 | **gzip** |
| Caddy (root) | 390 chars | streamed | 8 | 169 | 601/601 | **gzip** |
| **middlebox `compressed` → Caddy** | **390 chars** | **BUFFERED** | **10185** | **10541** | **1/600** | gzip |
| middlebox `all` → Caddy | short | BUFFERED | 10220 | 10610 | 6/603 | — |

Browser (`stream_delivery_ui.mjs`, middlebox `compressed`, 390-char prompt):
nothing visible for **14.0 s**, then all 200 lines in **1** step
(`media/pr/sse-buffering-middlebox/before-buffered.gif`, `before-t6s.png`).

## Fix

1. **Headers on every live stream** (`backend/app/streaming/delivery.py`
   `NO_BUFFER_HEADERS`, used by the completion kickoff and watch streams, the
   report activity stream, the MCP tool stream and the artifact NDJSON
   stream): `Cache-Control: no-cache, no-transform`,
   `Content-Encoding: identity`, `X-Accel-Buffering: no`. Caddy (and nginx
   `gzip`) leave a response alone once it names its encoding — so this alone
   fixes deployments that keep an old Caddyfile.
2. **Shipped Caddyfiles** compress everything except requests that send
   `Accept: text/event-stream` (every streaming fetch in the app does) — so a
   customer can apply the fix with a config edit, before upgrading the image.
3. **Diagnostics.**
   - Server: one `sse.stream` INFO line per kickoff/watch stream (run id,
     time to first byte, events, bytes, duration, outcome, `Accept-Encoding`,
     `Via`/`X-Forwarded-For` of the request).
   - Browser: `frontend/utils/streamDelivery.ts` measures the hold-back of
     every kickoff and watch stream from the events' server timestamps. When
     an answer was held back (≥ 1.5 s and ≥ 25 % of the time the tokens took)
     it logs a console warning and sends one
     `POST /api/reports/{id}/completions/{cid}/stream-diagnostics`; the
     server logs `sse.delivery_buffered` as a WARNING with the response's
     `Via` and `Content-Encoding` and the likely cause. Live streams send
     nothing.

## Verification (after)

HTTP probe, same paths:

| Path | Prompt | Verdict | Hold-back ms | Reads with tokens | Encoding |
|---|---|---|---|---|---|
| uvicorn / Caddy airgap / Caddy root | short + 390 chars | streamed | 0–11 | ~600/600 | identity |
| **middlebox `compressed` → Caddy** | **390 chars** | **streamed** | **2** | **604/604** | identity |
| middlebox `compressed` → Caddy | short | streamed | 3 | 606/606 | identity |
| middlebox `all` → Caddy | both | BUFFERED (expected) | ~10300 | 6/~600 | identity |

Each half on its own (390-char prompt, middlebox `compressed`):

| App | Caddyfile | Verdict | Hold-back ms |
|---|---|---|---|
| fixed | **old** (`encode gzip zstd`) | streamed | 6 |
| **old** | fixed | streamed | 2 |

Caddy still compresses everything else: `/`, `/_nuxt/*.js` and
`/api/settings` all return `Content-Encoding: gzip` on both Caddyfiles.

Browser, middlebox `compressed`: first line visible at **0.52 s**, **48**
visible updates, 97/200 lines on screen halfway through
(`after-streaming.gif`, `after-t6s.png`). With a reload at 3 s (watch
stream): first line at 0.54 s, 41 updates. No diagnostics sent for either.

Browser, middlebox `all` (still buffered — no header can stop a box that
holds everything): the browser reported it, and the server logged

```
WARNING sse.delivery_buffered kind=kickoff report=… completion=… holdback_ms=10199
  server_span_ms=10252 token_events=602 network_reads=2 response_via=1.1 bow-inspecting-proxy
  response_content_encoding=identity proxies=via=1.1 Caddy x-forwarded-for=127.0.0.1 — live tokens
  were held back on the way to the browser and appeared all at once: an intermediary (corporate
  proxy, TLS inspection, antivirus/DLP, load balancer) is buffering the response
```

and the same with `kind=watch` after a mid-answer reload. Exactly two
warnings for the two buffered browser runs; none for the streamed ones.

Tests: `backend/tests/e2e/test_stream_delivery.py` (headers, verdict + warning,
input validation incl. CR/LF injection, owner-only gate),
`backend/tests/unit/test_stream_delivery_stats.py` (stats line, disconnect
closes the wrapped stream, thresholds), `frontend/tests/unit/streamDelivery.mjs`
(hold-back math for live/buffered/chunked/hiccup streams, clock offset,
header cleaning). Each fails with the fix reverted.

## Not fixed here

A proxy that holds **every** response (middlebox `all`) still delivers the
answer at the end. That needs a delivery fallback (detect the hold-back, then
fetch the partial answer, which is already persisted every 1.2 s, with short
requests). The new diagnostics make those deployments visible in the logs.

## For the customer, now

- Ask whether short prompts stream and long ones don't. That points at
  compression plus an inspecting proxy.
- Immediate workaround without upgrading: in their `Caddyfile`, replace
  `encode gzip zstd` with
  `@compressible not header Accept *text/event-stream*` +
  `encode @compressible gzip zstd`.
- If short prompts are also buffered, their proxy holds everything: exempt
  the BOW host from inspection or bypass the proxy for it (PAC/no-proxy).
