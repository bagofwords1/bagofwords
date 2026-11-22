#!/usr/bin/env python3
"""Mock Confluence Data Center REST API: the test boundary for the confluence_dc connector.

No Confluence Data Center instance is available in CI, so this simulates the
REST surface `ConfluenceDcClient` touches, with shapes taken from the
Confluence Data Center REST API (7.x-10.x, `/rest/api/...`):

  GET {CTX}/rest/api/user/current             {type: known, username, userKey, displayName}
                                              ({type: anonymous} with no credentials and
                                              STATE["anonymous_access"])
  GET {CTX}/rest/api/space                    ?start&limit[&spaceKey=K...] -> PageResponse of spaces
  GET {CTX}/rest/api/space/{key}              one space; 404 when unknown or not visible
  GET {CTX}/rest/api/space/{key}/content/{t}  t = page|blogpost; ?depth=root|all&start&limit&expand
  GET {CTX}/rest/api/content                  ?spaceKey&title&type&expand -> PageResponse (title lookup)
  GET {CTX}/rest/api/content/{id}             ?expand=body.export_view,version,space,ancestors
                                              404 when unknown OR restricted for this user
                                              (Confluence does not reveal restricted pages)
  GET {CTX}/rest/api/content/{id}/child/page  ?start&limit&expand
  GET {CTX}/rest/api/content/search           ?cql&start&limit&expand -> PageResponse of content
  GET {CTX}/rest/api/search                   ?cql&start&limit -> search results with excerpt
                                              (highlights wrapped in @@@hl@@@ ... @@@endhl@@@)

`CTX` is the context path the server is started with (default "/confluence",
as on a typical on-prem install behind https://host/confluence).

Faithfulness rules enforced on purpose:
  - Auth is per request: `Authorization: Bearer <PAT>` or HTTP Basic. A bad
    credential answers 401 with `X-Seraph-LoginReason: AUTHENTICATED_FAILED`;
    a user under CAPTCHA (STATE["captcha_users"]) answers 401 with
    `X-Authentication-Denied-Reason: CAPTCHA_CHALLENGE` even with the right
    password, as Data Center does after repeated failed logins.
  - Every user sees only the spaces in their `spaces` set and the pages whose
    `restricted_to` (if any) names them. A restricted page is a 404, and is
    missing from listings, children and search.
  - `limit` is capped server-side (STATE["max_limit"], default 25, as with
    expanded content on Data Center); clients must page with start/limit.
  - The CQL subset understood: `type in (...)`, `type = x`, `space in (...)`,
    `space = "K"`, `text ~ "..."`, `title ~ "..."`, `ancestor = N`, joined by
    AND, optional `ORDER BY ...` (ignored). Anything else is a 400 with a JSON
    `{"statusCode": 400, "message": "Could not parse cql : ..."}` body.
  - Search matches when every word of the `text ~` phrase is in the title or
    the page text (case-insensitive).

Test-only switches (STATE, reset with reset()):
  - "anonymous_access": unauthenticated requests are anonymous (not 401).
  - "captcha_users": set of usernames whose Basic logins get the CAPTCHA 401.
  - "pats_disabled": every PAT answers 401 (an admin turned PATs off).
  - "rate_limit_next": the next N requests answer 429 with Retry-After
    (STATE["retry_after"], default "0").
  - "sso_redirect": every request answers 302 to {CTX}/login.action.
  - "html_page": every request answers 200 text/html (a proxy or login page).
  - "max_limit": the server-side limit cap.
  - "oversized": a content id whose body is larger than any client cap.
  - "fail_status": {path_suffix: status} forces a status for matching paths.
  REQUEST_LOG records (user or None, method, path) for every request.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DEFAULT_CONTEXT = "/confluence"

USERS = {
    "alice": {"password": "alice-conf-pass-7f3", "display": "Alice Admin", "key": "ff80aaa1",
              "spaces": {"ENG", "HR", "SEC"}},
    "bob": {"password": "bob-conf-pass-2c9", "display": "Bob Builder", "key": "ff80bbb2",
            "spaces": {"ENG", "HR"}},
    "svc": {"password": "svc-conf-pass-91d", "display": "BOW Service", "key": "ff80ccc3",
            "spaces": {"ENG", "HR"}},
}
# Personal access tokens (Bearer). Data Center PATs are opaque base64-ish strings.
TOKENS = {
    "MjAxNTY3ODkwMTIzOkFsaWNlUEFUc2VjcmV0": "alice",
    "MzAxNTY3ODkwMTIzOkJvYlBBVHNlY3JldDAx": "bob",
    "NDAxNTY3ODkwMTIzOlN2Y1BBVHNlY3JldDAy": "svc",
}

SPACES = {
    "ENG": {"id": 98301, "name": "Engineering", "type": "global"},
    "HR": {"id": 98302, "name": "Human Resources", "type": "global"},
    "SEC": {"id": 98303, "name": "Security", "type": "global"},
}

_FAILOVER_BODY = """
<p>This runbook moves the primary <strong>orders</strong> database to the standby region.</p>
<div class="confluence-information-macro confluence-information-macro-warning">
  <div class="confluence-information-macro-body"><p>Only the on-call DBA may start a failover.</p></div>
</div>
<h2>Steps</h2>
<table class="confluenceTable"><tbody>
<tr><th class="confluenceTh">Step</th><th class="confluenceTh">Action</th><th class="confluenceTh">Owner</th></tr>
<tr><td class="confluenceTd">1</td><td class="confluenceTd">Freeze writes on <code>orders-primary</code></td><td class="confluenceTd">DBA</td></tr>
<tr><td class="confluenceTd">2</td><td class="confluenceTd">Promote <code>orders-standby</code> (RTO 15 min)</td><td class="confluenceTd">DBA</td></tr>
<tr><td class="confluenceTd">3</td><td class="confluenceTd">Flip DNS &amp; notify #incidents</td><td class="confluenceTd">SRE</td></tr>
</tbody></table>
<div class="code panel pdl"><div class="codeContent panelContent pdl">
<pre class="syntaxhighlighter-pre">pg_ctl promote -D /var/lib/postgresql/data</pre>
</div></div>
<ul><li>Check replication lag first</li><li>Page the incident commander<ul><li>Use the bridge line</li></ul></li></ul>
"""

_ONCALL_BODY = """
<p>Primary on-call per week (restricted page: Alice and the service account only).</p>
<table><tr><th>Week</th><th>Primary</th><th>Secondary</th></tr>
<tr><td>2026-W47</td><td>Dana</td><td>Eli</td></tr>
<tr><td>2026-W48</td><td>Eli</td><td>Farah</td></tr></table>
"""

_VACATION_BODY = """
<h1>Vacation policy</h1>
<p>Paid vacation days per year depend on tenure:</p>
<table><thead><tr><th>Tenure (years)</th><th>Days</th></tr></thead>
<tbody><tr><td>0-2</td><td>20</td></tr><tr><td>3-5</td><td>23</td></tr><tr><td>6+</td><td>26</td></tr></tbody></table>
<p>Unused days carry over up to <em>5 days</em>.</p>
"""

# id -> page. `parent` is the parent page id (None: a root page of its space).
PAGES = {
    1001: {"type": "page", "title": "Engineering Home", "space": "ENG", "parent": None,
           "body": "<p>Welcome to the Engineering space. See Runbooks and the Release Process.</p>",
           "version": 7, "when": "2026-10-02T09:15:00.000Z", "by": "Alice Admin"},
    1002: {"type": "page", "title": "Runbooks", "space": "ENG", "parent": 1001,
           "body": "<p>Operational runbooks for production systems.</p>",
           "version": 3, "when": "2026-09-14T12:00:00.000Z", "by": "Bob Builder"},
    1003: {"type": "page", "title": "Database Failover", "space": "ENG", "parent": 1002, "body": _FAILOVER_BODY,
           "version": 12, "when": "2026-11-12T16:40:00.000Z", "by": "Dana DBA"},
    1004: {"type": "page", "title": "On-call Rotation", "space": "ENG", "parent": 1002, "body": _ONCALL_BODY,
           "version": 30, "when": "2026-11-18T08:00:00.000Z", "by": "Eli SRE", "restricted_to": {"alice", "svc"}},
    1005: {"type": "page", "title": "Release Process", "space": "ENG", "parent": 1001,
           "body": "<p>Releases ship every Tuesday after the change advisory board approves them.</p>",
           "version": 5, "when": "2026-08-01T10:00:00.000Z", "by": "Bob Builder"},
    1006: {"type": "page", "title": "CI / CD Pipelines", "space": "ENG", "parent": 1005,
           "body": "<p>Every merge to main runs the full pipeline; a failed failover drill blocks the release.</p>",
           "version": 2, "when": "2026-08-03T10:00:00.000Z", "by": "Bob Builder"},
    2001: {"type": "blogpost", "title": "Q3 incident review", "space": "ENG", "parent": None,
           "body": "<p>Two incidents in Q3; the database failover took 22 minutes, above the 15 minute RTO.</p>",
           "version": 1, "when": "2026-10-05T07:30:00.000Z", "by": "Alice Admin"},
    1101: {"type": "page", "title": "HR Home", "space": "HR", "parent": None,
           "body": "<p>Policies and benefits.</p>", "version": 2, "when": "2026-01-10T09:00:00.000Z", "by": "Hana HR"},
    1102: {"type": "page", "title": "Vacation Policy", "space": "HR", "parent": 1101, "body": _VACATION_BODY,
           "version": 9, "when": "2026-06-30T09:00:00.000Z", "by": "Hana HR"},
    1201: {"type": "page", "title": "Security Home", "space": "SEC", "parent": None,
           "body": "<p>Security team space.</p>", "version": 1, "when": "2026-02-01T09:00:00.000Z", "by": "Alice Admin"},
    1202: {"type": "page", "title": "Incident Response Plan", "space": "SEC", "parent": 1201,
           "body": "<p>Severity 1 incidents page the CISO within 10 minutes. The database failover runbook applies.</p>",
           "version": 4, "when": "2026-07-07T09:00:00.000Z", "by": "Alice Admin"},
}

STATE: dict = {}
REQUEST_LOG: list = []
_LOCK = threading.Lock()


def reset() -> None:
    with _LOCK:
        STATE.clear()
        STATE.update({
            "anonymous_access": False, "captcha_users": set(), "pats_disabled": False, "rate_limit_next": 0,
            "retry_after": "0", "sso_redirect": False, "html_page": False, "max_limit": 25, "oversized": None,
            "fail_status": {}, "rate_limited": 0,
        })
        REQUEST_LOG.clear()


reset()

_HL_OPEN, _HL_CLOSE = "@@@hl@@@", "@@@endhl@@@"
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html or "")).strip()


def _visible(user: str | None, page_id: int) -> bool:
    page = PAGES.get(page_id)
    if page is None or user is None:
        return False
    if page["space"] not in USERS[user]["spaces"]:
        return False
    restricted = page.get("restricted_to")
    return restricted is None or user in restricted


def _ancestors(page_id: int) -> list[int]:
    chain, parent = [], PAGES[page_id]["parent"]
    while parent is not None:
        chain.append(parent)
        parent = PAGES[parent]["parent"]
    return list(reversed(chain))


class _CqlError(ValueError):
    pass


_CLAUSE_RES = [
    ("type_in", re.compile(r'^type\s+in\s*\(([^)]*)\)$', re.I)),
    ("type_eq", re.compile(r'^type\s*=\s*"?([A-Za-z]+)"?$', re.I)),
    ("space_in", re.compile(r'^space\s+in\s*\(([^)]*)\)$', re.I)),
    ("space_eq", re.compile(r'^space\s*=\s*"([^"]+)"$', re.I)),
    ("text", re.compile(r'^text\s*~\s*"((?:[^"\\]|\\.)*)"$', re.I)),
    ("title", re.compile(r'^title\s*~\s*"((?:[^"\\]|\\.)*)"$', re.I)),
    ("ancestor", re.compile(r'^ancestor\s*=\s*"?(\d+)"?$', re.I)),
]


def _list_values(raw: str) -> set[str]:
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.startswith('"') and part.endswith('"') and len(part) >= 2:
            part = part[1:-1]
        if not part:
            raise _CqlError("empty list value")
        out.add(part)
    return out


def _parse_cql(cql: str) -> list[tuple[str, object]]:
    body = re.split(r"\s+ORDER\s+BY\s+", cql.strip(), maxsplit=1, flags=re.I)[0]
    clauses = []
    # Split on AND outside quotes.
    parts, buf, quoted, esc = [], "", False, False
    tokens = re.split(r'(\s+AND\s+)', body, flags=re.I)
    for tok in tokens:
        if re.fullmatch(r'\s+AND\s+', tok, flags=re.I) and not quoted:
            parts.append(buf)
            buf = ""
            continue
        for ch in tok:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                quoted = not quoted
        buf += tok
    parts.append(buf)
    for part in parts:
        part = part.strip()
        for name, rx in _CLAUSE_RES:
            m = rx.match(part)
            if m:
                value = m.group(1)
                if name in ("type_in", "space_in"):
                    value = _list_values(value)
                elif name in ("text", "title"):
                    value = re.sub(r"\\(.)", r"\1", value)
                clauses.append((name, value))
                break
        else:
            raise _CqlError(part[:40])
    return clauses


def _cql_matches(user: str, page_id: int, clauses) -> bool:
    page = PAGES[page_id]
    for name, value in clauses:
        if name == "type_in" and page["type"] not in value:
            return False
        if name == "type_eq" and page["type"] != value:
            return False
        if name == "space_in" and page["space"] not in value:
            return False
        if name == "space_eq" and page["space"] != value:
            return False
        if name == "ancestor" and int(value) not in _ancestors(page_id):
            return False
        if name in ("text", "title"):
            hay = (page["title"] + " " + ("" if name == "title" else _plain(page["body"]))).lower()
            words = [w for w in re.split(r"\s+", str(value).lower()) if w]
            if not words or not all(w in hay for w in words):
                return False
    return True


def _excerpt(page: dict, clauses) -> str:
    text = _plain(page["body"])[:200]
    for name, value in clauses:
        if name == "text":
            for word in str(value).split():
                text = re.sub("(?i)(" + re.escape(word) + ")", _HL_OPEN + r"\1" + _HL_CLOSE, text, count=1)
    return text


class Handler(BaseHTTPRequestHandler):
    server_version = "MockConfluence/10.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    # ------------------------------------------------------------ helpers
    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None):
        self.send_response(status)
        if body or status not in (204, 302):
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass   # the client stopped reading (e.g. its size cap); not a server error

    def _json(self, status: int, payload, headers: dict | None = None):
        self._send(status, json.dumps(payload).encode("utf-8"), headers=headers)

    def _error(self, status: int, message: str, headers: dict | None = None):
        self._json(status, {"statusCode": status, "message": message, "data": {"authorized": False}}, headers)

    def _identify(self):
        """(user, failure) for the request's credentials; user None + failure None means anonymous."""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if STATE["pats_disabled"] or token not in TOKENS:
                return None, "bad"
            return TOKENS[token], None
        if auth.lower().startswith("basic "):
            try:
                user, _, password = base64.b64decode(auth[6:].strip()).decode("utf-8").partition(":")
            except Exception:
                return None, "bad"
            if user in STATE["captcha_users"]:
                return None, "captcha"
            if user not in USERS or USERS[user]["password"] != password:
                return None, "bad"
            return user, None
        if auth:
            return None, "bad"
        return None, None if STATE["anonymous_access"] else "bad"

    @staticmethod
    def _limit(params) -> tuple[int, int]:
        start = int((params.get("start") or ["0"])[0])
        limit = int((params.get("limit") or ["25"])[0])
        return max(0, start), max(1, min(limit, STATE["max_limit"]))

    def _page_response(self, items: list, start: int, limit: int, path: str):
        chunk = items[start:start + limit]
        payload = {"results": chunk, "start": start, "limit": limit, "size": len(chunk),
                   "_links": {"base": "http://" + self.headers.get("Host", "localhost") + self.server.context,
                              "context": self.server.context, "self": self.server.context + path}}
        if start + limit < len(items):
            payload["_links"]["next"] = f"{path}?start={start + limit}&limit={limit}"
        return payload

    def _space_json(self, key: str) -> dict:
        s = SPACES[key]
        return {"id": s["id"], "key": key, "name": s["name"], "type": s["type"], "status": "current",
                "_links": {"webui": f"/display/{key}"}}

    def _content_json(self, page_id: int, expand: set[str]) -> dict:
        page = PAGES[page_id]
        out = {"id": str(page_id), "type": page["type"], "status": "current", "title": page["title"],
               "_links": {"webui": f"/pages/viewpage.action?pageId={page_id}", "tinyui": f"/x/{page_id}"}}
        if "space" in expand:
            out["space"] = self._space_json(page["space"])
        if "version" in expand:
            out["version"] = {"number": page["version"], "when": page["when"],
                              "by": {"type": "known", "displayName": page["by"]}}
        if "ancestors" in expand:
            out["ancestors"] = [{"id": str(a), "type": "page", "title": PAGES[a]["title"]} for a in _ancestors(page_id)]
        if "body.export_view" in expand:
            body = page["body"]
            if STATE["oversized"] == page_id:
                body = "<p>" + ("x" * (17 * 1024 * 1024)) + "</p>"
            out["body"] = {"export_view": {"value": body, "representation": "export_view"}}
        return out

    # ------------------------------------------------------------ dispatch
    def do_GET(self):  # noqa: C901 - one flat router reads best for a mock
        parts = urlsplit(self.path)
        path, params = parts.path, parse_qs(parts.query, keep_blank_values=True)
        ctx = self.server.context
        user, failure = self._identify()
        with _LOCK:
            REQUEST_LOG.append((user, "GET", path))
        if STATE["sso_redirect"]:
            return self._send(302, headers={"Location": ctx + "/login.action?os_destination=x"})
        if STATE["html_page"]:
            return self._send(200, b"<!DOCTYPE html><html><body>Log in</body></html>", "text/html;charset=UTF-8")
        if not path.startswith(ctx + "/rest/api/"):
            return self._send(404, b"<html>Not Found</html>", "text/html")
        if STATE["rate_limit_next"] > 0:
            with _LOCK:
                STATE["rate_limit_next"] -= 1
                STATE["rate_limited"] += 1
            return self._error(429, "Rate limit exceeded", {"Retry-After": STATE["retry_after"]})
        for suffix, status in STATE["fail_status"].items():
            if path.endswith(suffix):
                return self._error(status, "Forced failure")
        if failure == "captcha":
            return self._error(401, "CAPTCHA required", {"X-Seraph-LoginReason": "AUTHENTICATION_DENIED",
                                                          "X-Authentication-Denied-Reason": "CAPTCHA_CHALLENGE"})
        if failure == "bad":
            return self._error(401, "Unauthorized", {"X-Seraph-LoginReason": "AUTHENTICATED_FAILED",
                                                     "WWW-Authenticate": 'Basic realm="Protected"'})
        api = path[len(ctx) + len("/rest/api"):]
        expand = set(",".join(params.get("expand", [])).split(",")) - {""}

        if api == "/user/current":
            if user is None:
                return self._json(200, {"type": "anonymous", "displayName": "Anonymous"})
            u = USERS[user]
            return self._json(200, {"type": "known", "username": user, "userKey": u["key"], "displayName": u["display"]})
        if user is None:
            return self._error(401, "Anonymous access is not permitted for this resource")

        if api == "/space":
            start, limit = self._limit(params)
            wanted = set(params.get("spaceKey", []))
            keys = [k for k in sorted(SPACES) if k in USERS[user]["spaces"] and (not wanted or k in wanted)]
            return self._json(200, self._page_response([self._space_json(k) for k in keys], start, limit, ctx + "/rest/api/space"))
        m = re.fullmatch(r"/space/([^/]+)", api)
        if m:
            key = m.group(1)
            if key not in SPACES or key not in USERS[user]["spaces"]:
                return self._error(404, "No space with key : " + key)
            return self._json(200, self._space_json(key))
        m = re.fullmatch(r"/space/([^/]+)/content/(page|blogpost)", api)
        if m:
            key, ctype = m.groups()
            if key not in SPACES or key not in USERS[user]["spaces"]:
                return self._error(404, "No space with key : " + key)
            depth = (params.get("depth") or ["all"])[0]
            ids = [pid for pid in sorted(PAGES) if PAGES[pid]["space"] == key and PAGES[pid]["type"] == ctype
                   and _visible(user, pid) and (depth != "root" or PAGES[pid]["parent"] is None)]
            start, limit = self._limit(params)
            return self._json(200, self._page_response([self._content_json(pid, expand) for pid in ids], start, limit, path))
        if api == "/content":
            space_key = (params.get("spaceKey") or [""])[0]
            title = (params.get("title") or [""])[0]
            ctype = (params.get("type") or ["page"])[0]
            ids = [pid for pid in sorted(PAGES) if _visible(user, pid) and PAGES[pid]["type"] == ctype
                   and (not space_key or PAGES[pid]["space"] == space_key) and (not title or PAGES[pid]["title"] == title)]
            start, limit = self._limit(params)
            return self._json(200, self._page_response([self._content_json(pid, expand) for pid in ids], start, limit, path))
        m = re.fullmatch(r"/content/(\d+)", api)
        if m:
            pid = int(m.group(1))
            if not _visible(user, pid):
                return self._error(404, f"No content found with id : ContentId{{id={pid}}}")
            return self._json(200, self._content_json(pid, expand))
        m = re.fullmatch(r"/content/(\d+)/child/page", api)
        if m:
            parent = int(m.group(1))
            if not _visible(user, parent):
                return self._error(404, "No content found")
            ids = [pid for pid in sorted(PAGES) if PAGES[pid]["parent"] == parent and _visible(user, pid)]
            start, limit = self._limit(params)
            return self._json(200, self._page_response([self._content_json(pid, expand) for pid in ids], start, limit, path))
        if api in ("/content/search", "/search"):
            cql = (params.get("cql") or [""])[0]
            try:
                clauses = _parse_cql(cql)
            except _CqlError as exc:
                return self._error(400, f"Could not parse cql : {exc}")
            if not clauses:
                return self._error(400, "Could not parse cql : empty")
            ids = [pid for pid in sorted(PAGES) if _visible(user, pid) and _cql_matches(user, pid, clauses)]
            start, limit = self._limit(params)
            if api == "/content/search":
                return self._json(200, self._page_response([self._content_json(pid, expand) for pid in ids], start, limit, path))
            results = []
            for pid in ids:
                page = PAGES[pid]
                results.append({
                    "content": {"id": str(pid), "type": page["type"], "status": "current", "title": page["title"],
                                "_links": {"webui": f"/pages/viewpage.action?pageId={pid}"}},
                    "title": page["title"], "excerpt": _excerpt(page, clauses),
                    "url": f"/pages/viewpage.action?pageId={pid}", "entityType": "content",
                    "lastModified": page["when"], "friendlyLastModified": "recently",
                    "resultGlobalContainer": {"title": SPACES[page["space"]]["name"], "displayUrl": f"/display/{page['space']}"},
                })
            payload = self._page_response(results, start, limit, path)
            payload["totalSize"] = len(results)
            payload["cqlQuery"] = cql
            return self._json(200, payload)
        return self._error(404, "null for uri: " + path)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that hangs up (its size cap, a test's deadline) is not a server bug.
        import sys
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)

    def __init__(self, addr, context: str):
        super().__init__(addr, Handler)
        self.context = context


def make_server(port: int = 0, host: str = "127.0.0.1", context: str = DEFAULT_CONTEXT) -> MockServer:
    return MockServer((host, port), context.rstrip("/"))


def main():
    parser = argparse.ArgumentParser(description="Mock Confluence Data Center REST API")
    parser.add_argument("--port", type=int, default=8097)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help='context path, e.g. "/confluence" or ""')
    args = parser.parse_args()
    server = make_server(args.port, args.host, args.context)
    print(f"Mock Confluence DC listening on http://{args.host}:{args.port}{server.context}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
