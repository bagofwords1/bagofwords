"""Test doubles for outbound-HTTP (SSRF / DNS-rebinding) tests.

- ``FakeDNS``: replaces ``app.core.egress._getaddrinfo``; each lookup pops the
  next answer, so a test can play a rebinding DNS server (first answer fine,
  second answer the metadata IP).
- ``local_server``: a real HTTP(S) server on 127.0.0.1 that records
  ``(method, path, Host header)``. Tests address it through ``*.invalid``
  names that only FakeDNS can resolve, so reaching it at all proves the
  client connected to the address egress validated and pinned.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
from contextlib import contextmanager


class FakeDNS:
    def __init__(self, table):
        self.table = {k: list(v) if isinstance(v, list) else [v] for k, v in table.items()}
        self.lookups: list[str] = []

    def __call__(self, host):
        self.lookups.append(host)
        answers = self.table.get(host)
        if not answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        ip = answers.pop(0) if len(answers) > 1 else answers[0]
        ips = ip if isinstance(ip, (list, tuple)) else [ip]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in ips]


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.seen.append((self.command, self.path, self.headers.get("Host")))
        route = self.routes.get((self.command, self.path)) or self.routes.get(self.path)
        status, headers, body = route or (200, {"Content-Type": "text/plain"}, b"ok")
        if callable(body):
            body = body(self)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **headers}
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_HEAD = do_PUT = _serve

    def log_message(self, *a):
        pass


@contextmanager
def local_server(routes=None, tls_context=None):
    """Yield ``(port, seen)``; ``routes`` maps path or (method, path) to
    ``(status, headers, body)`` where body is bytes, a dict/list (JSON) or a
    callable taking the handler."""
    handler = type("H", (_Handler,), {"routes": routes if routes is not None else {}, "seen": []})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if tls_context is not None:
        srv.socket = tls_context.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1], handler.seen
    finally:
        srv.shutdown()
        srv.server_close()
