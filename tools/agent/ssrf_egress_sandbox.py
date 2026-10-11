#!/usr/bin/env python3
"""SSRF / DNS-rebinding sandbox: drive every admin-tier outbound HTTP path
over the real API and record whether a fake cloud-metadata service was hit.

Feedback loop: docs/feedback-loops/ssrf-egress-pinning.md

Two subcommands, both run from ``backend/`` with its venv:

    # 1. backend + fakes in ONE process (the DNS / routing simulation is
    #    in-process, so it must wrap the app):
    TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/ssrf.db \
        .venv/bin/alembic upgrade head
    TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/ssrf.db \
        .venv/bin/python ../tools/agent/ssrf_egress_sandbox.py serve --port 8731 [--block-private]

    # 2. drive it (seeds an admin through the API, prints a PASS/FAIL table):
    .venv/bin/python ../tools/agent/ssrf_egress_sandbox.py drive --port 8731 [--expect fixed|vulnerable]

What ``serve`` simulates (no root / no network changes needed):

* **IMDS stand-in** on 127.0.0.1:<imds-port>; every request it receives is a
  "metadata hit" (method, path, whether an Authorization header came along).
* **Routing**: a Python-level ``socket.connect`` shim sends any connection to
  169.254.169.254 to the stand-in, the way the cloud network routes it.
* **DNS**: a ``socket.getaddrinfo`` shim answers ``*.ssrf.test`` names:
    - ``imds.ssrf.test``            -> 169.254.169.254
    - ``internal.ssrf.test``        -> 127.0.0.1 (internal service fake)
    - ``rebind<N>-<x>.ssrf.test``   -> 127.0.0.1 for the first N lookups of
      that name, then 169.254.169.254 (a rebinding DNS server: it passes
      every validation the code does, then flips for the connect)
* **Internal services** on 127.0.0.1:<svc-port>: an OpenAI-compatible LLM
  gateway, an MCP server + OAuth authorization server, a SIEM intake and a
  custom REST API.

The shims only cover Python-level sockets, so curl_cffi paths (web_fetch, the
sandbox ``http`` helper) are covered by the unit loop, not this one.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import sys
import threading
import uuid

IMDS_IP = "169.254.169.254"
STATE = {"imds_hits": [], "svc_hits": [], "lookups": {}}


# ── fakes ───────────────────────────────────────────────────────────────────


class _Recorder(http.server.BaseHTTPRequestHandler):
    bucket = "svc_hits"

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _reply(self, status, obj=None, headers=None):
        body = b"" if obj is None else (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if obj is not None and not isinstance(obj, bytes):
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        body = self._body()
        if self.path == "/__hits":
            return self._reply(200, STATE)
        if self.path == "/__reset":
            STATE["imds_hits"].clear(); STATE["svc_hits"].clear(); STATE["lookups"].clear()
            return self._reply(200, {"ok": True})
        STATE[self.bucket].append({
            "method": self.command, "path": self.path, "host": self.headers.get("Host"),
            "authorization": bool(self.headers.get("Authorization")), "body_bytes": len(body),
        })
        return self.route(body)

    do_GET = do_POST = do_HEAD = do_PUT = _handle

    def route(self, body):
        self._reply(404, {"error": "metadata stand-in"})

    def log_message(self, *a):
        pass


class _Imds(_Recorder):
    bucket = "imds_hits"

    def route(self, body):
        self._reply(200, {"Code": "Success", "AccessKeyId": "ASIA-FAKE", "SecretAccessKey": "fake", "Token": "fake"})


class _Services(_Recorder):
    def route(self, body):
        base = f"http://{self.headers.get('Host')}"
        p = self.path.split("?")[0]
        if p == "/v1/models":
            return self._reply(200, {"object": "list", "data": [{"id": "gw-model", "object": "model", "created": 0, "owned_by": "gw"}]})
        if p == "/v1/chat/completions" and json.loads(body or b"{}").get("stream"):
            chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "gw-model",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
            sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            return self._reply(200, sse, {"Content-Type": "text/event-stream"})
        if p == "/v1/chat/completions":
            return self._reply(200, {
                "id": "c1", "object": "chat.completion", "created": 0, "model": "gw-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        if p in ("/mcp", "/evil/mcp") and self.command == "POST":
            prm = f"{base}/.well-known/oauth-protected-resource{p}"
            return self._reply(401, {"error": "unauthorized"}, {"WWW-Authenticate": f'Bearer resource_metadata="{prm}", scope="mcp:read"'})
        if p.startswith("/.well-known/oauth-protected-resource"):
            evil = p.endswith("/evil/mcp")
            return self._reply(200, {"resource": f"{base}{'/evil' if evil else ''}/mcp",
                                     "authorization_servers": [f"{base}/evil" if evil else base], "scopes_supported": ["mcp:read"]})
        if p.startswith("/.well-known/oauth-authorization-server"):
            # The "/evil" authorization server advertises endpoints on the
            # metadata service — discovery must refuse to use them.
            evil = p.endswith("/evil")
            ep = "http://imds.ssrf.test/latest/api" if evil else base
            return self._reply(200, {"issuer": base, "authorization_endpoint": f"{base}/authorize",
                                     "token_endpoint": f"{ep}/token", "registration_endpoint": f"{ep}/register"})
        if p == "/register":
            return self._reply(201, {"client_id": "cid-" + uuid.uuid4().hex[:6]})
        return self._reply(200, {"ok": True})


def _start(handler, port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── DNS + routing shims ─────────────────────────────────────────────────────


def install_shims(imds_port: int):
    real_getaddrinfo = socket.getaddrinfo
    lock = threading.Lock()

    def answer(host: str):
        h = host.lower().rstrip(".")
        if not h.endswith(".ssrf.test"):
            return None
        if h == "imds.ssrf.test":
            return IMDS_IP
        label = h.split(".", 1)[0]
        if label.startswith("rebind") and "-" in label:
            n = int(label[len("rebind"):label.index("-")] or 1)
            with lock:
                STATE["lookups"][h] = STATE["lookups"].get(h, 0) + 1
                k = STATE["lookups"][h]
            return "127.0.0.1" if k <= n else IMDS_IP
        return "127.0.0.1"

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        name = host.decode() if isinstance(host, bytes) else host  # anyio passes IDNA bytes
        ip = answer(name) if isinstance(name, str) else None
        if ip is None:
            return real_getaddrinfo(host, port, family, type, proto, flags)
        p = int(port) if port not in (None, "") and str(port).isdigit() else 0
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p))]

    socket.getaddrinfo = getaddrinfo

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def route(addr):
        if isinstance(addr, tuple) and addr and addr[0] == IMDS_IP:
            return ("127.0.0.1", imds_port)
        return addr

    socket.socket.connect = lambda self, addr: real_connect(self, route(addr))
    socket.socket.connect_ex = lambda self, addr: real_connect_ex(self, route(addr))


# ── serve ───────────────────────────────────────────────────────────────────


def serve(a):
    _start(_Imds, a.imds_port)
    _start(_Services, a.svc_port)
    install_shims(a.imds_port)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)

    sys.path.insert(0, os.getcwd())
    from app.ee import license as lic

    lic._get_license_key = lambda: "sandbox"
    lic._validate_license_key = lambda key: lic.LicenseInfo(
        licensed=True, tier="enterprise", org_name="ssrf-sandbox", features=["audit_logs", "audit_log_streams"])
    lic.clear_license_cache()
    from app.settings.config import settings

    settings.bow_config.security.block_private_outbound = bool(a.block_private)
    import uvicorn
    from main import app

    print(f"ssrf sandbox: api :{a.port} imds-stand-in :{a.imds_port} services :{a.svc_port} "
          f"block_private_outbound={bool(a.block_private)}", flush=True)
    # asyncio loop (not uvloop) so the getaddrinfo shim also covers async clients.
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning", loop="asyncio")


# ── drive ───────────────────────────────────────────────────────────────────


def drive(a):
    import httpx

    api = httpx.Client(base_url=f"http://127.0.0.1:{a.port}/api", timeout=120, trust_env=False)
    ctl = httpx.Client(base_url=f"http://127.0.0.1:{a.imds_port}", timeout=10, trust_env=False)
    email, pw = "ssrf-admin@example.com", "Password123!"
    api.post("/auth/register", json={"name": "SSRF Admin", "email": email, "password": pw})
    tok = api.post("/auth/jwt/login", data={"username": email, "password": pw}).json()["access_token"]
    api.headers["Authorization"] = f"Bearer {tok}"
    orgs = api.get("/organizations").json()
    org_id = orgs[0]["id"] if orgs else api.post("/organizations", json={"name": "SSRF Org"}).json()["id"]
    api.headers["X-Organization-Id"] = org_id
    S = a.svc_port
    tag = uuid.uuid4().hex[:5]

    def llm(base_url):
        return api.post("/llm/test_connection", json={
            "name": "gw", "provider_type": "custom",
            "credentials": {"base_url": base_url, "api_key": "sk-demo-secret"},
            "models": [{"model_id": "gw-model", "name": "gw-model", "is_custom": True, "is_enabled": True}],
        })

    def ds(base_url):
        return api.post("/data_sources/test_connection", json={
            "name": "api", "type": "custom_api", "config": {"base_url": base_url, "auth_type": "none"}, "credentials": {}})

    def mcp(server_url):
        r = api.post("/connections", json={
            "name": f"mcp-{uuid.uuid4().hex[:6]}", "type": "mcp", "auth_policy": "user_required",
            "allowed_user_auth_modes": ["oauth"],
            "config": {"server_url": server_url, "transport": "streamable_http", "auth_type": "dcr"}, "credentials": {}})
        if r.status_code >= 300:
            return r
        return api.get(f"/connections/{r.json()['id']}/oauth/authorize")

    def audit(url):
        return api.post("/enterprise/audit/streams/test", json={"destination": "https", "config": {"url": url}, "secrets": {}})

    def gchat(token_uri):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        sa = {"type": "service_account", "project_id": "p", "private_key_id": "k", "private_key": key,
              "client_email": "bot@p.iam.gserviceaccount.com", "client_id": "1", "token_uri": token_uri}
        return api.post("/settings/integrations/google_chat", json={
            "service_account_json": sa, "pubsub_subscription": "projects/p/subscriptions/s"})

    attacks = [
        ("LLM test_connection, base_url rebinds after 2 checks", lambda: llm(f"http://rebind2-llm{tag}.ssrf.test:{S}/v1")),
        ("custom_api test, base_url = metadata name", lambda: ds("http://imds.ssrf.test/latest/meta-data")),
        ("custom_api test, base_url rebinds after 1 check", lambda: ds(f"http://rebind1-api{tag}.ssrf.test:{S}")),
        ("MCP OAuth authorize, server_url = metadata name", lambda: mcp("http://imds.ssrf.test/mcp")),
        ("MCP OAuth authorize, AS advertises metadata registration_endpoint", lambda: mcp(f"http://internal.ssrf.test:{S}/evil/mcp")),
        ("MCP OAuth authorize, server rebinds after 2 lookups", lambda: mcp(f"http://rebind2-mcp{tag}.ssrf.test:{S}/mcp")),
        ("audit stream test, url rebinds after 1 check", lambda: audit(f"http://rebind1-siem{tag}.ssrf.test:{S}/ingest")),
        ("Google Chat create, token_uri = metadata", lambda: gchat("http://imds.ssrf.test/token")),
    ]
    legit = [
        ("LLM test_connection, internal OpenAI-compatible gateway", lambda: llm(f"http://internal.ssrf.test:{S}/v1"), "svc"),
        ("custom_api test, internal API", lambda: ds(f"http://internal.ssrf.test:{S}"), "svc"),
        ("MCP OAuth authorize, internal MCP + authorization server", lambda: mcp(f"http://internal.ssrf.test:{S}/mcp"), "svc"),
        ("audit stream test, internal SIEM", lambda: audit(f"http://internal.ssrf.test:{S}/ingest"), "svc"),
        ("Google Chat create, genuine token_uri (no Google egress here)", lambda: gchat("https://oauth2.googleapis.com/token"), "google"),
    ]

    rows, failed = [], 0
    for name, fn in attacks:
        ctl.get("/__reset")
        r = fn()
        hits = ctl.get("/__hits").json()["imds_hits"]
        verdict = "VULNERABLE (metadata hit)" if hits else "blocked"
        # Base code has no check at all on the custom_api rebind row, so the
        # rebinding answer is never reached there (its direct-metadata row
        # is the base-code demonstration).
        na = a.expect == "vulnerable" and "custom_api test, base_url rebinds" in name
        ok = na or ((not hits) if a.expect == "fixed" else bool(hits))
        failed += not ok
        rows.append((("PASS" if ok else "FAIL"), name, r.status_code, verdict, _short(r)))
    for name, fn, kind in legit:
        ctl.get("/__reset")
        r = fn()
        st = ctl.get("/__hits").json()
        reached = bool(st["svc_hits"])
        if kind == "google":
            reached = "token_uri" not in r.text  # not refused by validation
        expect_reach = not a.block_private or kind == "google"
        ok = reached == expect_reach and not st["imds_hits"]
        failed += not ok
        rows.append((("PASS" if ok else "FAIL"), name, r.status_code,
                     ("reached internal service" if reached else "refused"), _short(r)))
    print(f"\nblock_private_outbound={a.block_private}  expect={a.expect}\n")
    for row in rows:
        print(f"{row[0]:4} | {row[1]:<62} | HTTP {row[2]} | {row[3]:<26} | {row[4]}")
    sys.exit(1 if failed else 0)


def _short(r):
    try:
        j = r.json()
    except Exception:
        return r.text[:90]
    msg = j.get("message") or j.get("detail") or j.get("error") or j.get("authorization_url") or j
    if isinstance(j, dict) and "success" in j:
        msg = f"success={j['success']} {j.get('message') or j.get('error') or ''}"
    return str(msg).replace("\n", " ")[:110]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "drive"):
        s = sub.add_parser(name)
        s.add_argument("--port", type=int, default=8731)
        s.add_argument("--imds-port", type=int, default=18254)
        s.add_argument("--svc-port", type=int, default=18255)
        s.add_argument("--block-private", action="store_true")
    sub.choices["drive"].add_argument("--expect", choices=["fixed", "vulnerable"], default="fixed")
    a = p.parse_args()
    serve(a) if a.cmd == "serve" else drive(a)


if __name__ == "__main__":
    main()
