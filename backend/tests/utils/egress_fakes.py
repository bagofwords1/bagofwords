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
import socketserver
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


# ── socket-level trap (non-HTTP paths, drivers, SDKs that resolve themselves) ──

METADATA_IP = "169.254.169.254"


class _TrapHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the cloud metadata service: records every connection
    that reached it and answers like IMDS if it speaks HTTP."""

    hits: list = []

    def handle(self):
        self.hits.append(self.client_address)
        # Non-HTTP clients (SMTP, syslog, database drivers) wait for a
        # greeting that never comes: hang up after a second instead.
        self.request.settimeout(1.0)
        try:
            super().handle()
        except Exception:
            pass

    def _serve(self):
        body = json.dumps({"Code": "Success", "AccessKeyId": "ASIA-TRAP", "SecretAccessKey": "trap"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_HEAD = _serve

    def log_message(self, *a):
        pass


class NetTrap:
    """Plays the network for code that resolves and connects on its own
    (aiosmtplib, imaplib, asyncio streams, pymysql, botocore, requests).

    * ``dns`` maps a name to an answer or a list of answers (one per lookup,
      the last repeats — a rebinding DNS server); unknown names fall through
      to the real resolver (so numeric forms like ``0xa9fea9fe`` resolve the
      way the OS resolves them).
    * every connect to 169.254.169.254 (any port) is routed to a local
      metadata stand-in; ``hits`` lists what reached it.
    """

    def __init__(self, monkeypatch, dns=None):
        self.table = {k: list(v) if isinstance(v, list) else [v] for k, v in (dns or {}).items()}
        self.lookups: list = []
        handler = type("T", (_TrapHandler,), {"hits": []})
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.hits = handler.hits
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        trap_addr = ("127.0.0.1", self._srv.server_address[1])
        real_gai = socket.getaddrinfo

        def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            name = host.decode() if isinstance(host, bytes) else host
            answers = self.table.get(name) if isinstance(name, str) else None
            if not answers:
                return real_gai(host, port, family, type, proto, flags)
            self.lookups.append(name)
            ip = answers.pop(0) if len(answers) > 1 else answers[0]
            p = int(port) if port not in (None, "") and str(port).isdigit() else 0
            if ":" in ip:
                return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, p, 0, 0))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p))]

        real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

        def route(addr):
            if isinstance(addr, tuple) and addr and addr[0] == METADATA_IP:
                return trap_addr
            return addr

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(socket.socket, "connect", lambda s, a: real_connect(s, route(a)))
        monkeypatch.setattr(socket.socket, "connect_ex", lambda s, a: real_connect_ex(s, route(a)))

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


class _TcpRecorder(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.seen.append(b"")
        idx = len(self.server.seen) - 1
        if self.server.greeting:
            self.request.sendall(self.server.greeting)
        self.request.settimeout(3)
        try:
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                self.server.seen[idx] += chunk
                if self.server.reply_to:
                    reply = self.server.reply_to(chunk)
                    if reply:
                        self.request.sendall(reply)
        except OSError:
            pass


@contextmanager
def tcp_server(greeting: bytes = b"", reply_to=None):
    """Yield ``(port, seen)``: a raw TCP service on 127.0.0.1 (an internal
    SIEM, mail or database server …); ``seen`` holds the bytes of each
    connection it accepted."""
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TcpRecorder)
    srv.daemon_threads = True
    srv.seen, srv.greeting, srv.reply_to = [], greeting, reply_to
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], srv.seen
    finally:
        srv.shutdown()
        srv.server_close()
