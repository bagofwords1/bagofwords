#!/usr/bin/env python3
"""Local HTTP API returning a large JSON order list — the "custom API request"
a scheduled task pulls every run (scheduled-task context-growth feedback loop).

    GET /orders  ->  {"orders": [...FAKE_API_ROWS rows...], "generated_at": ...}

Env: FAKE_API_PORT (default 9199), FAKE_API_ROWS (default 1500, ~100K chars).
Run:  python3 tools/agent/scheduled_growth_fake_api.py
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROWS = int(os.environ.get("FAKE_API_ROWS", "1500"))
REGIONS = ["north", "south", "east", "west", "central"]


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):  # noqa: N802 — connection reachability check
        self.send_response(200)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not self.path.startswith("/orders"):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        day = int(time.time())
        orders = [
            {"order_id": f"o-{day}-{i}", "region": REGIONS[i % 5], "customer": f"customer-{i % 211}",
             "amount": round((i * 37 % 997) * 1.17, 2), "status": "paid" if i % 9 else "refunded"}
            for i in range(ROWS)
        ]
        body = json.dumps({"orders": orders, "generated_at": day}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(os.environ.get("FAKE_API_PORT", "9199"))), Handler).serve_forever()
