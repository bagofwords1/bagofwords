#!/usr/bin/env python3
"""Mock KMS Lighthouse public API: the test boundary for the KMS connector.

No KMS Lighthouse tenant is available, so this simulates the surface
`KmsLighthouseClient` touches, with shapes taken from the KMS Lighthouse
Public API Documentation 5.0 and the public username/password login:

  POST {PREFIX}/login?username&password     cookie and/or token (STATE["login_mode"])
  GET  {PREFIX}/sessionInfo                 data.userId, data.userFullName, data.username
  GET  {PREFIX}/cm/tree/branch/view         ?parentId; ids and statuses are strings
  GET  {PREFIX}/search/full                 ?query&start&rows[&layoutId] -> regularSearch.results[]

Faithfulness rules enforced on purpose:
  - A rejected login answers a bare HTTP 400 with no body.
  - The tree's "error" answer is HTTP 200 {"status": "SUCCESS", "data": []}.
  - Every route other than /login needs a session cookie, a login token or a
    pasted API token (TOKENS); a missing or expired session answers 401, or a
    302 to the login page when STATE["expired_response"] == "302".
  - Each user sees only the tree nodes in their `visible` set, and only the
    search results whose `visible` set names them. The tree has a nested
    folder (Procedures/Billing), two sibling folders with one title (Forms),
    an offline item, a scenario node, hostile folder titles
    (HOSTILE_FOLDER_TITLES) and a wide folder (WIDE_FOLDER) with more
    subfolders than the client's folder-call cap.
  - Search does not rank or filter by the query: every query matches every
    result the user may see, paged by `start`/`rows`. There is no length rule
    on `query` (the PDF states none); a 400 with no body is the PDF's
    "message too long" answer (STATE["search_400"]).

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
  - STATE["hold_stale_session"] = True: a request carrying an expired session
    waits the same way on HELD before it gets its 401/302.
  - Search: every /search/full call is counted in STATE["search_count"], and
    its query, start, rows and layoutId (None when absent) are recorded in
    STATE["last_search_params"]. STATE["extra_results"] holds extra result
    records (visible to every user unless they carry a `visible` set).
    STATE["search_body"], when not None, is sent as the whole /search/full
    JSON body instead (an odd shape a test wants to see handled).
    STATE["hold_search_after_first"] = True: a /search/full call with
    start > 0 waits on HELD before it answers (a slow later page).
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

# Content-management tree: node id -> record. "parent" None is the top level
# (asked for with no parentId). "subType" and "updateDate" are sent when set.
TREE = {
    "78": {"parent": None, "title": "Accounts", "type": "FOLDER", "status": "2"},
    "10": {"parent": None, "title": "Procedures", "type": "FOLDER", "status": "2"},
    "11": {"parent": None, "title": "Restricted", "type": "FOLDER", "status": "2"},     # alice only
    "95": {"parent": None, "title": "Archive", "type": "FOLDER", "status": "2"},        # empty
    "279": {"parent": "78", "title": "Reset a customer password", "type": "ITEM", "status": "2"},
    "301": {"parent": "10", "title": "Billing dispute", "type": "ITEM", "status": "2",
            "subType": "T_PROCEDURE", "updateDate": "10/04/2024"},
    "305": {"parent": "10", "title": "Policy v2.1 / summary", "type": "ITEM", "status": "2"},
    "306": {"parent": "10", "title": "Draft refund rules", "type": "ITEM", "status": "1"},   # offline
    "12": {"parent": "10", "title": "Billing", "type": "FOLDER", "status": "2"},
    "13": {"parent": "10", "title": "Forms", "type": "FOLDER", "status": "2"},
    "14": {"parent": "10", "title": "Forms", "type": "FOLDER", "status": "2"},             # same title as 13
    "302": {"parent": "12", "title": "Refund a payment", "type": "ITEM", "status": "2"},
    "4401": {"parent": "12", "title": "Card blocked flow", "type": "ITEM", "subType": "T_SCENARIO_FLOW",
             "status": "2"},
    "303": {"parent": "13", "title": "Change of address form", "type": "ITEM", "status": "2"},
    "304": {"parent": "14", "title": "Card replacement form", "type": "ITEM", "status": "2"},
    "307": {"parent": "11", "title": "Escalation path", "type": "ITEM", "status": "2"},
}
# Hostile folder titles, each holding one item: a path segment must still round-trip.
HOSTILE_FOLDER_TITLES = {
    "20": "Long " + "x" * 245,
    "21": "tab\there\nnewline",
    "22": "..",
    "23": "folder:7 Archive",
    "24": "<b>Bold</b> &amp; HTML",
}
for _folder_id, _title in HOSTILE_FOLDER_TITLES.items():
    TREE[_folder_id] = {"parent": None, "title": _title, "type": "FOLDER", "status": "2"}
    TREE[str(int(_folder_id) + 300)] = {"parent": _folder_id, "title": "Inside " + _folder_id, "type": "ITEM",
                                        "status": "2"}
# A wide folder (listed last): more subfolders than the client's folder-call cap.
WIDE_FOLDER = "30"
WIDE_CHILDREN = 60
TREE[WIDE_FOLDER] = {"parent": None, "title": "Wide", "type": "FOLDER", "status": "2"}
for _n in range(WIDE_CHILDREN):
    TREE[str(1000 + _n)] = {"parent": WIDE_FOLDER, "title": f"Wide {_n}", "type": "FOLDER", "status": "2"}
    TREE[str(2000 + _n)] = {"parent": str(1000 + _n), "title": f"Wide item {_n}", "type": "ITEM", "status": "2"}
_RESTRICTED = {"11", "307"}

# Planted in search titles and descriptions; neither may reach client output.
# Both are made-up test values (the JWT is a well-formed but unsigned dummy).
CONTENT_JWT = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJtb2NrLXNlYXJjaCJ9.bW9jay1zaWduYXR1cmU"
URL_TOKEN = "mock-url-token-3b9e71"

# /search/full results (PDF shape), plus who may see each one.
SEARCH_RESULTS = [
    {"id": "279", "subType": "T_PROCEDURE", "title": "Reset a customer <strong>password</strong>",
     "description": "Steps to <strong>reset</strong> a password from the agent desk.",
     "status": "Online", "statusCode": "2", "updateDate": "10/04/2024", "visible": {"alice", "svc"}},
    {"id": "301", "subType": "T_PROCEDURE", "title": "<strong>Billing</strong> dispute",
     "description": "Open a <strong>billing</strong> dispute &amp; track it.",
     "status": "Online", "statusCode": "2", "updatedDate": "2024-05-09 12:57:56.0",
     "visible": {"alice", "bob", "svc"}},
    {"id": "4400", "subType": "T_SCENARIO_FLOW", "title": "Card <strong>blocked</strong> flow",
     "description": "A decision tree for a blocked card.", "status": "Online", "statusCode": "2",
     "visible": {"alice", "bob", "svc"}},
    {"id": "4410", "subType": "T_PROCEDURE", "title": "Draft <strong>refund</strong> rules",
     "description": "Not published yet.", "status": "Offline", "statusCode": "1",
     "visible": {"alice", "bob", "svc"}},
    {"id": "4420", "subType": "T_PROCEDURE",
     "title": f"Token notes {CONTENT_JWT} https://kms.example.com/contents?token={URL_TOKEN}",
     "description": (f"See https://kms.example.com/contents?id=7&amp;token={URL_TOKEN}&amp;lang=en and "
                     f"<strong>bearer</strong> {CONTENT_JWT} for details."),
     "status": "Online", "statusCode": "2", "visible": {"alice", "bob", "svc"}},
    {"id": "4430", "subType": "T_PROCEDURE", "title": "Restricted <strong>escalation</strong> path",
     "description": "Managers only.", "status": "Online", "statusCode": "2", "visible": {"alice"}},
    {"id": "4440", "type": "FOLDER", "title": "A folder hit", "description": "", "status": "Online",
     "statusCode": "2", "visible": {"alice", "bob", "svc"}},
]

# "session_username" is what sessionInfo reports; it differs from the login
# name on purpose, so a test can tell a reported identity from the typed one.
USERS = {
    "alice": {"password": "alice-kms-pass", "user_id": 11, "full_name": "Alice Agent",
              "session_username": "alice.agent", "visible": set(TREE)},
    "bob": {"password": "bob-kms-pass", "user_id": 12, "full_name": "Bob Billing",
            "session_username": "bob.billing", "visible": {"10", "301", "12", "302"}},
    "svc": {"password": "svc-kms-pass", "user_id": 13, "full_name": "BOW Service",
            "session_username": "bow.service", "visible": set(TREE) - _RESTRICTED},
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
    "hold_stale_session": (True, False),
    "search_400": (True, False),
    "hold_search_after_first": (True, False),
}
# Counters and sizes: non-negative integers (bool is not an int here).
_COUNTERS = ("login_count", "accepted_logins", "hold_stale_401s", "stale_401s", "search_count")
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
    "hold_stale_session": False,     # True: a stale-session request waits on HELD before its 401/302
    "search_400": False,             # True: /search/full answers 400 with no body ("message too long")
    "hold_search_after_first": False,  # True: a /search/full past the first page waits on HELD
    "search_count": 0,               # /search/full requests
    "last_search_params": None,      # {"query", "start", "rows", "layoutId"} of the last /search/full
    "extra_results": [],
    "search_body": None,             # None | a JSON value sent as the whole /search/full body
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
            if STATE["hold_stale_session"]:
                HELD["arrived"].set()
                if not HELD["release"].wait(5):
                    return self._send_empty(500)   # never released: fail instead of hanging
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
                 "sons": str(sum(1 for n in TREE.values() if n["parent"] == node_id)),
                 **{key: node[key] for key in ("subType", "updateDate") if key in node}}
                for node_id, node in TREE.items()
                if node["parent"] == parent and node_id in visible
            ]
            return self._send_json(200, {"status": "SUCCESS", "data": data})
        if route == "/search/full":
            return self._search(user, query, odd)
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

    def _search(self, user, query, odd):
        params = {key: (query.get(key) or [None])[0] for key in ("query", "start", "rows", "layoutId")}
        with _LOCK:
            STATE["search_count"] += 1
            STATE["last_search_params"] = params
        if STATE["search_400"]:
            return self._send_empty(400)
        if STATE["hold_search_after_first"] and params["start"] not in (None, "0"):
            HELD["arrived"].set()
            if not HELD["release"].wait(5):
                return self._send_empty(500)   # never released: fail instead of hanging
            self.close_connection = True       # the client has usually given up by now
            try:
                return self._send_json(200, {"regularSearch": {"query": params["query"], "results": []}})
            except (BrokenPipeError, ConnectionResetError):
                return None
        if STATE["search_body"] is not None:
            return self._send_json(200, STATE["search_body"])
        if odd == "list":
            return self._send_json(200, [ODD_MARKER])
        if odd == "error":
            return self._send_json(200, {"status": "ERROR", "message": ODD_MARKER, "params": {}})
        if odd == "bad_id":
            return self._send_json(200, {"regularSearch": {"query": ODD_MARKER, "results": [
                {"id": "x-" + ODD_MARKER, "title": ODD_MARKER, "description": ODD_MARKER, "status": "Online"}]}})
        try:
            start, rows = int(params["start"] or 0), int(params["rows"] or 10)
        except ValueError:
            return self._send_empty(400)
        visible = [r for r in SEARCH_RESULTS + list(STATE["extra_results"]) if user in r.get("visible", (user,))]
        page = [{k: v for k, v in r.items() if k != "visible"} for r in visible[start:start + rows]]
        return self._send_json(200, {"regularSearch": {
            "query": params["query"], "suggestedQueryAlternative": None, "results": page}})


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
