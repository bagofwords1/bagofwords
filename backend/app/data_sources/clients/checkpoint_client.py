"""Read-only Check Point Management API connector for firewall policy and gateway investigations.

Contract: Management Web API v1.9.1 (R81.20 JHF) with the same wire shapes verified
against v2 (R82). Every call is ``POST <origin>/web_api[/v<version>]/<command>`` with a
JSON body, ``X-chkp-sid`` after a ``login`` that always requests ``"read-only": true``,
and a guaranteed ``logout``. The frozen catalog (``checkpoint/catalog.json``) is an
allow-list of ``show-*`` commands: the client cannot build a request for anything else,
so ``publish``, ``set-*``, ``add-*``, ``delete-*``, ``install-policy``, ``run-script`` and
``discard`` are unreachable by construction.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_progress


class CheckPointError(ValueError):
    """Stable machine code plus a safe explanation; never echoes bodies or credentials."""

    def __init__(self, code, message, status=None, vendor_code=None):
        self.code, self.status, self.vendor_code = code, status, vendor_code
        super().__init__(f"{code}: {message}")


_SESSION_COMMANDS = frozenset({"login", "logout", "keepalive", "show-session", "show-task"})
_TYPES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "json": "json"}
_META = {
    "_bow_management": "str",
    "_bow_domain": "str",
    "_bow_api_version": "str",
    "_bow_connection": "str",
    "_bow_retrieved_at": "datetime",
    "_bow_query_id": "str",
    "_bow_command": "str",
    "_bow_contract": "str",
}
_BASE_CONTROLS = frozenset({"table", "fields", "filter", "parent", "limit"})
_TIME_FRAMES = frozenset(
    {"last-7-days", "last-hour", "today", "last-24-hours", "yesterday", "this-week", "this-month", "last-30-days", "all-time"}
)
_TASK_STATUS = frozenset({"successful", "failed", "in-progress", "all"})
_OPS = frozenset({"eq", "ne", "gt", "ge", "lt", "le", "like", "in", "contains"})
_SECRET_KEY = re.compile(
    r"(^|[._-])(password|passphrase|secret|shared-secret|private[-_]?key|api[-_]?key|token|credentials?|sic-key)([._-]|$)",
    re.I,
)
_SECRET_ALLOWED = frozenset({"use-shared-secret"})
_DYNAMIC_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PAGE_SIZE = 500  # documented maximum for limit on show-* collections
_LOG_PAGE_SIZE = 100  # max-logs-per-request ceiling used by the official MCP server
_MAX_BODY = 32 * 1024 * 1024


@lru_cache(maxsize=1)
def _catalog():
    catalog = json.loads(Path(__file__).with_name("checkpoint").joinpath("catalog.json").read_text())
    allowed = set(catalog["allowed_commands"])
    for name, table in catalog["tables"].items():
        commands = table.get("commands", [table["command"]])
        if not all(c.startswith("show-") for c in commands) or not set(commands) <= allowed:
            raise CheckPointError("InvalidCatalog", f"Table {name} references a command outside the show-* allow-list.")
        if set(table["parents"]) & set(table["fields"]) - {"layer", "package"}:
            raise CheckPointError("InvalidCatalog", f"Table {name} parent collides with a field.")
        if not table["fields"]:
            raise CheckPointError("InvalidCatalog", f"Table {name} declares no fields.")
    if any(not (c.startswith("show-") or c in _SESSION_COMMANDS) for c in allowed):
        raise CheckPointError("InvalidCatalog", "A mutating command is present in the allow-list.")
    catalog["_allowed"] = frozenset(allowed) | _SESSION_COMMANDS
    return catalog


class CheckPointClient(DataSourceClient):
    capture_structured_queries = True
    relative_date_hint = (
        'Use JSON "lookback":"24h" (logs, audit_logs, tasks, changes); it is resolved at each execution. '
        "Do not freeze dates in saved investigations."
    )

    def __init__(
        self,
        url,
        username=None,
        password=None,
        api_key=None,
        domain=None,
        api_version=None,
        ca_certificate=None,
        allow_http=False,
        timeout=30,
        max_rows=10000,
        **kwargs,
    ):
        url = str(url).strip().rstrip("/")
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/")
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (path and not path.endswith("/web_api"))
            or ".." in path
        ):
            raise CheckPointError(
                "InvalidConfiguration",
                "Use the management origin (https://mgmt.example:443) or a Smart-1 Cloud URL ending in /web_api, "
                "without credentials, query or fragment.",
            )
        if parsed.scheme != "https" and (not allow_http or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise CheckPointError(
                "InvalidConfiguration", "HTTPS is required. HTTP is only available for a loopback simulator."
            )
        has_user = bool(username) and bool(password)
        if has_user == bool(api_key) or (bool(username) != bool(password)):
            raise CheckPointError(
                "InvalidConfiguration", "Supply either username and password or an API key, not both and not neither."
            )
        if api_version is not None and api_version != "":
            if not isinstance(api_version, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+){0,2}", api_version):
                raise CheckPointError("InvalidConfiguration", "api_version must look like 1.9.1.")
        else:
            api_version = None
        if domain is not None and domain != "":
            if not isinstance(domain, str) or len(domain) > 255 or any(c in domain for c in "\r\n\x00"):
                raise CheckPointError("InvalidConfiguration", "domain must be a short name or UID.")
        else:
            domain = None
        self.url = f"{parsed.scheme}://{parsed.netloc}{path or '/web_api'}"
        self.domain = domain
        self.api_version = api_version
        self.timeout = self._integer(timeout, "timeout", 1, 120)
        self.max_rows = self._integer(max_rows, "max_rows", 1, 100000)
        self._credentials = {"api-key": api_key} if api_key else {"user": username, "password": password}
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "bagofwords-checkpoint/1"}
        )
        self._ca = None
        if ca_certificate:
            self._ca = tempfile.TemporaryDirectory(prefix="bow-checkpoint-ca-")
            cert = Path(self._ca.name) / "ca.pem"
            cert.write_text(ca_certificate)
            cert.chmod(0o600)
            self._session.verify = str(cert)
        self._lock = threading.RLock()
        self._management = parsed.hostname
        self._login_info = {}

    def close(self):
        self._session.close()
        if self._ca:
            self._ca.cleanup()

    def __repr__(self):
        return f"CheckPointClient(url={self.url!r}, domain={self.domain!r})"

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise CheckPointError("InvalidQuery", f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    # ------------------------------------------------------------------ catalog
    @property
    def description(self):
        return """Check Point Management API connector (read-only session; Web API 1.9.1 contract, R81.10/R81.20/R82 compatible).
Call get_tables()/get_schema(name), then execute_query(query) with a JSON object or string. No SQL. Every query opens a read-only session, runs allow-listed show-* commands and logs out.
Tables: hosts, networks, address_ranges, groups, group_members, services_tcp, services_udp, service_groups, packages, access_layers, access_rules, nat_rules, threat_rules, gateways_and_servers, simple_gateways, sessions, unused_objects, tasks, logs, audit_logs, api_versions, session, last_published_session, domains, vpn_communities, changes.
Examples: {"table":"hosts","fields":["name","ipv4-address"],"filter":{"ipv4-address":{"op":"like","value":"10.1.%"}}}
          {"table":"access_rules","parent":{"layer":"Standard Network"},"filter":{"enabled":false}}
          {"table":"access_rules","parent":{"layer":"Standard Network"},"search":"10.1.20.5"} (server-side packet/text search)
          {"table":"nat_rules","parent":{"package":"Standard"}}
          {"table":"gateways_and_servers","fields":["name","version","policy.access-policy-name","policy.access-policy-installation-date.iso-8601"]}
          {"table":"logs","lookback":"24h","search":"action:Drop AND blade:Firewall","limit":500}
          {"table":"audit_logs","time_frame":"last-7-days","search":"operation:\\"Install Policy\\""}
          {"table":"changes","lookback":"7d"}  {"table":"tasks","status":"failed","lookback":"30d"}
Controls: table, fields, filter (client-side: scalar equality or {"op":"eq|ne|gt|ge|lt|le|like|in|contains","value":...}), search (server-side Check Point filter text: SmartConsole object search, rulebase packet search, SmartView log query language), parent (access_rules/threat_rules need layer; nat_rules needs package), limit, order_by (["name asc"]) where supported, lookback "30m|24h|7d" or UTC start_time/end_time (logs, audit_logs, tasks, changes), time_frame (logs: last-hour, today, last-24-hours, yesterday, last-7-days, this-week, this-month, last-30-days, all-time).
Rules come flattened in rulebase order: section, rule-number, enabled, action, source/destination/service as name arrays (plus .uids/.types), track, install-on. Disabled rules never match. Any-Any-Accept above a rule shadows it; the last Cleanup rule shows what is dropped by default.
Results are pandas DataFrames: scalar columns use the API's dotted kebab-case names (policy.access-policy-name), arrays stay structured, nullable dtypes, _bow_* provenance. limit is a complete-result ceiling (ResultLimitExceeded means narrow with search/parent/filter, never a silent truncation). Logs default to the last 24 hours; log values are strings as the log server returns them.
Pitfalls: gateways_and_servers.version vs the management version shows upgrade mismatches; policy.access-policy-installation-date older than last_published_session.publish-time means an uninstalled policy; sessions with state open, in-work false and locks>0 are stale locks. Blade flags are absent when the blade is off. Current objects do not prove past configuration: use changes and audit_logs for history. Simulator-verified; customer validation on a real Security Management Server is still required."""

    def get_tables(self):
        names = list(_catalog()["tables"])
        return [self.get_schema(n) for n in discovery_items(names, "tables", label=str)]

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    @staticmethod
    def _table(name):
        if not isinstance(name, str) or name not in _catalog()["tables"]:
            raise CheckPointError("InvalidQuery", "Unknown table; use get_tables to discover the reviewed tables.")
        return _catalog()["tables"][name]

    def get_schema(self, table_name):
        t = self._table(table_name)
        columns = [
            TableColumn(name=k, dtype=_TYPES.get(v["type"], "json"), description=v.get("description"))
            for k, v in t["fields"].items()
        ]
        names = {c.name for c in columns}
        for key in t["parents"]:
            if key not in names:
                columns.append(TableColumn(name=key, dtype="str", description="Explicit parent for this query."))
        columns += [TableColumn(name=k, dtype=v) for k, v in _META.items()]
        desc = f"{t['description']} Command: {', '.join(t.get('commands', [t['command']]))}."
        if t["parents"]:
            desc += " Required parent: " + ", ".join(t["parents"]) + "."
        if t["controls"]:
            desc += " Controls: " + ", ".join(t["controls"]) + "."
        if t.get("notes"):
            desc += " " + t["notes"]
        fks = []
        if table_name == "group_members":
            fks.append(
                ForeignKey(
                    column=TableColumn(name="group.uid", dtype="str"),
                    references_name="groups",
                    references_column=TableColumn(name="uid", dtype="str"),
                )
            )
        if table_name in {"access_rules", "threat_rules"}:
            fks.append(
                ForeignKey(
                    column=TableColumn(name="layer", dtype="str"),
                    references_name="access_layers",
                    references_column=TableColumn(name="name", dtype="str"),
                )
            )
        if table_name == "nat_rules":
            fks.append(
                ForeignKey(
                    column=TableColumn(name="package", dtype="str"),
                    references_name="packages",
                    references_column=TableColumn(name="name", dtype="str"),
                )
            )
        return Table(
            name=table_name,
            description=desc,
            columns=columns,
            pks=[c for c in columns if c.name == "uid"],
            fks=fks,
            metadata_json={
                "checkpoint": {
                    "command": t["command"],
                    "commands": t.get("commands", [t["command"]]),
                    "kind": t["kind"],
                    "parents": t["parents"],
                    "controls": t["controls"],
                    "details_level": t["details_level"],
                    "contract": _catalog()["contract"],
                    "dynamic_fields": bool(t.get("dynamic_fields")),
                }
            },
        )

    def prompt_schema(self):
        return "\n".join(
            f"{n}: {t['description'][:150]} | parents={','.join(t['parents']) or 'none'}"
            for n, t in _catalog()["tables"].items()
        )

    @staticmethod
    def allowed_commands():
        return sorted(_catalog()["_allowed"])

    # ------------------------------------------------------------------ transport
    def _post(self, command, body, sid, deadline):
        if command not in _catalog()["_allowed"]:
            raise CheckPointError("ForbiddenOperation", "Only allow-listed read-only show-* commands can be sent.")
        if not isinstance(body, dict):
            raise CheckPointError("InvalidQuery", "Request body must be an object.")
        url = self.url + (f"/v{self.api_version}" if self.api_version else "") + "/" + command
        headers = {"X-chkp-sid": sid} if sid else {}
        payload = json.dumps(body)
        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckPointError("BudgetExceeded", "Query deadline exhausted.")
            try:
                with self._session.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=min(self.timeout, remaining),
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    status = response.status_code
                    if status in {429, 502, 503, 504} and attempt < 3:
                        retry = response.headers.get("Retry-After", "")
                        delay = float(retry) if retry.isdigit() else 0.25 * 2**attempt
                        if delay >= deadline - time.monotonic():
                            raise CheckPointError("BudgetExceeded", "Retry exceeds query deadline.")
                        time.sleep(delay)
                        continue
                    if status != 200:
                        raise self._error(command, status, response)
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw) > _MAX_BODY:
                            raise CheckPointError("BudgetExceeded", "Response exceeds 32 MiB.")
                        if time.monotonic() > deadline:
                            raise CheckPointError("BudgetExceeded", "Query deadline exhausted.")
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise CheckPointError("InvalidResponse", "The management server did not return JSON.") from None
                    if not isinstance(data, dict):
                        raise CheckPointError("InvalidResponse", "Expected a JSON object reply.")
                    code = data.get("code")
                    if isinstance(code, str) and "message" in data and code.startswith(("err_", "generic_")):
                        # Defensive: an error envelope on HTTP 200 is not data.
                        raise CheckPointError("RemoteError", "The management server returned an error envelope.", status, code)
                    return data
            except requests.exceptions.SSLError:
                raise CheckPointError("TLSFailed", "Certificate verification failed; configure the trusted CA.") from None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 3 or command == "logout":
                    raise CheckPointError(
                        "TransportFailed", "The management server could not be reached within the retry budget."
                    ) from None
                time.sleep(min(0.25 * 2**attempt, max(0, deadline - time.monotonic())))
        raise CheckPointError("TransportFailed", "The management server could not be reached.")

    @staticmethod
    def _error(command, status, response):
        vendor = None
        try:
            raw = response.raw.read(8192)
            parsed = json.loads(raw)
            code = parsed.get("code") if isinstance(parsed, dict) else None
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
                vendor = code
        except (ValueError, AttributeError, TypeError, UnicodeError):
            pass
        auth_codes = {
            "err_login_failed",
            "err_login_failed_wrong_username_or_password",
            "err_login_failed_more_than_one_opened_session",
            "generic_err_missing_session_id",
            "generic_err_wrong_session_id",
            "generic_err_session_expired",
            "generic_err_session_in_use",
        }
        unsupported = {"generic_err_command_not_found", "generic_err_command_version_not_found", "err_unknown_api_version"}
        if status == 401 or vendor in auth_codes:
            code, msg = "AuthenticationFailed", "Credentials or session were not accepted by the management server."
        elif status == 403 or vendor in {"generic_err_no_permissions", "err_forbidden"}:
            code, msg = "PermissionDenied", "The administrator cannot run this read-only command; check its permission profile."
        elif vendor in unsupported:
            code, msg = "UnsupportedCommand", f"{command} is not available on this management server or API version."
        elif vendor == "generic_err_object_not_found" or status == 404:
            code, msg = "UnsupportedResource", "The requested layer, package or object does not exist."
        elif vendor == "err_too_many_requests" or status == 429:
            code, msg = "Throttled", "The management server is rate limiting requests; retry later."
        elif status == 400:
            code, msg = "InvalidRequest", f"The management server rejected the {command} request."
        elif 300 <= status < 400:
            code, msg = "RemoteError", "Redirects are not followed; use the management origin."
        else:
            code, msg = "RemoteError", f"The management server returned HTTP {status} for {command}."
        return CheckPointError(code, msg, status, vendor)

    @contextmanager
    def _session_scope(self):
        with self._lock:
            deadline = time.monotonic() + 120
            body = {**self._credentials, "read-only": True}
            if self.domain:
                body["domain"] = self.domain
            login = self._post("login", body, None, deadline)
            sid = login.get("sid")
            if not isinstance(sid, str) or not sid.strip():
                raise CheckPointError("InvalidResponse", "Login did not return a session identifier.")
            if login.get("read-only") is False:
                raise CheckPointError("ReadOnlyRequired", "The server opened a read-write session; the connector refuses it.")
            self._login_info = {
                "api-server-version": login.get("api-server-version"),
                "url": login.get("url"),
                "standby": login.get("standby"),
                "uid": login.get("uid"),
                "read-only": login.get("read-only"),
            }
            try:
                yield sid, deadline
            finally:
                try:
                    self._post("logout", {}, sid, min(deadline, time.monotonic() + 10))
                except CheckPointError:
                    pass

    # ------------------------------------------------------------------ connection test
    def test_connection(self):
        with self._session_scope() as (sid, deadline):
            versions = self._post("show-api-versions", {}, sid, deadline)
            current = versions.get("current-version")
            supported = versions.get("supported-versions")
            if not isinstance(current, str) or not isinstance(supported, list) or current not in supported:
                raise CheckPointError("InvalidResponse", "show-api-versions did not return a consistent version list.")
            if self.api_version and self.api_version not in supported:
                raise CheckPointError(
                    "UnsupportedVersion", f"API version {self.api_version} is not supported by this server."
                )
            me = self._post("show-session", {}, sid, deadline)
            if me.get("read-only") is False or me.get("connection-mode") not in (None, "read only"):
                raise CheckPointError("ReadOnlyRequired", "The management server did not grant a read-only session.")
            user = me.get("user-name")
        tables = len(_catalog()["tables"])
        who = f" as {user}" if isinstance(user, str) and user else ""
        where = f" domain {self.domain}" if self.domain else ""
        return {
            "success": True,
            "message": (
                f"Connected to Check Point Management API {current}{who}{where} with a read-only session; "
                f"{tables} tables available. Simulator-verified; customer validation remains required."
            ),
        }

    # ------------------------------------------------------------------ query validation
    @staticmethod
    def _text(value, name, maximum=1000):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(c in value for c in "\r\n\x00"):
            raise CheckPointError("InvalidQuery", f"{name} must be a nonempty single-line string of at most {maximum} chars.")
        return value

    @staticmethod
    def _date(value):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError()
            return dt.astimezone(UTC)
        except (ValueError, AttributeError, TypeError):
            raise CheckPointError("InvalidQuery", "Times require an ISO-8601 timestamp with timezone.") from None

    def _bounds(self, spec, now):
        if not any(k in spec for k in ("lookback", "start_time", "end_time")):
            return None
        if "lookback" in spec:
            if "start_time" in spec or "end_time" in spec:
                raise CheckPointError("InvalidQuery", "Use lookback or explicit start_time/end_time, not both.")
            match = re.fullmatch(r"([1-9][0-9]{0,4})(m|h|d|w)", str(spec["lookback"]))
            if not match:
                raise CheckPointError("InvalidQuery", "lookback must be a positive number followed by m, h, d or w.")
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
            if seconds > 366 * 86400:
                raise CheckPointError("InvalidQuery", "lookback cannot exceed one year.")
            return now - timedelta(seconds=seconds), now
        if "start_time" not in spec or "end_time" not in spec:
            raise CheckPointError("InvalidQuery", "Supply both start_time and end_time.")
        start, end = self._date(spec["start_time"]), self._date(spec["end_time"])
        if start >= end or end > now + timedelta(seconds=30):
            raise CheckPointError("InvalidQuery", "Time range must be ordered and cannot extend into the future.")
        return start, end

    def _validate_fields(self, table, requested):
        if requested is None:
            return list(table["fields"])
        if not isinstance(requested, list) or not requested or not all(isinstance(x, str) for x in requested):
            raise CheckPointError("InvalidQuery", "fields must be a nonempty list of column names.")
        out = []
        for name in requested:
            if name in table["fields"]:
                out.append(name)
            elif table.get("dynamic_fields") and _DYNAMIC_FIELD.fullmatch(name) and not name.startswith("_bow"):
                out.append(name)
            else:
                raise CheckPointError("InvalidQuery", f"Unknown or excluded field: {name}.")
        return list(dict.fromkeys(out))

    def _validate_filter(self, table, filters):
        if filters is None:
            return {}
        if not isinstance(filters, dict):
            raise CheckPointError("InvalidQuery", "filter must be an object of column -> value or {op, value}.")
        out = {}
        for column, condition in filters.items():
            if column not in table["fields"] and not (
                table.get("dynamic_fields") and isinstance(column, str) and _DYNAMIC_FIELD.fullmatch(column)
            ):
                raise CheckPointError("InvalidQuery", f"Unknown or non-filterable column: {column}.")
            if isinstance(condition, dict):
                if set(condition) != {"op", "value"} or condition["op"] not in _OPS:
                    raise CheckPointError("InvalidQuery", "Comparisons use {op, value} with a supported op.")
                op, value = condition["op"], condition["value"]
            else:
                op, value = "eq", condition
            if op == "in":
                if not isinstance(value, list) or not 1 <= len(value) <= 500 or any(isinstance(v, (dict, list)) for v in value):
                    raise CheckPointError("InvalidQuery", "in requires 1-500 scalar values.")
            elif isinstance(value, (dict, list)):
                raise CheckPointError("InvalidQuery", "Filter values must be scalars.")
            elif isinstance(value, float) and not math.isfinite(value):
                raise CheckPointError("InvalidQuery", "Filter values must be finite.")
            if op in {"like", "contains"} and not isinstance(value, str):
                raise CheckPointError("InvalidQuery", f"{op} requires a string pattern.")
            out[column] = (op, value)
        return out

    def _validate_order(self, table, order):
        if order is None:
            return None
        if not isinstance(order, list) or not order or len(order) > 3:
            raise CheckPointError("InvalidQuery", "order_by must be a list of 'field asc|desc' strings.")
        out = []
        for term in order:
            parts = term.split() if isinstance(term, str) else []
            if not 1 <= len(parts) <= 2 or parts[0] not in table["order_fields"] or (
                len(parts) == 2 and parts[1] not in {"asc", "desc"}
            ):
                raise CheckPointError("InvalidQuery", f"order_by supports {', '.join(table['order_fields'])} with asc/desc.")
            out.append({"DESC" if len(parts) == 2 and parts[1] == "desc" else "ASC": parts[0]})
        return out

    def _validate(self, query):
        try:
            spec = json.loads(query) if isinstance(query, str) else dict(query) if isinstance(query, dict) else None
        except (ValueError, TypeError):
            raise CheckPointError("InvalidQuery", "Query must be a JSON object.") from None
        if not isinstance(spec, dict):
            raise CheckPointError("InvalidQuery", "Query must be a JSON object.")
        table = self._table(spec.get("table"))
        allowed = _BASE_CONTROLS | set(table["controls"])
        if set(spec) - allowed:
            raise CheckPointError("InvalidQuery", f"Unknown query controls for {spec['table']}: {sorted(set(spec) - allowed)}.")
        plan = {"table": spec["table"], "t": table}
        plan["limit"] = self._integer(spec.get("limit", self.max_rows), "limit", 1, self.max_rows)
        parents = spec.get("parent", {})
        if not isinstance(parents, dict) or set(parents) != set(table["parents"]):
            raise CheckPointError(
                "InvalidQuery", f"Supply exactly the required parent(s) for {spec['table']}: {table['parents'] or 'none'}."
            )
        plan["parent"] = {k: self._text(v, f"parent {k}", 255) for k, v in parents.items()}
        plan["fields"] = self._validate_fields(table, spec.get("fields"))
        plan["filter"] = self._validate_filter(table, spec.get("filter"))
        plan["order"] = self._validate_order(table, spec.get("order_by")) if "order_by" in spec else None
        plan["search"] = self._text(spec["search"], "search") if "search" in spec else None
        plan["package"] = self._text(spec["package"], "package", 255) if "package" in spec else None
        now = datetime.now(UTC)
        plan["now"] = now
        plan["bounds"] = self._bounds(spec, now)
        if "time_frame" in spec:
            if spec["time_frame"] not in _TIME_FRAMES:
                raise CheckPointError("InvalidQuery", f"time_frame must be one of {sorted(_TIME_FRAMES)}.")
            if plan["bounds"]:
                raise CheckPointError("InvalidQuery", "Use time_frame or lookback/start_time, not both.")
            plan["time_frame"] = spec["time_frame"]
        if "status" in spec:
            if spec["status"] not in _TASK_STATUS:
                raise CheckPointError("InvalidQuery", f"status must be one of {sorted(_TASK_STATUS)}.")
            plan["status"] = spec["status"]
        if "initiator" in spec:
            plan["initiator"] = self._text(spec["initiator"], "initiator", 255)
        for key in ("show_hits", "view_published_sessions"):
            if key in spec:
                if not isinstance(spec[key], bool):
                    raise CheckPointError("InvalidQuery", f"{key} must be a boolean.")
                plan[key] = spec[key]
        if "log_servers" in spec:
            servers = spec["log_servers"]
            if (
                not isinstance(servers, list)
                or not servers
                or len(servers) > 20
                or not all(isinstance(s, str) and re.fullmatch(r"[0-9A-Za-z.:-]{1,64}", s) for s in servers)
            ):
                raise CheckPointError("InvalidQuery", "log_servers must be a list of IP addresses or host names.")
            plan["log_servers"] = servers
        for key in ("from_session", "to_session"):
            if key in spec:
                plan[key] = self._text(spec[key], key, 64)
        if "limit_sessions" in spec:
            plan["limit_sessions"] = self._integer(spec["limit_sessions"], "limit_sessions", 1, 500)
        return plan

    # ------------------------------------------------------------------ collectors
    @staticmethod
    def _page_envelope(data, container):
        items = data.get(container)
        if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
            raise CheckPointError("InvalidResponse", f"Expected a list of objects under '{container}'.")
        env = {}
        for key in ("from", "to", "total"):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CheckPointError("InvalidResponse", f"Paging envelope '{key}' is missing or invalid.")
            env[key] = value
        return items, env

    def _collect(self, sid, deadline, command, body, container, hard_cap):
        records, offset = [], 0
        for _ in range(1000):
            page = self._post(command, {**body, "limit": _PAGE_SIZE, "offset": offset}, sid, deadline)
            items, env = self._page_envelope(page, container)
            if env["total"] > hard_cap or len(records) + len(items) > hard_cap:
                raise CheckPointError(
                    "ResultLimitExceeded", "The collection exceeds the complete-result ceiling; narrow with search or parent."
                )
            records.extend(items)
            if env["to"] >= env["total"] or env["total"] == 0:
                return records, page
            if not items or env["to"] <= offset:
                raise CheckPointError("InvalidResponse", "Pagination made no progress; refusing an incomplete result.")
            offset = env["to"]
        raise CheckPointError("BudgetExceeded", "Pagination budget exhausted.")

    def _collect_rulebase(self, sid, deadline, plan):
        t = plan["t"]
        body = dict(t["extra_body"])
        body["details-level"] = t["details_level"]
        if "layer" in plan["parent"]:
            body["name"] = plan["parent"]["layer"]
        if "package" in plan["parent"]:
            body["package"] = plan["parent"]["package"]
        if plan.get("package"):
            body["package"] = plan["package"]
        if plan.get("search"):
            body["filter"] = plan["search"]
        if plan.get("show_hits"):
            body["show-hits"] = True
        dictionary, rules, offset = {}, [], 0
        for _ in range(1000):
            page = self._post(t["command"], {**body, "limit": _PAGE_SIZE, "offset": offset}, sid, deadline)
            items, env = self._page_envelope(page, "rulebase")
            for obj in page.get("objects-dictionary") or []:
                if isinstance(obj, dict) and isinstance(obj.get("uid"), str):
                    dictionary[obj["uid"]] = obj
            count = 0
            for item in items:
                nested = item.get("rulebase")
                if isinstance(nested, list):
                    for rule in nested:
                        if not isinstance(rule, dict):
                            raise CheckPointError("InvalidResponse", "Rulebase sections must contain rule objects.")
                        rules.append((item, rule))
                        count += 1
                else:
                    rules.append((None, item))
                    count += 1
            if len(rules) > self.max_rows:
                raise CheckPointError("ResultLimitExceeded", "The rulebase exceeds the complete-result ceiling.")
            if env["to"] >= env["total"] or env["total"] == 0:
                break
            if count == 0 or env["to"] <= offset:
                raise CheckPointError("InvalidResponse", "Rulebase pagination made no progress.")
            offset = env["to"]
        else:
            raise CheckPointError("BudgetExceeded", "Pagination budget exhausted.")
        layer_uid = page.get("uid")
        rows = []
        for section, rule in rules:
            row = self._flatten(rule, dictionary)
            row.update(
                {
                    "layer": plan["parent"].get("layer"),
                    "layer.uid": layer_uid,
                    "package": plan["parent"].get("package"),
                    "rulebase.uid": layer_uid,
                    "section": section.get("name") if section else None,
                    "section.uid": section.get("uid") if section else None,
                }
            )
            rows.append(row)
        return rows

    @staticmethod
    def _ref(value, dictionary, attr):
        if isinstance(value, dict):
            return value.get(attr)
        if isinstance(value, str):
            obj = dictionary.get(value)
            if obj is None:
                return value if attr == "uid" else None
            return obj.get(attr)
        return None

    def _flatten(self, rule, dictionary):
        row = {}
        ref_lists = {
            "source", "destination", "service", "vpn", "content", "time", "install-on", "protected-scope", "exceptions",
        }
        ref_single = {
            "action", "track", "inline-layer", "original-source", "original-destination", "original-service",
            "translated-source", "translated-destination", "translated-service",
        }
        for key, value in rule.items():
            if key == "track" and isinstance(value, dict):
                # R81+ track is an object whose "type" references the track action (Log, None, ...).
                kind = value.get("type")
                row["track"] = self._ref(kind, dictionary, "name") or self._ref(kind, dictionary, "uid")
                row["track.uid"] = self._ref(kind, dictionary, "uid")
                if "track-alert" not in rule and "alert" in value:
                    row["track-alert"] = value.get("alert")
                row["track-settings"] = value
            elif key in ref_lists and isinstance(value, list):
                row[key] = [self._ref(v, dictionary, "name") or self._ref(v, dictionary, "uid") for v in value]
                row[key + ".uids"] = [self._ref(v, dictionary, "uid") for v in value]
                row[key + ".types"] = [self._ref(v, dictionary, "type") for v in value]
            elif key in ref_single and isinstance(value, (str, dict)):
                row[key] = self._ref(value, dictionary, "name") or self._ref(value, dictionary, "uid")
                row[key + ".uid"] = self._ref(value, dictionary, "uid")
            else:
                row[key] = value
        return row

    def _collect_logs(self, sid, deadline, plan):
        t = plan["t"]
        query = {"type": t["log_type"], "max-logs-per-request": _LOG_PAGE_SIZE}
        if plan.get("search"):
            query["filter"] = plan["search"]
        if plan.get("bounds"):
            start, end = plan["bounds"]
            query["time-frame"] = "custom"
            query["custom-start"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            query["custom-end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            query["time-frame"] = plan.get("time_frame", "last-24-hours")
        if plan.get("log_servers"):
            query["log-servers"] = plan["log_servers"]
        body = {"new-query": query}
        records, query_id = [], None
        for _ in range(1000):
            page = self._post("show-logs", body, sid, deadline)
            logs = page.get("logs")
            if logs is None and "tops" in page:
                raise CheckPointError("InvalidResponse", "Unexpected tops reply for a logs query.")
            if not isinstance(logs, list) or not all(isinstance(r, dict) for r in logs):
                raise CheckPointError("InvalidResponse", "show-logs did not return a list of log records.")
            qid = page.get("query-id")
            if not isinstance(qid, str) or not qid or (query_id is not None and qid != query_id):
                raise CheckPointError("InvalidResponse", "show-logs continuation changed or lost its query-id.")
            query_id = qid
            records.extend(logs)
            if len(records) > self.max_rows:
                raise CheckPointError("ResultLimitExceeded", "Log result exceeds the complete-result ceiling; narrow the search or window.")
            status = page.get("query-status")
            if not logs or (isinstance(status, str) and status.lower() in {"done", "complete", "completed"}):
                return records
            body = {"query-id": query_id}
        raise CheckPointError("BudgetExceeded", "Log pagination budget exhausted.")

    def _collect_changes(self, sid, deadline, plan):
        body = {"details-level": "standard"}
        if plan.get("bounds"):
            start, end = plan["bounds"]
            body["from-date"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            body["to-date"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        if plan.get("from_session"):
            body["from-session"] = plan["from_session"]
        if plan.get("to_session"):
            body["to-session"] = plan["to_session"]
        body["limit"] = plan.get("limit_sessions", 50)
        reply = self._post("show-changes", body, sid, deadline)
        tasks = reply.get("tasks")
        task_id = reply.get("task-id")
        if tasks is None:
            if not isinstance(task_id, str) or not task_id:
                raise CheckPointError("InvalidResponse", "show-changes returned neither tasks nor a task-id.")
            while True:
                status_reply = self._post("show-task", {"task-id": task_id, "details-level": "full"}, sid, deadline)
                tasks = status_reply.get("tasks")
                if not isinstance(tasks, list) or not tasks or not all(isinstance(x, dict) for x in tasks):
                    raise CheckPointError("InvalidResponse", "show-task did not describe the show-changes task.")
                state = str(tasks[0].get("status", "")).lower()
                if state in {"succeeded", "partially succeeded"}:
                    break
                if state == "failed":
                    raise CheckPointError("RemoteError", "The show-changes task failed on the management server.")
                if time.monotonic() + 1 > deadline:
                    raise CheckPointError("BudgetExceeded", "show-changes did not complete within the query deadline.")
                time.sleep(1)
        if not isinstance(tasks, list) or not all(isinstance(x, dict) for x in tasks):
            raise CheckPointError("InvalidResponse", "show-changes tasks must be objects.")
        rows = []
        for task in tasks:
            for detail in task.get("task-details") or []:
                for change in (detail.get("changes") or []) if isinstance(detail, dict) else []:
                    session = change.get("session") or {}
                    ops = change.get("operations") or {}
                    for op, key in (("added", "added-objects"), ("deleted", "deleted-objects")):
                        for obj in ops.get(key) or []:
                            rows.append({"session": session, "operation": op, "object": obj, "task-id": task.get("task-id")})
                    for obj in ops.get("modified-objects") or []:
                        rows.append(
                            {
                                "session": session,
                                "operation": "modified",
                                "object": obj.get("new-object") if isinstance(obj, dict) else None,
                                "old-object": obj.get("old-object") if isinstance(obj, dict) else None,
                                "task-id": task.get("task-id"),
                            }
                        )
        return rows

    def _collect_objects(self, sid, deadline, plan, command=None):
        t = plan["t"]
        body = dict(t["extra_body"])
        body["details-level"] = t["details_level"]
        if plan.get("search"):
            body["filter"] = plan["search"]
        if plan.get("order"):
            body["order"] = plan["order"]
        if plan.get("view_published_sessions"):
            body["view-published-sessions"] = True
        if "status" in plan:
            body["status"] = plan["status"]
        if "initiator" in plan:
            body["initiator"] = plan["initiator"]
        if plan.get("bounds") and plan["table"] == "tasks":
            start, end = plan["bounds"]
            body["from-date"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            body["to-date"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        records, _ = self._collect(sid, deadline, command or t["command"], body, t["container"], self.max_rows)
        return records

    def _run(self, plan):
        t, kind = plan["t"], plan["t"]["kind"]
        with self._session_scope() as (sid, deadline):
            if kind == "objects":
                rows = self._collect_objects(sid, deadline, plan)
                if plan["table"] in {"groups", "service_groups"}:
                    for r in rows:
                        members = r.get("members")
                        r["member-count"] = len(members) if isinstance(members, list) else None
            elif kind == "group_members":
                rows = []
                for group in self._collect_objects(sid, deadline, plan):
                    for member in group.get("members") or []:
                        if isinstance(member, dict):
                            rows.append({"group": group, "member": member})
                        elif isinstance(member, str):
                            rows.append({"group": group, "member": {"uid": member}})
            elif kind == "rulebase":
                rows = self._collect_rulebase(sid, deadline, plan)
            elif kind == "logs":
                rows = self._collect_logs(sid, deadline, plan)
            elif kind == "api_versions":
                reply = self._post(t["command"], {}, sid, deadline)
                current, supported = reply.get("current-version"), reply.get("supported-versions")
                if not isinstance(supported, list) or not all(isinstance(v, str) for v in supported):
                    raise CheckPointError("InvalidResponse", "show-api-versions did not return supported-versions.")
                rows = [{"version": v, "current": v == current} for v in supported]
            elif kind == "singleton":
                reply = self._post(t["command"], {}, sid, deadline)
                if plan["table"] == "session":
                    reply = {
                        **reply,
                        "api-server-version": self._login_info.get("api-server-version"),
                        "login": {"url": self._login_info.get("url"), "standby": self._login_info.get("standby")},
                    }
                rows = [reply]
            elif kind == "vpn_communities":
                rows = []
                for command in t["commands"]:
                    kind_name = "meshed" if command.endswith("meshed") else "star"
                    for r in self._collect_objects(sid, deadline, plan, command):
                        rows.append({**r, "community-type": kind_name})
            elif kind == "changes":
                rows = self._collect_changes(sid, deadline, plan)
            else:
                raise CheckPointError("InvalidCatalog", f"Unknown table kind {kind}.")
        return rows

    # ------------------------------------------------------------------ shaping
    @staticmethod
    def _extract(record, dotted):
        current = record
        if dotted in record:
            return CheckPointClient._sanitize(record[dotted])
        for part in dotted.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return CheckPointClient._sanitize(current)

    @staticmethod
    def _sanitize(value):
        if isinstance(value, dict):
            return {
                k: CheckPointClient._sanitize(v)
                for k, v in value.items()
                if k in _SECRET_ALLOWED or not _SECRET_KEY.search(str(k))
            }
        if isinstance(value, list):
            return [CheckPointClient._sanitize(v) for v in value]
        return value

    @staticmethod
    def _coerce(value, typ, column):
        if value is None:
            return None
        try:
            if typ == "integer":
                if isinstance(value, bool):
                    raise ValueError()
                if isinstance(value, str):
                    if not re.fullmatch(r"-?[0-9]{1,20}", value.strip()):
                        raise ValueError()
                    return int(value)
                if isinstance(value, float) and value.is_integer():
                    return int(value)
                if isinstance(value, int):
                    return value
                raise ValueError()
            if typ == "number":
                if isinstance(value, bool) or isinstance(value, (dict, list)):
                    raise ValueError()
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError()
                return number
            if typ == "boolean":
                if value is True or value == "true":
                    return True
                if value is False or value == "false":
                    return False
                raise ValueError()
            if typ == "string":
                if isinstance(value, bool):
                    return str(value).lower()
                if isinstance(value, (int, float)):
                    return str(value)
                if isinstance(value, (dict, list)):
                    return json.dumps(value, sort_keys=True)
                return str(value)
            return value
        except (ValueError, TypeError, OverflowError):
            raise CheckPointError("InvalidResponse", f"Value for {column} does not match its declared type {typ}.") from None

    @staticmethod
    def _matches(actual, op, value):
        if op == "eq":
            return actual == value
        if op == "ne":
            return actual != value
        if op == "in":
            return actual in value
        if op == "contains":
            if isinstance(actual, list):
                return any(value in str(x) for x in actual if x is not None)
            return actual is not None and value in str(actual)
        if op == "like":
            pattern = "^" + ".*".join(re.escape(p) for p in value.split("%")) + "$"
            return actual is not None and re.fullmatch(pattern, str(actual), re.S) is not None
        if actual is None or isinstance(actual, (dict, list)):
            return False
        try:
            return {"gt": actual > value, "ge": actual >= value, "lt": actual < value, "le": actual <= value}[op]
        except TypeError:
            return False

    def execute_query(self, query):
        plan = self._validate(query)
        t = plan["t"]
        query_id = str(uuid.uuid4())
        records = self._run(plan)
        fields = plan["fields"]
        rows = []
        for record in records:
            row = {k: self._extract(record, k) for k in fields}
            for k, (op, value) in plan["filter"].items():
                actual = row[k] if k in row else self._extract(record, k)
                typ = t["fields"].get(k, {}).get("type", "json")
                if typ != "json":
                    actual = self._coerce(actual, typ, k)
                if not self._matches(actual, op, value):
                    break
            else:
                row.update(plan["parent"])
                row.update(
                    _bow_management=self._management,
                    _bow_domain=self.domain,
                    _bow_api_version=self._login_info.get("api-server-version") or self.api_version,
                    _bow_connection=getattr(self, "_bow_connection_id", None),
                    _bow_retrieved_at=plan["now"].isoformat(),
                    _bow_query_id=query_id,
                    _bow_command=t["command"],
                    _bow_contract=_catalog()["sha256"],
                )
                rows.append(row)
        if len(rows) > plan["limit"]:
            raise CheckPointError(
                "ResultLimitExceeded", "Result exceeds limit; narrow the query. No rows were silently truncated."
            )
        columns = list(dict.fromkeys(fields + list(plan["parent"]) + list(_META)))
        frame = pd.DataFrame({k: pd.Series([row.get(k) for row in rows], dtype="object") for k in columns})
        for k in fields:
            typ = t["fields"].get(k, {}).get("type", "json")
            values = [self._coerce(v, typ, k) for v in frame[k]]
            if typ == "integer":
                frame[k] = pd.array(values, dtype="Int64")
            elif typ == "number":
                frame[k] = pd.array(values, dtype="Float64")
            elif typ == "boolean":
                frame[k] = pd.array(values, dtype="boolean")
            elif typ == "string":
                frame[k] = pd.array(values, dtype="string")
            else:
                frame[k] = pd.Series(values, dtype="object")
        frame["_bow_retrieved_at"] = pd.to_datetime(frame["_bow_retrieved_at"], utc=True)
        return frame
