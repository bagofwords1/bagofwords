#!/usr/bin/env python3
"""Mock KMS Lighthouse public API: the test boundary for the KMS connector.

No KMS Lighthouse tenant is available, so this simulates the surface
`KmsLighthouseClient` touches, with shapes taken from the KMS Lighthouse
Public API Documentation 5.0 and the public username/password login:

  POST {PREFIX}/login?username&password     cookie and/or token (STATE["login_mode"])
  GET  {PREFIX}/sessionInfo                 data.userId, data.userFullName, data.username
  GET  {PREFIX}/cm/tree/branch/view         ?parentId; ids and statuses are strings

Faithfulness rules enforced on purpose:
  - A rejected login answers a bare HTTP 400 with no body.
  - The tree's "error" answer is HTTP 200 {"status": "SUCCESS", "data": []}.
  - Every route other than /login needs a session cookie, a login token or a
    pasted API token (TOKENS); a missing or expired session answers 401, or a
    302 to the login page when STATE["expired_response"] == "302".
  - Each user sees only the tree nodes in their `visible` set.

Test-only switches and routes (not part of the KMS API):
  - STATE["session_expiry"] = "immediate": every session /login hands out is
    already expired when it is used (pasted API tokens stay valid).
  - STATE["hold_stale_401s"] = N: the first N requests that carry an expired
    session wait on one threading.Barrier(N, timeout=5) before they get their
    401/302, and are counted in STATE["stale_401s"]; a broken barrier answers
    500, so a test fails instead of hanging.
  - GET {PREFIX}/_mock/status_error/<id>  HTTP 200 {"status": "ERROR"} echoing the id
  - GET {PREFIX}/_mock/held               waits for HELD["release"] (sets HELD["arrived"] first),
                                          so a test can act while a KMS request is in flight
  - STATE["hold_login"] = True: POST /login waits the same way on HELD, so a
    test can act while a login is in flight.
  - GET {PREFIX}/_mock/empty              HTTP 200 with an empty body
  - GET {PREFIX}/_mock/oversized          HTTP 200 with a large JSON body (OVERSIZED_BYTES)

Users: alice, bob and svc (the system account used by e2e). Pasted API
tokens are in TOKENS.

Run:  python tools/kms_lighthouse/mock_kms_server.py --port 8096
"""
from __future__ import annotations

import argparse
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PREFIX = "/kms/lh/api"
SESSION_COOKIE = "JSESSIONID"
# Planted in unexpected response shapes; it must never reach client output.
ODD_MARKER = "kms-odd-marker-5c1e"

# Content-management tree: node id -> record. "parent" None is the top level.
TREE = {
    "78": {"parent": None, "title": "Accounts", "type": "FOLDER", "status": "2"},
    "90": {"parent": None, "title": "Procedures", "type": "FOLDER", "status": "2"},
    "95": {"parent": None, "title": "Archive", "type": "FOLDER", "status": "2"},
    "279": {"parent": "78", "title": "Reset a customer password", "type": "ITEM", "status": "2"},
    "301": {"parent": "90", "title": "Billing dispute", "type": "ITEM", "status": "2"},
}

# "session_username" is what sessionInfo reports; it differs from the login
# name on purpose, so a test can tell a reported identity from the typed one.
USERS = {
    "alice": {"password": "alice-kms-pass", "user_id": 11, "full_name": "Alice Agent",
              "session_username": "alice.agent", "visible": {"78", "90", "95", "279", "301"}},
    "bob": {"password": "bob-kms-pass", "user_id": 12, "full_name": "Bob Billing",
            "session_username": "bob.billing", "visible": {"90", "301"}},
    "svc": {"password": "svc-kms-pass", "user_id": 13, "full_name": "BOW Service",
            "session_username": "bow.service", "visible": {"78", "90", "95", "279", "301"}},
}

# Pasted API tokens (the "token" auth variant) -> user.
TOKENS = {
    "alice-api-token-1": "alice",
    "bob-api-token-1": "bob",
    "svc-api-token-1": "svc",
}

# Size of the /_mock/oversized body (a test lowers the client's cap below it).
OVERSIZED_BYTES = 256 * 1024

# Allowed values of the switches a test sets; anything else is a typo and raises.
_SWITCHES = {
    "login_mode": ("cookie", "token", "both", "none"),
    "expired_response": ("401", "302"),
    "session_expiry": ("never", "immediate"),
    "odd_shapes": (False, "list", "error", "bad_id", "bad_username"),
    "ai_text_available": (True, False),
    "top_level_requires_parent": (True, False),
    "hold_login": (True, False),
}
# Counters and sizes: non-negative integers (bool is not an int here).
_COUNTERS = ("login_count", "accepted_logins", "hold_stale_401s", "stale_401s")
_DEFAULTS = {
    "login_mode": "cookie",          # cookie | token | both | none (SUCCESS, no session)
    "expired_response": "401",       # 401 | 302
    "session_expiry": "never",       # never | immediate (login sessions are dead on arrival)
    "ai_text_available": True,
    "top_level_requires_parent": False,
    "hold_login": False,             # True: POST /login waits on HELD like /_mock/held
    "login_count": 0,                # every login attempt
    "accepted_logins": 0,            # logins that returned SUCCESS
    "hold_stale_401s": 0,            # N > 0: the first N stale-session requests meet at a barrier
    "stale_401s": 0,                 # requests that carried an expired session
    "extra_results": [],
    "odd_shapes": False,             # False | "list" | "error" | "bad_id" | "bad_username"
}
_STALE = {"barrier": None}           # the barrier for STATE["hold_stale_401s"]
# GET {PREFIX}/_mock/held sets "arrived", then waits up to 5 s for "release".
HELD = {"arrived": threading.Event(), "release": threading.Event()}


class _State(dict):
    def __setitem__(self, key, value):
        if key not in _DEFAULTS:
            raise KeyError(f"STATE has no switch {key!r}")
        allowed = _SWITCHES.get(key)
        # `True == 1` would pass a plain `in` check, so compare types too.
        if allowed is not None and not any(type(value) is type(a) and value == a for a in allowed):
            raise ValueError(f"STATE[{key!r}] must be one of {allowed!r}, not {value!r}")
        if key in _COUNTERS and (type(value) is not int or value < 0):
            raise ValueError(f"STATE[{key!r}] must be a non-negative int, not {value!r}")
        if key == "hold_stale_401s":
            old = _STALE["barrier"]
            if old is not None:
                old.abort()  # release any thread still waiting on the old barrier
            _STALE["barrier"] = threading.Barrier(value, timeout=5) if value else None
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


STATE: dict = _State()
REQUEST_LOG: list = []
_SESSIONS: dict = {}        # session cookie value -> user
_LOGIN_TOKENS: dict = {}    # token issued by /login -> user
_LOCK = threading.Lock()


def reset():
    """Restore the default state; called before every test."""
    with _LOCK:
        STATE.clear()
        STATE.update({key: (list(value) if isinstance(value, list) else value) for key, value in _DEFAULTS.items()})
        REQUEST_LOG.clear()
        _SESSIONS.clear()
        _LOGIN_TOKENS.clear()
        for event in HELD.values():
            event.clear()


def expire_sessions():
    """Invalidate every session opened by /login (pasted API tokens stay valid)."""
    with _LOCK:
        _SESSIONS.clear()
        _LOGIN_TOKENS.clear()


reset()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output quiet
        pass

    # ------------------------------------------------------------ plumbing
    def _send_json(self, status, body, headers=None):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status, headers=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _identity(self):
        auth = self.headers.get("Authorization") or ""
        login_sessions_live = STATE["session_expiry"] == "never"
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):]
            user = TOKENS.get(token) or (_LOGIN_TOKENS.get(token) if login_sessions_live else None)
            if user:
                return user
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value in _SESSIONS and login_sessions_live:
                return _SESSIONS[value]
        return None

    def _carries_session(self):
        """True when the request sends a session (cookie or Bearer) that was not accepted."""
        cookie = self.headers.get("Cookie") or ""
        has_cookie = any(part.strip().startswith(SESSION_COOKIE + "=") for part in cookie.split(";"))
        return has_cookie or (self.headers.get("Authorization") or "").startswith("Bearer ")

    def _unauthenticated(self):
        if self._carries_session():
            with _LOCK:
                index = STATE["stale_401s"]
                STATE["stale_401s"] = index + 1
                barrier = _STALE["barrier"] if index < STATE["hold_stale_401s"] else None
            if barrier is not None:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    return self._send_empty(500)
        if STATE["expired_response"] == "302":
            self._send_empty(302, {"Location": "/kms/lh/login"})
        else:
            self._send_empty(401)

    # ------------------------------------------------------------ routes
    def do_POST(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path != PREFIX + "/login":
            REQUEST_LOG.append((self._identity(), "POST", parts.path))
            return self._send_empty(404)
        username = (query.get("username") or [""])[0]
        password = (query.get("password") or [""])[0]
        if STATE["hold_login"]:
            HELD["arrived"].set()
            if not HELD["release"].wait(5):
                return self._send_empty(500)   # never released: fail instead of hanging
        with _LOCK:
            REQUEST_LOG.append((username or None, "POST", parts.path))
            STATE["login_count"] += 1
            record = USERS.get(username)
            if not record or record["password"] != password:
                accepted = False
            else:
                accepted = True
                STATE["accepted_logins"] += 1
                mode = STATE["login_mode"]
                headers, data = {}, {"userId": record["user_id"]}
                if mode in ("cookie", "both"):
                    sid = secrets.token_hex(16)
                    _SESSIONS[sid] = username
                    headers["Set-Cookie"] = f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly"
                if mode in ("token", "both"):
                    token = "lt-" + secrets.token_hex(16)
                    _LOGIN_TOKENS[token] = username
                    data["token"] = token
        if not accepted:
            return self._send_empty(400)
        return self._send_json(200, {"status": "SUCCESS", "data": data}, headers)

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        user = self._identity()
        REQUEST_LOG.append((user, "GET", parts.path))
        if not parts.path.startswith(PREFIX + "/"):
            return self._send_empty(404)
        if user is None:
            return self._unauthenticated()
        route = parts.path[len(PREFIX):]
        odd = STATE["odd_shapes"]
        if route == "/sessionInfo":
            if odd == "list":
                return self._send_json(200, [ODD_MARKER])
            if odd == "error":
                return self._send_json(200, {"status": "ERROR", "exception": ODD_MARKER, "message": ODD_MARKER})
            if odd == "bad_id":
                return self._send_json(200, {"status": "SUCCESS", "data": [ODD_MARKER]})
            record = USERS[user]
            username = {"name": ODD_MARKER} if odd == "bad_username" else record["session_username"]
            return self._send_json(200, {"status": "SUCCESS", "data": {
                "userId": record["user_id"], "userFullName": record["full_name"], "username": username}})
        if route == "/cm/tree/branch/view":
            if odd == "list":
                return self._send_json(200, [ODD_MARKER])
            if odd == "error":
                return self._send_json(200, {"status": "ERROR", "exception": ODD_MARKER})
            if odd == "bad_id":
                return self._send_json(200, {"status": "SUCCESS", "data": [
                    {"id": "x-" + ODD_MARKER, "title": ODD_MARKER, "type": "FOLDER", "status": "2"}]})
            parent = (query.get("parentId") or [None])[0]
            if parent is None and STATE["top_level_requires_parent"]:
                return self._send_json(200, {"status": "SUCCESS", "data": []})
            visible = USERS[user]["visible"]
            data = [
                {"id": node_id, "language": "en", "title": node["title"], "type": node["type"],
                 "version": "1", "status": node["status"], "state": "ACTIVE",
                 "sons": str(sum(1 for n in TREE.values() if n["parent"] == node_id))}
                for node_id, node in TREE.items()
                if node["parent"] == parent and node_id in visible
            ]
            return self._send_json(200, {"status": "SUCCESS", "data": data})
        if route.startswith("/_mock/status_error/"):
            item_id = route[len("/_mock/status_error/"):]
            return self._send_json(200, {"status": "ERROR", "exception": f"Item {item_id} was not found",
                                         "message": f"no item {item_id}"})
        if route == "/_mock/held":
            HELD["arrived"].set()
            if not HELD["release"].wait(5):
                return self._send_empty(500)   # never released: fail instead of hanging
            return self._send_json(200, {"status": "SUCCESS", "data": []})
        if route == "/_mock/empty":
            return self._send_empty(200)
        if route == "/_mock/oversized":
            filler = "x" * OVERSIZED_BYTES
            return self._send_json(200, {"status": "SUCCESS", "data": filler})
        return self._send_empty(404)


def make_server(port: int = 0, host: str = "127.0.0.1"):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Mock KMS Lighthouse public API")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = make_server(args.port, args.host)
    print(f"Mock KMS Lighthouse listening on http://{args.host}:{args.port}{PREFIX}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
