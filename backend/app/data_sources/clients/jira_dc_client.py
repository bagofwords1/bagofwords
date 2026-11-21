"""Read-only Jira Data Center / Server connector: issues and their history as tables, issues as documents.

Talks to ``{base_url}/rest/api/2/...`` and ``{base_url}/rest/agile/1.0/...``
only, where ``base_url`` carries the site's context path (``https://host/jira``
on a typical on-prem install). Every call is a stateless GET as one identity, so
Jira project permissions and issue security levels decide what is seen. Auth,
URL handling, the response cap, 429 retries and the fixed error texts come from
``_atlassian_dc_http`` (shared with the Confluence Data Center connector).

Query model. JQL only selects issues; it cannot pick columns or aggregate. So a
query is a JSON spec (``{"table": "issues", "jql": "...", "fields": [...],
"limit": N}``) that becomes a paged ``/search`` call, and the agent aggregates
the returned DataFrame. Calls are kept low the same way for every table:

  * one search call fills ``issues`` and every table embedded in an issue
    (links, attachments, sprint membership, versions and components on the
    issue); only the Jira fields the query needs are requested (``fields=``);
  * ``issue_changelog`` adds ``expand=changelog`` to that same search; only an
    issue whose history Jira cut short costs one more call;
  * ``issue_comments`` / ``worklogs`` read the copies embedded in the issue and
    fetch the rest only when ``total`` says Jira cut them off (at most
    ``_MAX_EXTRA_CALLS`` such calls per query, else a fixed "narrow the JQL"
    error instead of partial data);
  * projects, lookups and field metadata are cached for ten minutes per
    identity; versions, components, boards and sprints are one call per
    project or board, only when those tables are queried.

Scope. The optional project list is a hard scope: the model's JQL is checked for
balanced quotes and parentheses and wrapped as ``project in (...) AND (<jql>)``
(a balanced string cannot close the wrapper), and every returned issue's project
is checked again before it becomes a row or a document.

Messages leaving this module are fixed strings: they never carry a request URL,
a provider response body or a credential.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from urllib.parse import quote, urlsplit

import pandas as pd

from app.ai.prompt_formatters import ServiceFormatter, Table, TableColumn
from app.data_sources.clients._atlassian_dc_http import (
    AtlassianDcHttp, AtlassianError, AtlassianHTTPError, flag as _flag, parse_base_url, resolve_auth,
)
from app.data_sources.clients._confluence_html import html_to_text
from app.data_sources.clients._file_source_common import DocumentText, FileTooLargeError, GlobScopeError
from app.data_sources.clients.base import Capability, DataSourceClient, SearchStopped
from app.data_sources.clients.progress import discovery_progress
from app.schemas.data_sources.configs import JIRA_DC_TABLES, JIRA_TOKEN_FORMAT_MESSAGE

logger = logging.getLogger(__name__)

API = "/rest/api/2"
AGILE = "/rest/agile/1.0"
DEFAULT_LIMIT = 1000
DEFAULT_MAX_ROWS = 2000
_SEARCH_PAGE = 1000            # asked per call; Jira clamps it silently (default cap 1000 on DC)
_RESULT_WINDOW = 10_000        # DC refuses startAt past its result window
_MAX_EXTRA_CALLS = 100         # per query: per-issue follow-ups for cut-off comments/worklogs/changelog
_MAX_LIST_CALLS = 60           # per query: versions/components/boards/sprints fan-out
_MAX_JQL_CHARS = 4000
_MAX_QUERY_CHARS = 200
_SNIPPET_CHARS = 300
_CACHE_TTL_S = 600
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,254}-[0-9]{1,12}$")
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}$")
_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
_JQL_NOISE_RE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/<>=;,%]')
_SPRINT_FIELD_RE = re.compile(r"\[(.*)\]\s*$")
_SLASH_IN_NAME = "∕"

ISSUE_CHILD_TABLES = ("issue_changelog", "issue_comments", "worklogs", "issue_links", "attachments")
LOOKUP_TABLES = {"statuses": "/status", "priorities": "/priority", "resolutions": "/resolution",
                 "issue_types": "/issuetype"}

# Static columns per table: (name, dtype). Custom fields are appended to ``issues`` at discovery.
TABLE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "issues": [
        ("key", "str"), ("id", "str"), ("project", "str"), ("summary", "str"), ("issue_type", "str"),
        ("status", "str"), ("status_category", "str"), ("priority", "str"), ("resolution", "str"),
        ("assignee", "str"), ("assignee_username", "str"), ("reporter", "str"), ("created", "datetime"),
        ("updated", "datetime"), ("resolved", "datetime"), ("due_date", "date"), ("labels", "str"),
        ("components", "str"), ("fix_versions", "str"), ("sprint", "str"), ("sprint_ids", "str"),
        ("epic_link", "str"), ("parent", "str"), ("web_url", "str"),
    ],
    "issue_changelog": [
        ("issue_key", "str"), ("history_id", "str"), ("created", "datetime"), ("author", "str"),
        ("field", "str"), ("from_value", "str"), ("to_value", "str"),
    ],
    "issue_comments": [
        ("issue_key", "str"), ("comment_id", "str"), ("author", "str"), ("created", "datetime"),
        ("updated", "datetime"), ("body", "str"),
    ],
    "worklogs": [
        ("issue_key", "str"), ("worklog_id", "str"), ("author", "str"), ("started", "datetime"),
        ("time_spent_seconds", "int"), ("hours", "float"), ("comment", "str"),
    ],
    "issue_links": [
        ("issue_key", "str"), ("link_type", "str"), ("direction", "str"), ("relation", "str"),
        ("linked_issue_key", "str"), ("linked_summary", "str"), ("linked_status", "str"),
    ],
    "attachments": [
        ("issue_key", "str"), ("attachment_id", "str"), ("filename", "str"), ("size", "int"),
        ("mime_type", "str"), ("created", "datetime"), ("author", "str"),
    ],
    "projects": [("key", "str"), ("id", "str"), ("name", "str"), ("lead", "str"), ("project_type", "str"),
                 ("category", "str")],
    "versions": [("project", "str"), ("id", "str"), ("name", "str"), ("released", "bool"),
                 ("release_date", "date"), ("archived", "bool"), ("description", "str")],
    "components": [("project", "str"), ("id", "str"), ("name", "str"), ("lead", "str"), ("description", "str")],
    "boards": [("id", "int"), ("name", "str"), ("type", "str"), ("project", "str")],
    "sprints": [("board_id", "int"), ("id", "int"), ("name", "str"), ("state", "str"),
                ("start_date", "datetime"), ("end_date", "datetime"), ("complete_date", "datetime"), ("goal", "str")],
    "statuses": [("id", "str"), ("name", "str"), ("category", "str")],
    "priorities": [("id", "str"), ("name", "str")],
    "resolutions": [("id", "str"), ("name", "str")],
    "issue_types": [("id", "str"), ("name", "str"), ("subtask", "bool")],
    "fields": [("id", "str"), ("name", "str"), ("custom", "bool"), ("column", "str")],
}
TABLE_KEYS = {"issues": "key", "projects": "key", "boards": "id", "sprints": "id", "statuses": "id",
              "priorities": "id", "resolutions": "id", "issue_types": "id", "fields": "id",
              "versions": "id", "components": "id"}

# issues column -> the Jira field id it needs (None: always present on an issue).
_ISSUE_FIELD_OF = {
    "key": None, "id": None, "web_url": None, "project": "project", "summary": "summary",
    "issue_type": "issuetype", "status": "status", "status_category": "status", "priority": "priority",
    "resolution": "resolution", "assignee": "assignee", "assignee_username": "assignee", "reporter": "reporter",
    "created": "created", "updated": "updated", "resolved": "resolutiondate", "due_date": "duedate",
    "labels": "labels", "components": "components", "fix_versions": "fixVersions", "parent": "parent",
}

# --------------------------------------------------------------------- messages
_URL_MESSAGE = ("Jira URL must be an HTTP(S) URL such as https://jira.example.com or "
                "https://intranet.example.com/jira (include the context path, if any).")
_NO_CREDENTIAL = "Provide a Jira personal access token, or a username and password."
_ANONYMOUS = ("Jira treated the request as anonymous, so the credentials were not used. Check the "
              "username and password or the personal access token.")
_NOT_FOUND = "Not found, or this identity may not view it (Jira hides issues it does not let you see)."
_BAD_PROJECTS = "Projects must be a comma-separated list of project keys such as OPS, HR."
_BAD_SPEC = ('A Jira query must be a JSON object like {"table": "issues", "jql": "project = OPS AND '
             'status != Done", "fields": ["key", "status"], "limit": 500}.')
_UNKNOWN_TABLE = "Unknown or disabled Jira table {table!r}. Available tables: {tables}."
_UNKNOWN_COLUMNS = "Unknown column(s) for {table}: {columns}. Available: {available}."
_BAD_JQL_SHAPE = ("That JQL has an unbalanced quote or parenthesis, so it was not sent. Fix the JQL and try again.")
_JQL_TOO_LONG = "That JQL is too long; keep it under 4000 characters."
_JQL_REJECTED = ("Jira could not run that JQL (HTTP 400). Check field names and values; for example "
                 "status = \"In Progress\", resolved >= -30d, project = OPS. Use the `fields` table for custom field names.")
_TOO_MANY_FOLLOWUPS = ("More than {n} matching issues have more {what} than Jira embeds in a search, so the query "
                       "stopped instead of returning partial data. Narrow the JQL (a project, a date range or "
                       "issue keys) and try again.")
_RESULT_WINDOW_HIT = ("Jira's search window ends at 10000 results, so the query stopped there. Narrow the JQL "
                      "(for example a date range) to read the rest.")
_OUTSIDE_SCOPE = "That issue or project is outside the projects this connection is limited to."
_BAD_ID = "That is not a Jira issue id; use an issue key such as OPS-123 (or issue:OPS-123), or an issue link."
_BAD_FOLDER = "That folder is not valid; pass a project key such as OPS."
_NO_AGILE = "Boards and sprints need Jira Software, which this Jira site does not offer."
_BAD_LIMIT = "The limit must be a positive whole number."
_TOO_LARGE_READ = "Issue exceeds the configured byte limit; increase it explicitly to read this issue."
_DOCS_DISABLED = "Reading issues as documents is turned off for this connection; query the issues table instead."


class JiraError(AtlassianError):
    """A fixed, user-facing Jira connector error."""


class JiraHTTPError(AtlassianHTTPError, JiraError):
    """One failed Jira exchange. ``kind`` is a stable machine word; the text is fixed."""


# --------------------------------------------------------------------- cache (per process, per identity)
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SECRET = os.urandom(32)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _parse_projects(value) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    keys = []
    for item in items:
        key = str(item).strip()
        if not key:
            continue
        if not _PROJECT_KEY_RE.match(key):
            raise JiraError(_BAD_PROJECTS)
        if key.upper() not in keys:
            keys.append(key.upper())
    return tuple(keys)


def _parse_tables(value) -> tuple[str, ...]:
    if value in (None, ""):
        return JIRA_DC_TABLES
    names = [t.strip().lower() for t in re.split(r"[,\s]+", str(value)) if t.strip()]
    return tuple(t for t in JIRA_DC_TABLES if t in names) or JIRA_DC_TABLES


def _jql_string(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split_jql(jql: str) -> tuple[str, str]:
    """``(where, order_by)`` after checking quotes and parentheses are balanced (outside quotes)."""
    if len(jql) > _MAX_JQL_CHARS:
        raise JiraError(_JQL_TOO_LONG)
    depth, quote_char, escaped, order_at = 0, None, False, None
    for i, ch in enumerate(jql):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote_char:
            if ch == quote_char:
                quote_char = None
            continue
        if ch in "\"'":
            quote_char = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise JiraError(_BAD_JQL_SHAPE)
        elif depth == 0 and order_at is None and ch in "oO":
            m = _ORDER_BY_RE.match(jql, i)
            if m and (i == 0 or not (jql[i - 1].isalnum() or jql[i - 1] == "_")):
                order_at = i
    if depth != 0 or quote_char or escaped:
        raise JiraError(_BAD_JQL_SHAPE)
    if order_at is None:
        return jql.strip(), ""
    where, order = jql[:order_at].strip(), _ORDER_BY_RE.sub("", jql[order_at:], count=1).strip()
    return where, order


def _clean_words(query) -> str:
    text = _JQL_NOISE_RE.sub(" ", str(query or ""))
    text = " ".join(w for w in text.split() if w.upper() not in ("AND", "OR", "NOT", "EMPTY", "NULL", "ORDER", "BY"))
    return text[:_MAX_QUERY_CHARS].strip()


def _snake(name: str) -> str:
    out = re.sub(r"[^0-9a-zA-Z]+", "_", str(name or "")).strip("_").lower()
    return out if out and not out[0].isdigit() else f"f_{out}"


def _name(user) -> str | None:
    if isinstance(user, dict):
        return user.get("displayName") or user.get("name")
    return None


def _named(value) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("value") or value.get("displayName") or value.get("key")
    if value is None:
        return None
    return str(value)


def _join(values) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    parts = [p for p in (_named(v) for v in values) if p]
    return ", ".join(parts) or None


def parse_sprints(value) -> list[dict]:
    """Sprints from the Sprint custom field: DC returns strings like ``...Sprint@1a[id=41,state=CLOSED,name=X,...]``."""
    sprints = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            sprints.append({"id": item.get("id"), "name": item.get("name"), "state": item.get("state")})
            continue
        m = _SPRINT_FIELD_RE.search(str(item))
        if not m:
            continue
        attrs = {}
        for pair in re.split(r",(?=[A-Za-z]+=)", m.group(1)):
            k, _, v = pair.partition("=")
            attrs[k.strip()] = None if v in ("<null>", "") else v
        sprints.append({"id": attrs.get("id"), "name": attrs.get("name"), "state": (attrs.get("state") or "").lower()})
    return sprints


def planner_scope_note(config: dict) -> str:
    cfg = config or {}
    try:
        projects = _parse_projects(cfg.get("projects"))
    except JiraError:
        projects = ()
    scope = (f"limited to the projects {', '.join(projects)} (anything else is refused)" if projects
             else "every project this identity can see")
    return f"Jira Data Center, {scope}; live, as the connection's Jira identity, so issue security applies."


class JiraDcClient(AtlassianDcHttp, DataSourceClient):
    # Hybrid: tabular queries plus issues as documents through the file tools.
    capabilities = {Capability.QUERY, Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}
    cheap_live_listing = True        # issue listings are one live search, never a catalog
    product = "Jira"
    api_root = ""
    log_name = "jira_dc"
    http_error_class = JiraHTTPError
    not_found_message = _NOT_FOUND
    sql_literal_style = "standard"

    def __init__(self, base_url: str, projects: str | None = None, tables: str | None = None,
                 max_rows: int = DEFAULT_MAX_ROWS, include_issue_documents: bool = True, allow_http: bool = False,
                 username: str | None = None, password: str | None = None,
                 personal_access_token: str | None = None, auth_type: str | None = None, **kwargs):
        self.host, self.context_path, self._base = parse_base_url(
            base_url, allow_http, error_class=JiraError, url_message=_URL_MESSAGE,
            api_suffixes=("/rest/api/2", "/rest/api/latest", "/rest/api"))
        self.projects = _parse_projects(projects)
        self._project_set = set(self.projects)
        self.tables = _parse_tables(tables)
        try:
            self.max_rows = max(1, min(10_000, int(max_rows or DEFAULT_MAX_ROWS)))
        except (TypeError, ValueError):
            self.max_rows = DEFAULT_MAX_ROWS
        self.include_issue_documents = _flag(include_issue_documents) if include_issue_documents is not None else True
        if not self.include_issue_documents:
            self.capabilities = {Capability.QUERY}
        self.auth_method, self._auth_header, self._username = resolve_auth(
            username, password, personal_access_token, auth_type, error_class=JiraError,
            no_credential=_NO_CREDENTIAL, token_format_message=JIRA_TOKEN_FORMAT_MESSAGE)
        self._identity_key = hmac.new(_CACHE_SECRET, (self._base + "\0" + self._auth_header).encode("utf-8",
                                      "surrogatepass"), hashlib.sha256).hexdigest()
        self.calls = 0                # API calls made by this client (for tests and diagnostics)

    # ------------------------------------------------------------------ http helpers
    def _get(self, op, path, params=None, deadline=None):
        self.calls += 1
        return super()._get(op, path, params, deadline)

    def _cached(self, op: str, path: str, params=None):
        key = (self._identity_key, path, json.dumps(params or {}, sort_keys=True))
        now = time.monotonic()
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = self._get(op, path, params)
        with _CACHE_LOCK:
            if len(_CACHE) > 512:
                _CACHE.clear()
            _CACHE[key] = (now + _CACHE_TTL_S, value)
        return value

    # ------------------------------------------------------------------ scope
    def _in_scope(self, project_key) -> bool:
        return not self._project_set or (isinstance(project_key, str) and project_key.upper() in self._project_set)

    def scoped_jql(self, jql) -> str:
        """The JQL actually sent: the model's filter inside the project scope, with a stable order."""
        where, order = _split_jql(str(jql or "").strip())
        clauses = []
        if self.projects:
            clauses.append("project in (" + ", ".join(_jql_string(k) for k in self.projects) + ")")
        if where:
            clauses.append(f"({where})" if self.projects else where)
        return " AND ".join(clauses) + " ORDER BY " + (order or "key ASC")

    def _web_url(self, key) -> str:
        return f"{self._base}/browse/{quote(str(key), safe='-')}"

    # ------------------------------------------------------------------ field metadata
    def _field_meta(self) -> list[dict]:
        fields = self._cached("fields", f"{API}/field")
        return [f for f in fields if isinstance(f, dict) and f.get("id")] if isinstance(fields, list) else []

    def _special_fields(self) -> tuple[str | None, str | None]:
        """Field ids of the Sprint and Epic Link custom fields (they differ per site)."""
        sprint = epic = None
        for f in self._field_meta():
            custom = str((f.get("schema") or {}).get("custom") or "")
            if custom.endswith(":gh-sprint") or (f.get("custom") and f.get("name") == "Sprint"):
                sprint = sprint or f["id"]
            elif custom.endswith(":gh-epic-link") or (f.get("custom") and f.get("name") == "Epic Link"):
                epic = epic or f["id"]
        return sprint, epic

    def _custom_types(self) -> dict[str, str]:
        """issues custom column -> dtype ("float" for number fields, "datetime"/"date", else "str")."""
        types = {}
        by_id = {f["id"]: f for f in self._field_meta()}
        for col, fid in self._custom_columns().items():
            kind = str((by_id.get(fid, {}).get("schema") or {}).get("type") or "")
            types[col] = {"number": "float", "datetime": "datetime", "date": "date"}.get(kind, "str")
        return types

    def _custom_columns(self) -> dict[str, str]:
        """issues column name -> custom field id, for every custom field except Sprint and Epic Link."""
        sprint, epic = self._special_fields()
        taken = {c for c, _ in TABLE_COLUMNS["issues"]}
        out: dict[str, str] = {}
        for f in self._field_meta():
            if not f.get("custom") or f["id"] in (sprint, epic):
                continue
            col = _snake(f.get("name") or f["id"])
            if col in taken or col in out:
                col = _snake(f["id"])
            out[col] = f["id"]
        return out

    # ------------------------------------------------------------------ meta / prompts
    @property
    def description(self):
        return (f"Jira Data Center issue tracker at {self.host}{self.context_path} — {planner_scope_note({'projects': ','.join(self.projects)})}"
                + "\n\n" + self.system_prompt())

    def system_prompt(self):
        scope = (f"Every query is limited to the projects {', '.join(self.projects)}; JQL you send is AND-ed with "
                 "that scope." if self.projects else "Queries cover every project this identity can see.")
        text = f"""
        ## Jira Integration (Data Center, read-only, live)
        Query Jira via `execute_query(query)` where `query` is a JSON string:

        ```json
        {{"table": "issues",
         "jql": "project = OPS AND status != Done AND created >= -90d ORDER BY priority DESC",
         "fields": ["key", "summary", "status", "priority", "assignee", "created"],
         "limit": 500}}
        ```

        - `table` (required): one of {', '.join(self.tables)}.
        - `jql` (issues, issue_changelog, issue_comments, worklogs, issue_links, attachments): selects WHICH
          issues; the child tables return one row per change / comment / worklog / link / attachment of the
          matching issues. JQL examples: `project = OPS`, `status = "In Progress"`, `statusCategory = Done`,
          `assignee = currentUser()`, `resolved >= -30d`, `created >= "2026-01-01"`, `sprint in openSprints()`,
          `issuetype = Bug`, `labels = failover`, `text ~ "timeout"`, `"Story Points" >= 5`, `ORDER BY created DESC`.
        - `project` (projects, versions, components, boards): optional project key filter.
          `board_id` (sprints): optional board id; without it, sprints of every board in scope.
        - `fields` (optional): columns to return; omit for all. `limit` (optional): rows, default {DEFAULT_LIMIT},
          at most {self.max_rows}.
        {scope}

        JQL selects rows but cannot group or count: fetch the rows, then aggregate in pandas (e.g. group
        `issues` by status or assignee). Always filter with JQL (a project and a date range) — especially for
        issue_changelog, issue_comments and worklogs — so a query reads only the issues it needs.
        Dates are timezone-aware datetimes; `labels`, `components`, `fix_versions` are comma-separated strings;
        `sprint` is the latest sprint's name. Cycle time: from issue_changelog rows where field == "status"
        (to_value "In Progress" → to_value "Done"). Velocity: sum story_points of issues resolved per sprint.

        Examples:
        ```python
        df = client.execute_query('{{"table": "issues", "jql": "project = OPS AND resolved >= -30d", "fields": ["key", "assignee", "created", "resolved", "story_points"]}}')
        ch = client.execute_query('{{"table": "issue_changelog", "jql": "project = OPS AND resolved >= -30d"}}')
        sp = client.execute_query('{{"table": "sprints", "board_id": 1}}')
        ```
        """
        if self.include_issue_documents:
            text += (
                "\n        ### Issues as documents\n"
                "        To read ONE issue in full (description, every comment, links, attachments) use the file\n"
                "        tools: read_file(file_id='issue:OPS-123'). search_files(query) is Jira text search over\n"
                "        summaries, descriptions and comments; list_files(folder_id='OPS') lists recent issues of a\n"
                "        project. Cite the issue's web_url.\n"
            )
        return text

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    # ------------------------------------------------------------------ discovery
    def _table(self, name: str, custom: dict | None = None) -> Table:
        columns = [TableColumn(name=c, dtype=t) for c, t in TABLE_COLUMNS[name]]
        if name == "issues" and custom:
            types = self._custom_types()
            columns += [TableColumn(name=c, dtype=types.get(c, "str")) for c in custom]
        pk = TABLE_KEYS.get(name)
        pks = [TableColumn(name=pk, dtype="str")] if pk else []
        return Table(name=name, columns=columns, pks=pks, fks=[])

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        """The table list is fixed; one ``/field`` call adds the site's custom fields to ``issues``."""
        custom = self._custom_columns() if "issues" in self.tables else {}
        return [self._table(name, custom) for name in self.tables]

    def get_schema(self, table_name):
        if table_name not in self.tables:
            return None
        return self._table(table_name, self._custom_columns() if table_name == "issues" else None)

    # ------------------------------------------------------------------ test
    def test_connection(self):
        try:
            me = self._get("whoami", f"{API}/myself")
            if not isinstance(me, dict) or not (me.get("name") or me.get("key")):
                return {"success": False, "message": _ANONYMOUS, "login_refused": "rejected"}
            username = str(me.get("name") or "") or None
            display = " ".join(str(me.get("displayName") or "").split())[:120] or None
            identity = username or display
            named = f"{display} ({username})" if display and username and display != username else (identity or "?")
            if self.projects:
                missing = []
                for key in self.projects[:20]:
                    try:
                        self._get("project", f"{API}/project/{quote(key)}")
                    except JiraHTTPError as exc:
                        if exc.kind not in ("not_found", "forbidden"):
                            raise
                        missing.append(key)
                if missing:
                    return {"success": False, "reachable": True,
                            "message": (f"Connected to Jira as {named}, but these projects are missing or not "
                                        f"visible to this identity: {', '.join(missing)}. Check the Projects setting.")}
            else:
                listed = self._get("projects", f"{API}/project")
                if not listed:
                    return {"success": False, "reachable": True,
                            "message": f"Connected to Jira as {named}, but this identity cannot see any project."}
            return {"success": True,
                    "message": f"Connected to Jira at {self.host}{self.context_path} as {named}.",
                    "details": {"host": self.host, "context_path": self.context_path, "identity": identity,
                                "display_name": display, "auth_method": self.auth_method,
                                "projects": list(self.projects)}}
        except JiraHTTPError as exc:
            logger.info("jira_dc test_connection failed: %s", exc.kind)
            result = {"success": False, "message": str(exc)}
            if exc.kind in ("auth", "captcha"):
                result["login_refused"] = exc.kind if exc.kind == "captcha" else "rejected"
            elif exc.kind == "not_found":
                result["message"] = self._msg("not_api")
            return result
        except JiraError as exc:
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            logger.warning("jira_dc test_connection failed unexpectedly: %s", type(exc).__name__)
            return {"success": False, "message": "Jira connection test failed."}

    # ------------------------------------------------------------------ search engine
    def _search(self, jql: str, fields: list[str], expand: list[str] | None, limit: int, deadline=None,
                cancel_check=None):
        """Yield in-scope issues from a paged search, advancing by the rows Jira actually returned."""
        sent = self.scoped_jql(jql)
        start, served = 0, 0
        field_param = ",".join(dict.fromkeys(["project", *fields]))
        while served < limit:
            if cancel_check is not None and cancel_check():
                raise SearchStopped("cancelled", [])
            if start >= _RESULT_WINDOW:
                raise JiraError(_RESULT_WINDOW_HIT)
            params = {"jql": sent, "startAt": str(start), "maxResults": str(min(_SEARCH_PAGE, limit - served)),
                      "fields": field_param}
            if expand:
                params["expand"] = ",".join(expand)
            try:
                page = self._get("search", f"{API}/search", params, deadline)
            except JiraHTTPError as exc:
                if exc.kind == "bad_request":
                    raise JiraHTTPError(400, "bad_request", _JQL_REJECTED)
                raise
            issues = page.get("issues") if isinstance(page, dict) else None
            if not isinstance(issues, list):
                raise self._error(200, "not_api")
            for issue in issues:
                if not isinstance(issue, dict) or not issue.get("key"):
                    continue
                proj = ((issue.get("fields") or {}).get("project") or {}).get("key")
                if not self._in_scope(proj):
                    continue            # belt and braces: JQL already limits the projects
                served += 1
                yield issue
                if served >= limit:
                    return
            start += len(issues)
            total = page.get("total")
            if not issues or (isinstance(total, int) and start >= total):
                return

    # ------------------------------------------------------------------ query
    def _parse_spec(self, query) -> dict:
        spec = query
        if not isinstance(query, dict):
            try:
                spec = json.loads(str(query))
            except (TypeError, ValueError, RecursionError):
                spec = None
        if not isinstance(spec, dict) or not spec.get("table"):
            raise JiraError(_BAD_SPEC)
        table = str(spec["table"]).strip().lower()
        if table not in self.tables:
            raise JiraError(_UNKNOWN_TABLE.format(table=spec["table"], tables=", ".join(self.tables)))
        spec["table"] = table
        return spec

    def _limit(self, spec) -> int:
        try:
            limit = int(spec.get("limit") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            raise JiraError(_BAD_LIMIT)
        if limit <= 0:
            raise JiraError(_BAD_LIMIT)
        return min(limit, self.max_rows)

    def execute_query(self, query=None, **kwargs):
        """Run one JSON spec and return a DataFrame with the table's columns (see ``system_prompt``)."""
        spec = self._parse_spec(query if query is not None else kwargs)
        table, limit = spec["table"], self._limit(spec)
        if table == "issues" or table in ISSUE_CHILD_TABLES:
            _split_jql(str(spec.get("jql") or ""))      # refuse a malformed JQL before any request
        custom = self._custom_columns() if table == "issues" else {}
        all_columns = [c for c, _ in TABLE_COLUMNS[table]] + list(custom)
        wanted = spec.get("fields")
        if isinstance(wanted, str):
            wanted = [w.strip() for w in wanted.split(",") if w.strip()]
        if wanted:
            unknown = [w for w in wanted if w not in all_columns]
            if unknown:
                raise JiraError(_UNKNOWN_COLUMNS.format(table=table, columns=", ".join(unknown),
                                                        available=", ".join(all_columns)))
        columns = list(wanted) if wanted else all_columns
        if table == "issues":
            rows = self._issue_rows(spec, columns, custom, limit)
        elif table in ISSUE_CHILD_TABLES:
            rows = self._child_rows(table, spec, limit)
        elif table in LOOKUP_TABLES:
            rows = self._lookup_rows(table)
        elif table == "fields":
            rows = self._fields_rows()
        elif table == "projects":
            rows = self._project_rows(spec)
        elif table in ("versions", "components"):
            rows = self._project_child_rows(table, spec)
        elif table == "boards":
            rows = self._board_rows(spec)
        else:
            rows = self._sprint_rows(spec)
        return self._frame(table, rows[:limit], columns)

    def _frame(self, table: str, rows: list[dict], columns: list[str]) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
        types = dict(TABLE_COLUMNS[table])
        if table == "issues":
            types.update(self._custom_types())
        for col in columns:
            kind = types.get(col)
            if kind == "datetime":
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="mixed")
            elif kind == "date":
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            elif kind in ("int", "float") and not df.empty:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    # ---- issues
    def _issue_rows(self, spec, columns, custom, limit) -> list[dict]:
        sprint_id, epic_id = self._special_fields()
        needed = []
        for col in columns:
            if col in custom:
                needed.append(custom[col])
            elif col in ("sprint", "sprint_ids"):
                if sprint_id:
                    needed.append(sprint_id)
            elif col == "epic_link":
                if epic_id:
                    needed.append(epic_id)
            elif _ISSUE_FIELD_OF.get(col):
                needed.append(_ISSUE_FIELD_OF[col])
        rows = []
        for issue in self._search(spec.get("jql"), needed, None, limit):
            f = issue.get("fields") or {}
            sprints = parse_sprints(f.get(sprint_id)) if sprint_id else []
            assignee = f.get("assignee") if isinstance(f.get("assignee"), dict) else None
            status = f.get("status") if isinstance(f.get("status"), dict) else {}
            row = {
                "key": issue.get("key"), "id": str(issue.get("id") or ""),
                "project": (f.get("project") or {}).get("key"), "summary": f.get("summary"),
                "issue_type": _named(f.get("issuetype")), "status": status.get("name"),
                "status_category": (status.get("statusCategory") or {}).get("name"),
                "priority": _named(f.get("priority")), "resolution": _named(f.get("resolution")),
                "assignee": _name(assignee), "assignee_username": (assignee or {}).get("name"),
                "reporter": _name(f.get("reporter")), "created": f.get("created"), "updated": f.get("updated"),
                "resolved": f.get("resolutiondate"), "due_date": f.get("duedate"),
                "labels": ", ".join(f.get("labels") or []) or None, "components": _join(f.get("components")),
                "fix_versions": _join(f.get("fixVersions")),
                "sprint": sprints[-1]["name"] if sprints else None,
                "sprint_ids": ", ".join(str(s["id"]) for s in sprints if s.get("id")) or None,
                "epic_link": _named(f.get(epic_id)) if epic_id else None,
                "parent": (f.get("parent") or {}).get("key") if isinstance(f.get("parent"), dict) else None,
                "web_url": self._web_url(issue.get("key")),
            }
            for col, fid in custom.items():
                value = f.get(fid)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    row[col] = value
                else:
                    row[col] = _join(value) if isinstance(value, list) else _named(value)
            rows.append(row)
        return rows

    # ---- child tables
    def _child_rows(self, table, spec, limit) -> list[dict]:
        field = {"issue_changelog": None, "issue_comments": "comment", "worklogs": "worklog",
                 "issue_links": "issuelinks", "attachments": "attachment"}[table]
        expand = ["changelog"] if table == "issue_changelog" else None
        rows, extra = [], [0]
        for issue in self._search(spec.get("jql"), [field] if field else [], expand, self.max_rows):
            key = issue["key"]
            f = issue.get("fields") or {}
            if table == "issue_changelog":
                histories = self._full(issue, "changelog", "histories", extra, "change history",
                                       lambda: self._get("changelog", f"{API}/issue/{quote(key)}",
                                                         {"expand": "changelog", "fields": "project"}).get("changelog"))
                for h in histories:
                    for item in h.get("items") or []:
                        rows.append({"issue_key": key, "history_id": str(h.get("id") or ""), "created": h.get("created"),
                                     "author": _name(h.get("author")), "field": item.get("field"),
                                     "from_value": item.get("fromString"), "to_value": item.get("toString")})
            elif table == "issue_comments":
                comments = self._full_comments(key, f.get("comment"), extra)
                rows += [{"issue_key": key, "comment_id": str(c.get("id") or ""), "author": _name(c.get("author")),
                          "created": c.get("created"), "updated": c.get("updated"), "body": c.get("body")}
                         for c in comments]
            elif table == "worklogs":
                block = f.get("worklog") if isinstance(f.get("worklog"), dict) else {}
                logs = block.get("worklogs") or []
                if isinstance(block.get("total"), int) and block["total"] > len(logs):
                    self._take_extra(extra, "worklogs")
                    logs = self._get("worklog", f"{API}/issue/{quote(key)}/worklog").get("worklogs") or []
                for w in logs:
                    secs = w.get("timeSpentSeconds")
                    rows.append({"issue_key": key, "worklog_id": str(w.get("id") or ""), "author": _name(w.get("author")),
                                 "started": w.get("started"), "time_spent_seconds": secs,
                                 "hours": round(secs / 3600, 2) if isinstance(secs, (int, float)) else None,
                                 "comment": w.get("comment")})
            elif table == "issue_links":
                for link in f.get("issuelinks") or []:
                    t = link.get("type") or {}
                    outward = link.get("outwardIssue")
                    other = outward or link.get("inwardIssue") or {}
                    rows.append({"issue_key": key, "link_type": t.get("name"),
                                 "direction": "outward" if outward else "inward",
                                 "relation": t.get("outward") if outward else t.get("inward"),
                                 "linked_issue_key": other.get("key"),
                                 "linked_summary": (other.get("fields") or {}).get("summary"),
                                 "linked_status": ((other.get("fields") or {}).get("status") or {}).get("name")})
            else:
                for a in f.get("attachment") or []:
                    rows.append({"issue_key": key, "attachment_id": str(a.get("id") or ""), "filename": a.get("filename"),
                                 "size": a.get("size"), "mime_type": a.get("mimeType"), "created": a.get("created"),
                                 "author": _name(a.get("author"))})
            if len(rows) >= limit:
                break
        return rows

    def _take_extra(self, extra: list, what: str) -> None:
        if extra[0] >= _MAX_EXTRA_CALLS:
            raise JiraError(_TOO_MANY_FOLLOWUPS.format(n=_MAX_EXTRA_CALLS, what=what))
        extra[0] += 1

    def _full(self, issue, block_name, list_name, extra, what, fetch) -> list:
        block = issue.get(block_name) if isinstance(issue.get(block_name), dict) else {}
        items = block.get(list_name) or []
        if isinstance(block.get("total"), int) and block["total"] > len(items):
            self._take_extra(extra, what)
            fuller = fetch() or {}
            items = fuller.get(list_name) or items
        return items

    def _full_comments(self, key, block, extra) -> list:
        block = block if isinstance(block, dict) else {}
        comments = list(block.get("comments") or [])
        total = block.get("total")
        if not (isinstance(total, int) and total > len(comments)):
            return comments
        self._take_extra(extra, "comments")
        comments, start = [], 0
        while True:
            page = self._get("comments", f"{API}/issue/{quote(key)}/comment", {"startAt": str(start), "maxResults": "100"})
            batch = page.get("comments") or []
            comments += batch
            start += len(batch)
            if not batch or start >= (page.get("total") or 0):
                return comments
            self._take_extra(extra, "comments")

    # ---- reference tables
    def _lookup_rows(self, table) -> list[dict]:
        data = self._cached(table, f"{API}{LOOKUP_TABLES[table]}") or []
        rows = []
        for item in data if isinstance(data, list) else []:
            row = {"id": str(item.get("id") or ""), "name": item.get("name")}
            if table == "statuses":
                row["category"] = (item.get("statusCategory") or {}).get("name")
            if table == "issue_types":
                row["subtask"] = bool(item.get("subtask"))
            rows.append(row)
        return rows

    def _fields_rows(self) -> list[dict]:
        custom_by_id = {fid: col for col, fid in self._custom_columns().items()}
        sprint, epic = self._special_fields()
        named = {sprint: "sprint", epic: "epic_link"}
        return [{"id": f["id"], "name": f.get("name"), "custom": bool(f.get("custom")),
                 "column": custom_by_id.get(f["id"]) or named.get(f["id"])} for f in self._field_meta()]

    def _visible_projects(self) -> list[dict]:
        data = self._cached("projects", f"{API}/project") or []
        return [p for p in data if isinstance(p, dict) and self._in_scope(p.get("key"))]

    def _target_projects(self, spec) -> list[str]:
        want = spec.get("project")
        if want:
            key = str(want).strip().upper()
            if not self._in_scope(key):
                raise GlobScopeError(_OUTSIDE_SCOPE)
            return [key]
        return [p["key"] for p in self._visible_projects()]

    def _project_rows(self, spec) -> list[dict]:
        want = str(spec.get("project") or "").strip().upper()
        return [{"key": p.get("key"), "id": str(p.get("id") or ""), "name": p.get("name"),
                 "lead": _name(p.get("lead")), "project_type": p.get("projectTypeKey"),
                 "category": (p.get("projectCategory") or {}).get("name")}
                for p in self._visible_projects() if not want or p.get("key") == want]

    def _project_child_rows(self, table, spec) -> list[dict]:
        rows, calls = [], 0
        for key in self._target_projects(spec):
            calls += 1
            if calls > _MAX_LIST_CALLS:
                break
            items = self._get(table, f"{API}/project/{quote(key)}/{table}") or []
            for it in items:
                if table == "versions":
                    rows.append({"project": key, "id": str(it.get("id") or ""), "name": it.get("name"),
                                 "released": bool(it.get("released")), "release_date": it.get("releaseDate"),
                                 "archived": bool(it.get("archived")), "description": it.get("description")})
                else:
                    rows.append({"project": key, "id": str(it.get("id") or ""), "name": it.get("name"),
                                 "lead": _name(it.get("lead")), "description": it.get("description")})
        return rows

    def _boards(self, spec) -> list[dict]:
        boards, start = [], 0
        want = str(spec.get("project") or "").strip().upper()
        if want and not self._in_scope(want):
            raise GlobScopeError(_OUTSIDE_SCOPE)
        for _ in range(_MAX_LIST_CALLS):
            params = {"startAt": str(start), "maxResults": "50"}
            if want:
                params["projectKeyOrId"] = want
            try:
                page = self._get("boards", f"{AGILE}/board", params)
            except JiraHTTPError as exc:
                if exc.kind == "not_found":
                    raise JiraError(_NO_AGILE)
                raise
            values = page.get("values") or []
            for b in values:
                proj = (b.get("location") or {}).get("projectKey")
                if self._project_set and not self._in_scope(proj):
                    continue
                boards.append({"id": b.get("id"), "name": b.get("name"), "type": b.get("type"), "project": proj})
            start += len(values)
            if page.get("isLast", True) or not values:
                break
        return boards

    def _board_rows(self, spec) -> list[dict]:
        return self._boards(spec)

    def _sprint_rows(self, spec) -> list[dict]:
        if spec.get("board_id") not in (None, ""):
            try:
                board_ids = [int(spec["board_id"])]
            except (TypeError, ValueError):
                raise JiraError('board_id must be a number, e.g. {"table": "sprints", "board_id": 1}.')
            allowed = {b["id"] for b in self._boards({})}
            if board_ids[0] not in allowed:
                raise GlobScopeError(_OUTSIDE_SCOPE)
        else:
            board_ids = [b["id"] for b in self._boards(spec) if b.get("type") == "scrum"]
        rows, calls = [], 0
        for bid in board_ids:
            start = 0
            while calls < _MAX_LIST_CALLS:
                calls += 1
                try:
                    page = self._get("sprints", f"{AGILE}/board/{bid}/sprint", {"startAt": str(start), "maxResults": "50"})
                except JiraHTTPError as exc:
                    if exc.kind == "bad_request":       # a kanban board has no sprints
                        break
                    raise
                values = page.get("values") or []
                rows += [{"board_id": bid, "id": s.get("id"), "name": s.get("name"), "state": s.get("state"),
                          "start_date": s.get("startDate"), "end_date": s.get("endDate"),
                          "complete_date": s.get("completeDate"), "goal": s.get("goal")} for s in values]
                start += len(values)
                if page.get("isLast", True) or not values:
                    break
        return rows

    # ------------------------------------------------------------------ issues as documents
    def _require_docs(self):
        if not self.include_issue_documents:
            raise JiraError(_DOCS_DISABLED)

    def _entry(self, issue: dict, snippet: str | None = None) -> dict:
        f = issue.get("fields") or {}
        key = issue["key"]
        summary = " ".join(str(f.get("summary") or "").split())[:200]
        name = f"{key} {summary}".strip().replace("/", _SLASH_IN_NAME)
        status = (f.get("status") or {}).get("name")
        return {"id": f"issue:{key}", "name": name, "path": f"{(f.get('project') or {}).get('key')}/{name}",
                "size": 0, "modified_at": f.get("updated"), "mime_type": "text/plain", "is_folder": False,
                "kind": "issue", "status": status, "snippet": snippet, "web_url": self._web_url(key)}

    def search_files(self, query, limit=50, deadline=None, cancel_check=None, **_):
        """Jira text search (summary, description, comments) inside the scope, newest first."""
        self._require_docs()
        words = _clean_words(query)
        try:
            cap = min(int(50 if limit is None else limit), self.max_rows)
        except (TypeError, ValueError):
            raise JiraError(_BAD_LIMIT)
        if not words or cap <= 0:
            return []
        files = []
        jql = f"text ~ {_jql_string(words)} ORDER BY updated DESC"
        try:
            for issue in self._search(jql, ["summary", "status", "issuetype", "assignee", "updated"], None, cap + 1,
                                      deadline, cancel_check):
                if len(files) >= cap:
                    raise SearchStopped("max_results", files)
                f = issue.get("fields") or {}
                snippet = " · ".join(p for p in (_named(f.get("issuetype")), (f.get("status") or {}).get("name"),
                                                 _name(f.get("assignee"))) if p)[:_SNIPPET_CHARS]
                files.append(self._entry(issue, snippet or None))
        except SearchStopped as stop:
            if stop.stop_reason == "cancelled" and not stop.files:
                raise SearchStopped("cancelled", files)
            raise
        except JiraHTTPError as exc:
            if exc.kind == "deadline":
                raise SearchStopped("deadline", files)
            raise
        return files

    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None, **_):
        """Recent issues (newest update first): every project in scope, or one project key as folder_id."""
        self._require_docs()
        folder = str(folder_id or "").strip().strip("/")
        try:
            cap = min(int(100 if limit is None else limit), self.max_rows)
        except (TypeError, ValueError):
            raise JiraError(_BAD_LIMIT)
        if cap <= 0:
            raise JiraError(_BAD_LIMIT)
        jql = "ORDER BY updated DESC"
        if folder:
            key = folder.split("/")[0]
            if not _PROJECT_KEY_RE.match(key):
                raise JiraError(_BAD_FOLDER)
            if not self._in_scope(key):
                raise GlobScopeError(_OUTSIDE_SCOPE)
            jql = f"project = {_jql_string(key.upper())} ORDER BY updated DESC"
        return [self._entry(i) for i in self._search(jql, ["summary", "status", "updated"], None, cap)]

    def get_schema_text(self):
        return self.prompt_schema()

    def _parse_issue_ref(self, file_id) -> str:
        text = str(file_id or "").strip()
        if text.lower().startswith("issue:"):
            text = text[6:].strip()
        if _ISSUE_KEY_RE.match(text.upper()):
            return text.upper()
        try:
            parsed = urlsplit(text)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme in ("http", "https") and parsed.netloc.lower() == self.host:
            tail = parsed.path.rstrip("/").rsplit("/", 1)[-1].upper()
            if "/browse/" in parsed.path and _ISSUE_KEY_RE.match(tail):
                return tail
        raise JiraError(_BAD_ID)

    def read_file(self, file_id, sheet=None, max_bytes=None, page_range=None, **_):
        """One issue as markdown-like text: header fields, description, every comment, links and attachments."""
        self._require_docs()
        key = self._parse_issue_ref(file_id)
        issue = self._get("read", f"{API}/issue/{quote(key)}", {"expand": "renderedFields"})
        if not isinstance(issue, dict) or not issue.get("key"):
            raise self._error(200, "not_api")
        f = issue.get("fields") or {}
        if not self._in_scope((f.get("project") or {}).get("key")):
            raise GlobScopeError(_OUTSIDE_SCOPE)
        key = issue["key"]
        sprint_id, epic_id = self._special_fields()
        sprints = parse_sprints(f.get(sprint_id)) if sprint_id else []
        lines = [f"# {key}: {' '.join(str(f.get('summary') or '').split())}", ""]
        facts = [
            ("Project", (f.get("project") or {}).get("name")), ("Type", _named(f.get("issuetype"))),
            ("Status", (f.get("status") or {}).get("name")), ("Priority", _named(f.get("priority"))),
            ("Resolution", _named(f.get("resolution"))), ("Assignee", _name(f.get("assignee"))),
            ("Reporter", _name(f.get("reporter"))), ("Created", f.get("created")), ("Updated", f.get("updated")),
            ("Resolved", f.get("resolutiondate")), ("Labels", ", ".join(f.get("labels") or []) or None),
            ("Components", _join(f.get("components"))), ("Fix versions", _join(f.get("fixVersions"))),
            ("Sprint", sprints[-1]["name"] if sprints else None),
            ("Epic", _named(f.get(epic_id)) if epic_id else None), ("URL", self._web_url(key)),
        ]
        lines += [f"{label}: {value}" for label, value in facts if value]
        rendered = issue.get("renderedFields") or {}
        description = html_to_text(rendered.get("description")) or (f.get("description") or "").strip()
        lines += ["", "## Description", "", description or "(no description)"]
        comments = self._full_comments(key, f.get("comment"), [0])
        if comments:
            lines += ["", f"## Comments ({len(comments)})"]
            for c in comments:
                body = c.get("renderedBody") or c.get("body") or ""
                body = html_to_text(body) if "<" in body else body.strip()
                lines += ["", f"**{_name(c.get('author')) or 'unknown'}** — {c.get('created')}", body]
        links = f.get("issuelinks") or []
        if links:
            lines += ["", "## Links"]
            for link in links:
                t = link.get("type") or {}
                other = link.get("outwardIssue") or link.get("inwardIssue") or {}
                rel = t.get("outward") if link.get("outwardIssue") else t.get("inward")
                status = ((other.get("fields") or {}).get("status") or {}).get("name")
                lines.append(f"- {rel} {other.get('key')}: {(other.get('fields') or {}).get('summary')} ({status})")
        attachments = f.get("attachment") or []
        if attachments:
            lines += ["", "## Attachments"]
            lines += [f"- {a.get('filename')} ({a.get('mimeType')}, {a.get('size')} bytes)" for a in attachments]
        return DocumentText("\n".join(lines).strip() + "\n", name=f"{key}.txt", mime="text/plain")

    def read_raw_bytes(self, file_id, *, max_bytes=None):
        text = self.read_file(file_id)
        data = text.encode("utf-8")
        if max_bytes and len(data) > max_bytes:
            raise FileTooLargeError(_TOO_LARGE_READ)
        return data, text.name, "text/plain"
