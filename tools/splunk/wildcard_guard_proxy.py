"""Reverse proxy in front of Splunk's management port that emulates a hardened
deployment where wildcard index searches are administratively blocked.

Policy (mirrors the customer-reported behavior):
  - `| metadata ...` searches pass through (metadata commands bypass the guard).
  - Any other search whose SPL references a wildcard index (`index=*`) is
    rejected with a Splunk-style ERROR message: "You must specify a specific
    index...".
  - Everything else (server info, index-scoped searches, etc.) passes through.

Listens on http://0.0.0.0:8091 → forwards to https://127.0.0.1:8089.
Every search decision is logged to stdout.
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import requests
import urllib3

urllib3.disable_warnings()

UPSTREAM = "https://127.0.0.1:8089"
WILDCARD_INDEX = re.compile(r'index\s*(?:=|::|\bIN\b)\s*"?\*', re.IGNORECASE)

BLOCK_BODY = json.dumps({
    "messages": [{
        "type": "ERROR",
        "text": ("Search not executed: You must specify a specific index for this "
                 "search. Global wildcard index searches (e.g. index=*) are not "
                 "permitted on this deployment. Contact your Splunk administrator."),
    }],
    "results": [],
    "preview": False,
}).encode()

HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "host",
               "content-length", "content-encoding"}


def blocked(spl: str) -> bool:
    s = (spl or "").strip()
    if s.startswith("|") and s.lstrip("|").strip().lower().startswith("metadata"):
        return False  # metadata commands bypass the guard
    return bool(WILDCARD_INDEX.search(s))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("[proxy] " + fmt % args + "\n")
        sys.stdout.flush()

    def _forward(self, body: bytes | None):
        url = f"{UPSTREAM}{self.path}"
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS}
        resp = requests.request(self.command, url, data=body, headers=headers,
                                verify=False, timeout=300, proxies={"http": None, "https": None})
        payload = resp.content
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reject(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(BLOCK_BODY)))
        self.end_headers()
        self.wfile.write(BLOCK_BODY)

    def do_GET(self):
        self._forward(None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/services/search/jobs"):
            form = parse_qs(body.decode("utf-8", "replace"))
            spl = (form.get("search") or [""])[0]
            if blocked(spl):
                print(f"[guard] BLOCKED wildcard search: {spl[:200]}", flush=True)
                return self._reject()
            print(f"[guard] allowed search: {spl[:200]}", flush=True)
        self._forward(body)

    do_DELETE = do_GET
    do_PUT = do_POST


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", 8091), Handler)
    print("guard proxy on :8091 ->", UPSTREAM, flush=True)
    srv.serve_forever()
