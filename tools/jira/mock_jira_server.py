#!/usr/bin/env python3
"""Mock Jira Data Center REST API: the test boundary for the jira_dc connector.

Simulates the surface `JiraDcClient` touches, with shapes taken from the Jira
Data Center REST API (v2) and the Jira Software agile API (1.0), under a
context path (default "/jira", as on a typical on-prem install):

  GET {CTX}/rest/api/2/myself                       the identity behind the credentials
  GET {CTX}/rest/api/2/field                        system + custom fields (Story Points, Sprint, Epic Link)
  GET {CTX}/rest/api/2/{status,priority,resolution,issuetype}
  GET {CTX}/rest/api/2/project[/{key}[/versions|/components]]
  GET {CTX}/rest/api/2/search                       ?jql&startAt&maxResults&fields&expand
  GET {CTX}/rest/api/2/issue/{key}                  ?fields&expand (404 when hidden)
  GET {CTX}/rest/api/2/issue/{key}/comment          ?startAt&maxResults
  GET {CTX}/rest/api/2/issue/{key}/worklog
  GET {CTX}/rest/agile/1.0/board                    ?startAt&maxResults&projectKeyOrId
  GET {CTX}/rest/agile/1.0/board/{id}/sprint        ?startAt&maxResults

Faithfulness rules enforced on purpose:
  - Auth is per request (PAT Bearer or Basic). A bad credential is a 401;
    STATE["captcha_users"] gets the CAPTCHA 401 (X-Authentication-Denied-Reason).
  - Each user sees only the projects in their `projects` set and the issues
    whose `security` (if any) names them. A hidden issue is a 404 and is
    missing from search.
  - Search clamps maxResults to STATE["max_results_cap"] without saying so
    (the response's maxResults shows the clamp), as Jira does.
  - Embedded collections are cut off at 20 (fields.comment / fields.worklog
    carry `total`), and an embedded changelog at STATE["changelog_cap"]
    histories, so a client must fetch the rest.
  - A JQL parse error is a 400 with `errorMessages`, as Jira answers.
  - Dates are relative to a fixed NOW (2026-11-20T12:00:00Z) so tests are stable.

Test-only switches (STATE, reset()): captcha_users, pats_disabled, sso_redirect,
html_page, rate_limit_next / retry_after, max_results_cap, changelog_cap,
fail_status {path_suffix: status}. REQUEST_LOG records (user, path, query).
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DEFAULT_CONTEXT = "/jira"
NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

USERS = {
    "alice": {"password": "alice-jira-pass-7f3", "display": "Alice Admin", "key": "JIRAUSER10001",
              "email": "alice@example.com", "projects": {"OPS", "HR", "SEC"}},
    "bob": {"password": "bob-jira-pass-2c9", "display": "Bob Builder", "key": "JIRAUSER10002",
            "email": "bob@example.com", "projects": {"OPS", "HR"}},
    "svc": {"password": "svc-jira-pass-91d", "display": "BOW Service", "key": "JIRAUSER10003",
            "email": "svc@example.com", "projects": {"OPS", "HR"}},
    # People who appear on issues but never call the API.
    "dana": {"display": "Dana DBA", "projects": set()},
    "eli": {"display": "Eli SRE", "projects": set()},
    "hana": {"display": "Hana HR", "projects": set()},
}
TOKENS = {
    "TkRBeE5UWTNPRGt3TVRJek9rRnNhV05sVUVGVQ": "alice",
    "TkRBeE5UWTNPRGt3TVRJek9rSnZZbEJCVkhNZQ": "bob",
    "TkRBeE5UWTNPRGt3TVRJek9sTjJZMUJCVkhNZQ": "svc",
}

PROJECTS = {
    "OPS": {"id": "10000", "name": "Operations", "lead": "alice", "projectTypeKey": "software",
            "category": "Engineering"},
    "HR": {"id": "10001", "name": "People Ops", "lead": "hana", "projectTypeKey": "business", "category": "Business"},
    "SEC": {"id": "10002", "name": "Security", "lead": "alice", "projectTypeKey": "software",
            "category": "Engineering"},
}
STATUSES = {
    "To Do": {"id": "10000", "category": "new", "categoryName": "To Do"},
    "In Progress": {"id": "3", "category": "indeterminate", "categoryName": "In Progress"},
    "In Review": {"id": "10101", "category": "indeterminate", "categoryName": "In Progress"},
    "Done": {"id": "10001", "category": "done", "categoryName": "Done"},
}
PRIORITIES = {"Highest": "1", "High": "2", "Medium": "3", "Low": "4"}
RESOLUTIONS = {"Fixed": "10000", "Won't Fix": "10001", "Duplicate": "10002"}
ISSUE_TYPES = {"Bug": "10004", "Story": "10001", "Task": "10002", "Epic": "10000", "Incident": "10100"}
VERSIONS = {
    "OPS": [
        {"id": "10200", "name": "1.0", "released": True, "releaseDate": "2026-09-01", "archived": False},
        {"id": "10201", "name": "1.1", "released": True, "releaseDate": "2026-10-15", "archived": False},
        {"id": "10202", "name": "2.0", "released": False, "releaseDate": "2026-12-15", "archived": False},
    ],
    "HR": [], "SEC": [{"id": "10300", "name": "Audit 2026", "released": False, "archived": False}],
}
COMPONENTS = {
    "OPS": [{"id": "10400", "name": "Database", "lead": "dana"}, {"id": "10401", "name": "Network", "lead": "eli"},
            {"id": "10402", "name": "API", "lead": "bob"}],
    "HR": [{"id": "10500", "name": "Onboarding", "lead": "hana"}], "SEC": [],
}
BOARDS = [
    {"id": 1, "name": "OPS board", "type": "scrum", "project": "OPS"},
    {"id": 2, "name": "HR kanban", "type": "kanban", "project": "HR"},
    {"id": 3, "name": "SEC board", "type": "scrum", "project": "SEC"},
]
SPRINTS = {
    1: [
        {"id": 41, "name": "OPS Sprint 41", "state": "closed", "startDate": "2026-10-06T09:00:00.000Z",
         "endDate": "2026-10-20T09:00:00.000Z", "completeDate": "2026-10-20T10:00:00.000Z", "goal": "Failover drill"},
        {"id": 42, "name": "OPS Sprint 42", "state": "closed", "startDate": "2026-10-20T09:00:00.000Z",
         "endDate": "2026-11-03T09:00:00.000Z", "completeDate": "2026-11-03T11:00:00.000Z", "goal": "API latency"},
        {"id": 43, "name": "OPS Sprint 43", "state": "active", "startDate": "2026-11-03T09:00:00.000Z",
         "endDate": "2026-11-17T09:00:00.000Z", "goal": "Release 2.0 prep"},
    ],
    2: [],
    3: [{"id": 51, "name": "SEC Sprint 1", "state": "active", "startDate": "2026-11-10T09:00:00.000Z",
         "endDate": "2026-11-24T09:00:00.000Z", "goal": "Audit"}],
}
SPRINT_BY_ID = {s["id"]: {**s, "board": b} for b, ss in SPRINTS.items() for s in ss}

CUSTOM_FIELDS = [
    {"id": "customfield_10016", "name": "Story Points", "custom": True, "schema": {"type": "number"}},
    {"id": "customfield_10020", "name": "Sprint", "custom": True,
     "schema": {"type": "array", "items": "string", "custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
    {"id": "customfield_10014", "name": "Epic Link", "custom": True,
     "schema": {"type": "any", "custom": "com.pyxis.greenhopper.jira:gh-epic-link"}},
]
SYSTEM_FIELDS = ["summary", "issuetype", "status", "priority", "assignee", "reporter", "created", "updated",
                 "resolutiondate", "resolution", "labels", "components", "fixVersions", "duedate", "project",
                 "description", "comment", "worklog", "issuelinks", "attachment", "parent", "security"]


def _ts(days_ago: float, hour: int = 9) -> str:
    when = (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _build_issues() -> dict:
    """Deterministic issues. OPS-1 has 25 comments, OPS-2 23 worklogs, OPS-7 is security-restricted."""
    issues = {}
    plan = [
        # key, type, summary, status, priority, assignee, created_days_ago, resolved_days_ago, components, labels,
        # fixVersion, sprints, points, resolution
        ("OPS-1", "Incident", "Orders database failover took 22 minutes", "Done", "Highest", "dana", 40, 33,
         ["Database"], ["failover", "postmortem"], "1.1", [41], 5, "Fixed"),
        ("OPS-2", "Story", "Automate the failover runbook", "Done", "High", "dana", 38, 20,
         ["Database"], ["failover"], "1.1", [41, 42], 8, "Fixed"),
        ("OPS-3", "Bug", "API p99 latency above 800 ms", "Done", "High", "bob", 30, 12,
         ["API"], ["latency"], "1.1", [42], 3, "Fixed"),
        ("OPS-4", "Bug", "Connection pool exhausted under load", "In Progress", "High", "bob", 15, None,
         ["API", "Database"], ["latency"], "2.0", [43], 5, None),
        ("OPS-5", "Task", "Rotate TLS certificates on the edge proxies", "To Do", "Medium", "eli", 9, None,
         ["Network"], ["security"], "2.0", [43], 2, None),
        ("OPS-6", "Bug", "DNS failover drops 1% of requests", "In Review", "Highest", "eli", 7, None,
         ["Network"], ["failover"], "2.0", [43], 3, None),
        ("OPS-7", "Bug", "Leaked staging credentials in logs", "Done", "Highest", "alice", 25, 24,
         ["API"], ["security"], "1.1", [42], 1, "Fixed"),
        ("OPS-8", "Story", "Dashboard for replication lag", "Done", "Medium", "dana", 28, 6,
         ["Database"], [], "1.1", [42], 5, "Fixed"),
        ("OPS-9", "Bug", "Stale cache after deploy", "Done", "Low", "bob", 20, 19,
         ["API"], [], "1.1", [42], 1, "Duplicate"),
        ("OPS-10", "Epic", "Release 2.0 readiness", "In Progress", "High", "alice", 45, None,
         [], [], "2.0", [], None, None),
        ("OPS-11", "Bug", "Backup job silently skips Sundays", "To Do", "Medium", None, 3, None,
         ["Database"], [], "2.0", [], 2, None),
        ("OPS-12", "Task", "Document the on-call escalation path", "Done", "Low", "eli", 12, 2,
         [], ["docs"], "2.0", [43], 1, "Fixed"),
        ("HR-1", "Task", "Onboarding checklist for engineers", "Done", "Medium", "hana", 50, 30,
         ["Onboarding"], [], None, [], None, "Fixed"),
        ("HR-2", "Task", "Update the vacation policy page", "In Progress", "Low", "hana", 10, None,
         [], ["policy"], None, [], None, None),
        ("SEC-1", "Bug", "Pen test finding: open admin port", "In Progress", "Highest", "alice", 6, None,
         [], ["pentest"], "Audit 2026", [51], 3, None),
    ]
    for (key, itype, summary, status, prio, assignee, created, resolved, comps, labels, fixv, sprints, points,
         resolution) in plan:
        project = key.split("-")[0]
        num = int(key.split("-")[1])
        iid = str({"OPS": 20000, "HR": 21000, "SEC": 22000}[project] + num)
        histories = []
        hid = int(iid) * 10
        if status != "To Do":
            histories.append({"id": str(hid + 1), "author": assignee or "alice", "created": _ts(created - 1, 10),
                              "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]})
        if status in ("In Review",):
            histories.append({"id": str(hid + 2), "author": assignee or "alice", "created": _ts(max(created - 3, 1), 11),
                              "items": [{"field": "status", "fromString": "In Progress", "toString": "In Review"}]})
        if status == "Done":
            histories.append({"id": str(hid + 3), "author": "bob" if key != "OPS-7" else "alice",
                              "created": _ts(max((created + resolved) / 2, resolved), 12),
                              "items": [{"field": "assignee", "fromString": None, "toString": USERS[assignee or "alice"]["display"]}]})
            histories.append({"id": str(hid + 4), "author": assignee or "alice", "created": _ts(resolved, 15),
                              "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"},
                                        {"field": "resolution", "fromString": None, "toString": resolution}]})
        comments = [{"id": str(int(iid) * 100 + i), "author": "bob" if i % 2 else "dana",
                     "body": f"Update {i + 1} on {key}: checked the *replication lag*.",
                     "created": _ts(created - 1 - i * 0.2, 13)} for i in range(25 if key == "OPS-1" else 2)]
        worklogs = [{"id": str(int(iid) * 1000 + i), "author": assignee or "alice",
                     "started": _ts(created - 1 - i * 0.5, 9), "timeSpentSeconds": 3600 * (1 + i % 3),
                     "comment": f"Work session {i + 1}"} for i in range(23 if key == "OPS-2" else (2 if assignee else 0))]
        issues[key] = {
            "id": iid, "key": key, "project": project, "issuetype": itype, "summary": summary, "status": status,
            "priority": prio, "assignee": assignee, "reporter": "alice" if project != "HR" else "hana",
            "created": _ts(created), "updated": _ts(resolved if resolved is not None else max(created - 2, 0.5), 16),
            "resolutiondate": _ts(resolved, 15) if resolved is not None else None, "resolution": resolution,
            "labels": labels, "components": comps, "fixVersions": [fixv] if fixv else [], "sprints": sprints,
            "points": points, "epic": "OPS-10" if project == "OPS" and key not in ("OPS-10",) and num >= 4 else None,
            "description": f"h2. Context\nThe *{summary.lower()}* issue.\n||Metric||Value||\n|Impact|{prio}|",
            "description_html": (f"<h2>Context</h2><p>The <b>{summary.lower()}</b> issue.</p>"
                                 f"<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Impact</td><td>{prio}</td></tr></table>"),
            "comments": comments, "worklogs": worklogs, "histories": histories,
            "links": [], "attachments": [],
            "security": {"alice", "svc"} if key == "OPS-7" else None,
        }
    issues["OPS-4"]["links"] = [{"type": "Blocks", "direction": "outward", "key": "OPS-10"}]
    issues["OPS-6"]["links"] = [{"type": "Relates", "direction": "outward", "key": "OPS-1"},
                                {"type": "Duplicate", "direction": "inward", "key": "OPS-11"}]
    issues["OPS-1"]["attachments"] = [{"id": "30001", "filename": "failover-timeline.png", "size": 48213,
                                       "mimeType": "image/png", "created": _ts(33, 14), "author": "dana"}]
    return issues


ISSUES = _build_issues()
STATE: dict = {}
REQUEST_LOG: list = []
_LOCK = threading.Lock()


def reset() -> None:
    with _LOCK:
        STATE.clear()
        STATE.update({"captcha_users": set(), "pats_disabled": False, "sso_redirect": False, "html_page": False,
                      "rate_limit_next": 0, "retry_after": "0", "rate_limited": 0, "max_results_cap": 100,
                      "changelog_cap": 100, "fail_status": {}})
        REQUEST_LOG.clear()


reset()


# ---------------------------------------------------------------------------------------------- JQL
class JqlError(ValueError):
    pass


_TOKEN_RE = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')|(?P<op>!=|!~|>=|<=|=|~|>|<|\(|\)|,)'
                       r'|(?P<word>[A-Za-z0-9_.\-+/:\[\]@]+(?:\(\))?))')


def _tokens(text: str) -> list:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise JqlError(f"Error in the JQL Query: The character '{text[pos]}' is a reserved JQL character.")
        if m.group("str") is not None:
            out.append(("str", re.sub(r"\\(.)", r"\1", m.group("str")[1:-1])))
        elif m.group("op") is not None:
            out.append(("op", m.group("op")))
        else:
            out.append(("word", m.group("word")))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


class _Parser:
    def __init__(self, tokens):
        self.t, self.i = tokens, 0

    def peek(self, offset=0):
        j = self.i + offset
        return self.t[j] if j < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def kw(self, *words, offset=0):
        kind, val = self.peek(offset)
        return kind == "word" and val.upper() in words

    def parse(self):
        expr = None if self.kw("ORDER") or self.peek()[0] is None else self.or_expr()
        order = []
        if self.kw("ORDER"):
            self.take()
            if not self.kw("BY"):
                raise JqlError("Error in the JQL Query: Expecting 'BY' after 'ORDER'.")
            self.take()
            while True:
                kind, field = self.take()
                if kind not in ("word", "str"):
                    raise JqlError("Error in the JQL Query: Expecting a field name after 'ORDER BY'.")
                direction = "ASC"
                if self.kw("ASC", "DESC"):
                    direction = self.take()[1].upper()
                order.append((field.lower(), direction))
                if self.peek() == ("op", ","):
                    self.take()
                    continue
                break
        if self.peek()[0] is not None:
            raise JqlError(f"Error in the JQL Query: Unexpected '{self.peek()[1]}'.")
        return expr, order

    def or_expr(self):
        node = self.and_expr()
        while self.kw("OR"):
            self.take()
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.not_expr()
        while self.kw("AND"):
            self.take()
            node = ("and", node, self.not_expr())
        return node

    def not_expr(self):
        if self.kw("NOT"):
            self.take()
            return ("not", self.not_expr())
        if self.peek() == ("op", "("):
            self.take()
            node = self.or_expr()
            if self.take() != ("op", ")"):
                raise JqlError("Error in the JQL Query: Expecting ')'.")
            return node
        return self.clause()

    def value(self):
        kind, val = self.take()
        if kind in ("str", "word"):
            return val
        raise JqlError("Error in the JQL Query: Expecting a value.")

    def clause(self):
        kind, field = self.take()
        if kind not in ("word", "str"):
            raise JqlError("Error in the JQL Query: Expecting a field name.")
        if self.kw("NOT") and self.kw("IN", offset=1):
            self.take(); self.take()
            op = "not in"
        elif self.kw("IN"):
            self.take()
            op = "in"
        elif self.kw("IS"):
            self.take()
            op = "is"
            if self.kw("NOT"):
                self.take()
                op = "is not"
        else:
            k, op = self.take()
            if k != "op" or op in ("(", ")", ","):
                raise JqlError(f"Error in the JQL Query: Expecting an operator after '{field}'.")
        if op in ("in", "not in"):
            kind, val = self.peek()
            if kind == "word" and val.endswith("()"):      # sprint in openSprints()
                self.take()
                return ("clause", field, op, [val])
            if self.take() != ("op", "("):
                raise JqlError("Error in the JQL Query: Expecting '(' after IN.")
            values = [self.value()]
            while self.peek() == ("op", ","):
                self.take()
                values.append(self.value())
            if self.take() != ("op", ")"):
                raise JqlError("Error in the JQL Query: Expecting ')'.")
            return ("clause", field, op, values)
        return ("clause", field, op, self.value())


_FIELD_ALIASES = {"issuekey": "key", "type": "issuetype", "resolved": "resolutiondate", "fixversion": "fixversions",
                  "component": "components", "cf[10016]": "story points", "cf[10020]": "sprint",
                  "statuscategory": "statuscategory", "due": "duedate"}
_KNOWN = {"project", "key", "summary", "text", "status", "statuscategory", "priority", "assignee", "reporter",
          "issuetype", "resolution", "labels", "components", "fixversions", "sprint", "created", "updated",
          "resolutiondate", "story points", "epic link", "duedate", "description", "comment"}


def _date_value(raw: str):
    raw = raw.strip()
    m = re.fullmatch(r"([-+]?)(\d+)([dwhm])", raw)
    if m:
        n = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
        unit = {"d": timedelta(days=1), "w": timedelta(weeks=1), "h": timedelta(hours=1), "m": timedelta(minutes=1)}
        return NOW + n * unit[m.group(3)]
    low = raw.lower()
    if low in ("now()",):
        return NOW
    if low == "startofday()":
        return NOW.replace(hour=0, minute=0, second=0)
    if low == "startofmonth()":
        return NOW.replace(day=1, hour=0, minute=0, second=0)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise JqlError(f"Error in the JQL Query: Date value '{raw}' for field is invalid.")


def _parse_ts(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.000+0000").replace(tzinfo=timezone.utc) if value else None


def _issue_values(issue: dict, field: str, user: str):
    if field == "project":
        return [issue["project"], PROJECTS[issue["project"]]["name"], PROJECTS[issue["project"]]["id"]]
    if field == "key":
        return [issue["key"], issue["id"]]
    if field in ("status", "priority", "issuetype", "resolution"):
        return [issue[field]] if issue[field] else []
    if field == "statuscategory":
        return [STATUSES[issue["status"]]["categoryName"]]
    if field in ("assignee", "reporter"):
        who = issue[field]
        return [who, USERS[who]["display"]] if who else []
    if field == "labels":
        return issue["labels"]
    if field == "components":
        return issue["components"]
    if field == "fixversions":
        return issue["fixVersions"]
    if field == "sprint":
        return [str(s) for s in issue["sprints"]] + [SPRINT_BY_ID[s]["name"] for s in issue["sprints"]]
    if field == "epic link":
        return [issue["epic"]] if issue["epic"] else []
    return []


def _matches(node, issue: dict, user: str) -> bool:
    kind = node[0]
    if kind == "and":
        return _matches(node[1], issue, user) and _matches(node[2], issue, user)
    if kind == "or":
        return _matches(node[1], issue, user) or _matches(node[2], issue, user)
    if kind == "not":
        return not _matches(node[1], issue, user)
    _, raw_field, op, value = node
    field = _FIELD_ALIASES.get(raw_field.lower(), raw_field.lower())
    if field not in _KNOWN:
        raise JqlError(f"Field '{raw_field}' does not exist or you do not have permission to view it.")

    def norm(v):
        v = str(v)
        if v.lower() == "currentuser()":
            return user.lower()
        if v.lower() in ("opensprints()",):
            return "__open__"
        if v.lower() in ("closedsprints()",):
            return "__closed__"
        return v.lower()

    if field in ("text", "summary", "description", "comment"):
        hay = issue["summary"]
        if field in ("text", "description"):
            hay += " " + issue["description"]
        if field in ("text", "comment"):
            hay += " " + " ".join(c["body"] for c in issue["comments"])
        words = [w for w in re.split(r"\W+", str(value).lower()) if w]
        hit = bool(words) and all(w in hay.lower() for w in words)
        if op == "~":
            return hit
        if op == "!~":
            return not hit
        raise JqlError(f"Error in the JQL Query: The operator '{op}' is not supported by the '{raw_field}' field.")
    if field in ("created", "updated", "resolutiondate", "duedate"):
        if op in ("is", "is not"):
            empty = issue.get(field) is None
            return empty if op == "is" else not empty
        actual = _parse_ts(issue.get(field))
        if actual is None:
            return False
        target = _date_value(str(value))
        return {"=": actual == target, "!=": actual != target, ">": actual > target, ">=": actual >= target,
                "<": actual < target, "<=": actual <= target}.get(op, False)
    if field == "story points":
        pts = issue["points"]
        if op in ("is", "is not"):
            return (pts is None) == (op == "is")
        if pts is None:
            return False
        v = float(value)
        return {"=": pts == v, "!=": pts != v, ">": pts > v, ">=": pts >= v, "<": pts < v, "<=": pts <= v}.get(op, False)
    actual = {str(a).lower() for a in _issue_values(issue, field, user)}
    if field == "sprint":
        states = {SPRINT_BY_ID[s]["state"] for s in issue["sprints"]}
        if "active" in states:
            actual.add("__open__")
        if "closed" in states:
            actual.add("__closed__")
    if op in ("is", "is not"):
        if str(value).upper() not in ("EMPTY", "NULL"):
            raise JqlError("Error in the JQL Query: IS needs EMPTY or NULL.")
        return (not actual) == (op == "is")
    if op == "=":
        return norm(value) in actual
    if op == "!=":
        return norm(value) not in actual
    if op == "in":
        return any(norm(v) in actual for v in value)
    if op == "not in":
        return not any(norm(v) in actual for v in value)
    if op == "~":
        return any(norm(value) in a for a in actual)
    raise JqlError(f"Error in the JQL Query: The operator '{op}' is not supported by the '{raw_field}' field.")


_SORT_KEYS = {"created": "created", "updated": "updated", "key": "id", "priority": "priority", "resolved": "resolutiondate",
              "resolutiondate": "resolutiondate", "status": "status", "rank": "id"}


def run_jql(jql: str, user: str) -> list:
    expr, order = _Parser(_tokens(jql or "")).parse()
    visible = [i for i in ISSUES.values() if _visible(user, i)]
    hits = [i for i in visible if expr is None or _matches(expr, i, user)]
    hits.sort(key=lambda i: int(i["id"]))
    for field, direction in reversed(order or [("key", "DESC")]):
        attr = _SORT_KEYS.get(field, "id")
        if attr == "priority":
            keyf = lambda i: int(PRIORITIES[i["priority"]])
        elif attr == "id":
            keyf = lambda i: int(i["id"])
        else:
            keyf = lambda i, a=attr: i.get(a) or ""
        hits.sort(key=keyf, reverse=direction == "DESC")
    return hits


def _visible(user, issue) -> bool:
    if user is None or issue["project"] not in USERS[user]["projects"]:
        return False
    return issue["security"] is None or user in issue["security"]


# ---------------------------------------------------------------------------------------------- JSON shapes
def _user_json(name):
    if not name:
        return None
    u = USERS[name]
    return {"name": name, "key": u.get("key", name), "displayName": u["display"], "active": True,
            "emailAddress": u.get("email")}


def _sprint_string(sid):
    s = SPRINT_BY_ID[sid]
    return (f"com.atlassian.greenhopper.service.sprint.Sprint@{sid:x}[id={sid},rapidViewId={s['board']},"
            f"state={s['state'].upper()},name={s['name']},startDate={s.get('startDate')},endDate={s.get('endDate')},"
            f"completeDate={s.get('completeDate') or '<null>'},sequence={sid},goal={s.get('goal') or ''}]")


def _issue_json(issue: dict, fields_param: str | None, expand: set, ctx: str, host: str) -> dict:
    wanted = None
    if fields_param:
        parts = {p.strip() for p in fields_param.split(",") if p.strip()}
        if not ({"*all", "*navigable"} & parts):
            wanted = parts
    proj = issue["project"]
    full = {
        "summary": issue["summary"],
        "issuetype": {"id": ISSUE_TYPES[issue["issuetype"]], "name": issue["issuetype"], "subtask": False},
        "status": {"id": STATUSES[issue["status"]]["id"], "name": issue["status"],
                   "statusCategory": {"key": STATUSES[issue["status"]]["category"],
                                      "name": STATUSES[issue["status"]]["categoryName"]}},
        "priority": {"id": PRIORITIES[issue["priority"]], "name": issue["priority"]},
        "assignee": _user_json(issue["assignee"]), "reporter": _user_json(issue["reporter"]),
        "created": issue["created"], "updated": issue["updated"], "resolutiondate": issue["resolutiondate"],
        "resolution": ({"id": RESOLUTIONS[issue["resolution"]], "name": issue["resolution"]}
                       if issue["resolution"] else None),
        "labels": issue["labels"],
        "components": [{"id": c["id"], "name": c["name"]} for c in COMPONENTS[proj] if c["name"] in issue["components"]],
        "fixVersions": [{"id": v["id"], "name": v["name"], "released": v["released"]}
                        for v in VERSIONS[proj] if v["name"] in issue["fixVersions"]],
        "duedate": None,
        "project": {"id": PROJECTS[proj]["id"], "key": proj, "name": PROJECTS[proj]["name"]},
        "description": issue["description"],
        "comment": {"startAt": 0, "maxResults": 20, "total": len(issue["comments"]),
                    "comments": [_comment_json(c) for c in issue["comments"][:20]]},
        "worklog": {"startAt": 0, "maxResults": 20, "total": len(issue["worklogs"]),
                    "worklogs": [_worklog_json(w, issue) for w in issue["worklogs"][:20]]},
        "issuelinks": [_link_json(issue, link) for link in issue["links"]],
        "attachment": [{"id": a["id"], "filename": a["filename"], "size": a["size"], "mimeType": a["mimeType"],
                        "created": a["created"], "author": _user_json(a["author"]),
                        "content": f"http://{host}{ctx}/secure/attachment/{a['id']}/{a['filename']}"}
                       for a in issue["attachments"]],
        "customfield_10016": issue["points"],
        "customfield_10020": [_sprint_string(s) for s in issue["sprints"]] or None,
        "customfield_10014": issue["epic"],
        "security": {"id": "10010", "name": "Restricted"} if issue["security"] else None,
        "parent": None,
    }
    fields = {k: v for k, v in full.items() if wanted is None or k in wanted}
    out = {"id": issue["id"], "key": issue["key"], "self": f"http://{host}{ctx}/rest/api/2/issue/{issue['id']}",
           "fields": fields}
    if "renderedFields" in expand:
        rendered = {}
        if wanted is None or "description" in wanted:
            rendered["description"] = issue["description_html"]
        if wanted is None or "comment" in wanted:
            rendered["comment"] = {"comments": [{**_comment_json(c), "body": f"<p>{c['body'].replace('*', '')}</p>"}
                                                for c in issue["comments"][:20]]}
        out["renderedFields"] = rendered
    if "changelog" in expand:
        cap = STATE["changelog_cap"]
        hist = issue["histories"]
        out["changelog"] = {"startAt": 0, "maxResults": cap, "total": len(hist),
                            "histories": [_history_json(h) for h in hist[:cap]]}
    return out


def _comment_json(c):
    return {"id": c["id"], "author": _user_json(c["author"]), "updateAuthor": _user_json(c["author"]),
            "body": c["body"], "created": c["created"], "updated": c["created"]}


def _worklog_json(w, issue):
    return {"id": w["id"], "issueId": issue["id"], "author": _user_json(w["author"]), "started": w["started"],
            "timeSpentSeconds": w["timeSpentSeconds"], "timeSpent": f"{w['timeSpentSeconds'] // 3600}h",
            "comment": w["comment"], "created": w["started"], "updated": w["started"]}


def _history_json(h):
    return {"id": h["id"], "author": _user_json(h["author"]), "created": h["created"],
            "items": [{"field": i["field"], "fieldtype": "jira", "fromString": i["fromString"],
                       "toString": i["toString"]} for i in h["items"]]}


def _link_json(issue, link):
    other = ISSUES[link["key"]]
    names = {"Blocks": ("blocks", "is blocked by"), "Relates": ("relates to", "relates to"),
             "Duplicate": ("duplicates", "is duplicated by")}[link["type"]]
    side = "outwardIssue" if link["direction"] == "outward" else "inwardIssue"
    return {"id": f"{issue['id']}{other['id']}",
            "type": {"name": link["type"], "outward": names[0], "inward": names[1]},
            side: {"id": other["id"], "key": other["key"],
                   "fields": {"summary": other["summary"], "status": {"name": other["status"]}}}}


# ---------------------------------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "MockJira/10.3"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body=b"", ctype="application/json;charset=UTF-8", headers=None):
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
                pass

    def _json(self, status, payload, headers=None):
        self._send(status, json.dumps(payload).encode("utf-8"), headers=headers)

    def _errors(self, status, *messages, headers=None):
        self._json(status, {"errorMessages": list(messages), "errors": {}}, headers)

    def _identify(self):
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
            if user not in USERS or USERS[user].get("password") != password:
                return None, "bad"
            return user, None
        return None, "bad"

    @staticmethod
    def _page(params, default=50, cap=None):
        start = max(0, int((params.get("startAt") or ["0"])[0] or 0))
        size = max(1, int((params.get("maxResults") or [str(default)])[0] or default))
        return start, min(size, cap or size)

    def do_GET(self):  # noqa: C901
        parts = urlsplit(self.path)
        path, params = parts.path, parse_qs(parts.query, keep_blank_values=True)
        ctx, host = self.server.context, self.headers.get("Host", "localhost")
        user, failure = self._identify()
        with _LOCK:
            REQUEST_LOG.append((user, path, parts.query))
        if STATE["sso_redirect"]:
            return self._send(302, headers={"Location": ctx + "/login.jsp?os_destination=x"})
        if STATE["html_page"]:
            return self._send(200, b"<!DOCTYPE html><html><body>Log in</body></html>", "text/html;charset=UTF-8")
        if not (path.startswith(ctx + "/rest/api/2/") or path.startswith(ctx + "/rest/agile/1.0/")):
            return self._send(404, b"<html>Not Found</html>", "text/html")
        if STATE["rate_limit_next"] > 0:
            with _LOCK:
                STATE["rate_limit_next"] -= 1
                STATE["rate_limited"] += 1
            return self._errors(429, "Rate limit exceeded", headers={"Retry-After": STATE["retry_after"]})
        for suffix, status in STATE["fail_status"].items():
            if path.endswith(suffix):
                return self._errors(status, "Forced failure")
        if failure == "captcha":
            return self._errors(401, "CAPTCHA required", headers={
                "X-Seraph-LoginReason": "AUTHENTICATION_DENIED", "X-Authentication-Denied-Reason": "CAPTCHA_CHALLENGE"})
        if failure:
            return self._errors(401, "You are not authenticated. Authentication required to perform this operation.",
                                headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"})
        api = path[len(ctx):]
        projects = USERS[user]["projects"]

        if api == "/rest/api/2/myself":
            return self._json(200, {**_user_json(user), "timeZone": "UTC"})
        if api == "/rest/api/2/field":
            system = [{"id": f, "name": f.capitalize(), "custom": False, "schema": {"type": "string"}}
                      for f in SYSTEM_FIELDS]
            return self._json(200, system + CUSTOM_FIELDS)
        if api == "/rest/api/2/status":
            return self._json(200, [{"id": v["id"], "name": k, "statusCategory": {"key": v["category"],
                                     "name": v["categoryName"]}} for k, v in STATUSES.items()])
        if api == "/rest/api/2/priority":
            return self._json(200, [{"id": v, "name": k} for k, v in PRIORITIES.items()])
        if api == "/rest/api/2/resolution":
            return self._json(200, [{"id": v, "name": k} for k, v in RESOLUTIONS.items()])
        if api == "/rest/api/2/issuetype":
            return self._json(200, [{"id": v, "name": k, "subtask": False} for k, v in ISSUE_TYPES.items()])
        if api == "/rest/api/2/project":
            return self._json(200, [self._project_json(k) for k in sorted(PROJECTS) if k in projects])
        m = re.fullmatch(r"/rest/api/2/project/([A-Za-z0-9_]+)(/versions|/components)?", api)
        if m:
            key = m.group(1).upper()
            if key not in PROJECTS or key not in projects:
                return self._errors(404, f"No project could be found with key '{m.group(1)}'.")
            if m.group(2) == "/versions":
                return self._json(200, [{**v, "projectId": PROJECTS[key]["id"]} for v in VERSIONS[key]])
            if m.group(2) == "/components":
                return self._json(200, [{"id": c["id"], "name": c["name"], "lead": _user_json(c["lead"]),
                                         "project": key} for c in COMPONENTS[key]])
            return self._json(200, self._project_json(key))
        if api == "/rest/api/2/search":
            jql = (params.get("jql") or [""])[0]
            try:
                hits = run_jql(jql, user)
            except JqlError as exc:
                return self._errors(400, str(exc))
            start, size = self._page(params, 50, STATE["max_results_cap"])
            fields = (params.get("fields") or [None])[0]
            expand = set(",".join(params.get("expand", [])).split(",")) - {""}
            page = [_issue_json(i, fields, expand, ctx, host) for i in hits[start:start + size]]
            return self._json(200, {"expand": "names,schema", "startAt": start, "maxResults": size,
                                    "total": len(hits), "issues": page})
        m = re.fullmatch(r"/rest/api/2/issue/([A-Za-z0-9_-]+)(/comment|/worklog)?", api)
        if m:
            ref = m.group(1).upper()
            issue = ISSUES.get(ref) or next((i for i in ISSUES.values() if i["id"] == ref), None)
            if issue is None or not _visible(user, issue):
                return self._errors(404, "Issue Does Not Exist")
            if m.group(2) == "/comment":
                start, size = self._page(params, 50, 50)
                chunk = issue["comments"][start:start + size]
                return self._json(200, {"startAt": start, "maxResults": size, "total": len(issue["comments"]),
                                        "comments": [_comment_json(c) for c in chunk]})
            if m.group(2) == "/worklog":
                return self._json(200, {"startAt": 0, "maxResults": len(issue["worklogs"]),
                                        "total": len(issue["worklogs"]),
                                        "worklogs": [_worklog_json(w, issue) for w in issue["worklogs"]]})
            expand = set(",".join(params.get("expand", [])).split(",")) - {""}
            saved = STATE["changelog_cap"]
            STATE["changelog_cap"] = 10_000          # a single-issue read returns the full history
            try:
                body = _issue_json(issue, (params.get("fields") or [None])[0], expand, ctx, host)
            finally:
                STATE["changelog_cap"] = saved
            return self._json(200, body)
        if api == "/rest/agile/1.0/board":
            want = (params.get("projectKeyOrId") or [None])[0]
            boards = [b for b in BOARDS if b["project"] in projects and (not want or b["project"] == want.upper())]
            start, size = self._page(params, 50, 50)
            chunk = boards[start:start + size]
            return self._json(200, {"maxResults": size, "startAt": start, "isLast": start + size >= len(boards),
                                    "values": [{"id": b["id"], "name": b["name"], "type": b["type"],
                                                "location": {"projectKey": b["project"]}} for b in chunk]})
        m = re.fullmatch(r"/rest/agile/1.0/board/(\d+)/sprint", api)
        if m:
            board = next((b for b in BOARDS if b["id"] == int(m.group(1))), None)
            if board is None or board["project"] not in projects:
                return self._errors(404, "Board does not exist or you do not have permission to see it.")
            sprints = SPRINTS[board["id"]]
            start, size = self._page(params, 50, 50)
            chunk = sprints[start:start + size]
            return self._json(200, {"maxResults": size, "startAt": start, "isLast": start + size >= len(sprints),
                                    "values": [{**s, "originBoardId": board["id"]} for s in chunk]})
        return self._errors(404, "null for uri: " + path)

    @staticmethod
    def _project_json(key):
        p = PROJECTS[key]
        return {"id": p["id"], "key": key, "name": p["name"], "projectTypeKey": p["projectTypeKey"],
                "lead": _user_json(p["lead"]), "projectCategory": {"name": p["category"]}}


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, context):
        super().__init__(addr, Handler)
        self.context = context

    def handle_error(self, request, client_address):
        import sys
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


def make_server(port: int = 0, host: str = "127.0.0.1", context: str = DEFAULT_CONTEXT) -> MockServer:
    return MockServer((host, port), context.rstrip("/"))


def main():
    parser = argparse.ArgumentParser(description="Mock Jira Data Center REST API")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--context", default=DEFAULT_CONTEXT)
    args = parser.parse_args()
    server = make_server(args.port, args.host, args.context)
    print(f"Mock Jira DC listening on http://{args.host}:{args.port}{server.context}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
