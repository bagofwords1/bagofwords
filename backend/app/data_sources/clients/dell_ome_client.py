"""Read-only Dell OpenManage Enterprise (OME 3.x/4.x) investigation tables.

The frozen catalog (dell_ome/catalog.json) is an execution allow-list of GET
endpoints on the OME REST API (OData v4 flavoured). The only non-GET requests
this client can ever send are the documented session handshake:
POST /api/SessionService/Sessions and DELETE /api/SessionService/Sessions('<Id>').
OME is not Redfish; the iDRAC Redfish service is a different API on each server.
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
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_progress


class DellOmeError(ValueError):
    """Stable machine code plus a safe explanation; never echoes bodies or credentials."""

    def __init__(self, code, message, status=None):
        self.code, self.status = code, status
        self.ome_message_id = None
        super().__init__(f"{code}: {message}")


_SESSIONS = "/api/SessionService/Sessions"
_PAGE = 100
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_TYPES = {"integer": "int", "number": "float", "boolean": "bool", "string": "str", "array": "json", "object": "json"}
_META = {
    "_bow_appliance": "str",
    "_bow_connection": "str",
    "_bow_retrieved_at": "datetime",
    "_bow_query_id": "str",
    "_bow_endpoint": "str",
    "_bow_contract": "str",
}
# Table -> (filter field, join target) for the device identifier columns.
_DEVICE_KEYS = {
    "Id": ("devices", "Id"),
    "DeviceId": ("devices", "Id"),
    "AlertDeviceId": ("devices", "Id"),
    "BaselineId": ("firmware_baselines", "Id"),
    "JobId": ("jobs", "Id"),
    "GroupId": ("groups", "Id"),
    "CatalogId": ("firmware_catalogs", "Id"),
    "TemplateId": ("templates", "Id"),
}
_TIME_FILTER = {"alerts": "TimeStamp", "audit_logs": "CreatedDate"}
_SECRET = re.compile(r"(password|passphrase|secret|private_?key|access_?key|auth_?key|token|credential)", re.I)


@lru_cache(maxsize=1)
def _catalog():
    catalog = json.loads(Path(__file__).with_name("dell_ome").joinpath("catalog.json").read_text())
    for name, r in catalog["resources"].items():
        placeholders = set(re.findall(r"\{([^}]+)\}", r["path"]))
        if placeholders != set(r["parents"]) or "{" in re.sub(r"\{[^}]+\}", "x", r["path"]):
            raise DellOmeError("InvalidCatalog", f"Invalid parent contract for {name}.")
        for key, opt in r["optional_parents"].items():
            expected = placeholders | {key}
            if set(re.findall(r"\{([^}]+)\}", opt["path"])) != expected:
                raise DellOmeError("InvalidCatalog", f"Invalid optional parent contract for {name}.")
        if any(k not in r["fields"] for k in r["filters"]):
            raise DellOmeError("InvalidCatalog", f"Filter outside the field contract for {name}.")
        if r["explode"] and not (r["explode"].get("as") or r["explode"].get("prefix")):
            raise DellOmeError("InvalidCatalog", f"Invalid explode contract for {name}.")
    return catalog


def _path_pattern(template):
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", r"[^/()']+", re.escape(template)) + "$")


@lru_cache(maxsize=1)
def _allowed_get_patterns():
    patterns = []
    for r in _catalog()["resources"].values():
        patterns.append(_path_pattern(r["path"]))
        for opt in r["optional_parents"].values():
            patterns.append(_path_pattern(opt["path"]))
    return tuple(patterns)


class DellOmeClient(DataSourceClient):
    # Preserve JSON evidence in the same query history as SQL/string connectors.
    capture_structured_queries = True
    relative_date_hint = 'Use JSON "lookback":"24h" on alerts/audit_logs; it is resolved at each execution. Do not freeze dates in saved investigations.'

    def __init__(self, url, username, password, ca_certificate=None, timeout=30, max_rows=10000, allow_http=False, **kwargs):
        url = str(url).rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
        ):
            raise DellOmeError(
                "InvalidConfiguration",
                "Use the appliance origin such as https://ome.example:443, without a path or credentials.",
            )
        if parsed.scheme == "http" and (not allow_http or parsed.hostname not in _LOOPBACK):
            raise DellOmeError("InvalidConfiguration", "HTTPS is required; HTTP is only allowed for a loopback simulator.")
        if not username or not password:
            raise DellOmeError("InvalidConfiguration", "An OpenManage Enterprise account with the Viewer role is required.")
        self.url = url
        self.timeout = self._integer(timeout, "timeout", 1, 120)
        self.max_rows = self._integer(max_rows, "max_rows", 1, 100000)
        self._username, self._password = username, password
        self._verify = True
        self._ca = None
        if ca_certificate:
            self._ca = tempfile.TemporaryDirectory(prefix="bow-ome-ca-")
            cert = Path(self._ca.name) / "ca.pem"
            cert.write_text(ca_certificate)
            cert.chmod(0o600)
            self._verify = str(cert)
        self._lock = threading.RLock()
        self._availability = {}
        self._appliance = parsed.hostname
        self._version = None

    def close(self):
        if self._ca:
            self._ca.cleanup()
            self._ca = None

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise DellOmeError("InvalidQuery", f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    # ------------------------------------------------------------------ schema
    @property
    def description(self):
        return """Dell OpenManage Enterprise (OME 3.x/4.x REST API, OData flavoured) read-only fleet investigation connector. Not Redfish: OME aggregates servers, chassis and switches; use iDRAC Redfish separately for per-node detail.
Call get_tables()/get_schema(name) then execute_query(query) with a JSON object/string: {"table":..., "fields":[...], "filter":{...}, "parent":{...}, "order_by":[...], "limit":N, "lookback":"24h"}.
Tables: appliance_info, devices, device_summary, device_types, device_inventory*, device_hardware_logs*, device_power*, device_temperature*, device_system_uptime*, alerts, alert_categories, warranties, firmware_baselines, firmware_compliance_reports*, firmware_catalogs, jobs, job_execution_histories*, job_execution_details*, groups, group_devices*, group_all_leaf_devices*, audit_logs, templates, configuration_baselines, configuration_compliance_reports*, management_domains, appliance_network (* = needs "parent").
Examples: {"table":"devices","filter":{"Status":4000}} (1000 normal, 3000 warning, 4000 critical, 2000 unknown); {"table":"alerts","filter":{"SeverityType":{"op":"ge","value":8}},"order_by":["TimeStamp desc"],"lookback":"7d"}; {"table":"device_inventory","parent":{"DeviceId":10001,"InventoryType":"serverPowerSupplies"}}; {"table":"firmware_compliance_reports","parent":{"BaselineId":50}} then filter Component.ComplianceStatus=="CRITICAL" in pandas; {"table":"job_execution_details","parent":{"JobId":10004,"ExecutionHistoryId":1000400}} (Value holds the failure text).
Filters: scalar = equality; {"op":"eq|ne|gt|ge|lt|le|contains|in","value":...} only on fields listed as filters in get_schema (OME supports a small $filter subset; parent-scoped sub-resources have no server-side filter; warranties do not support date comparison, filter EndDate/DaysRemaining in pandas). order_by only where $orderby is documented (devices, alerts, groups, warranties); jobs cannot be ordered server-side.
Results are pandas DataFrames with dotted columns for nested scalars (LastRunStatus.Id, ComplianceSummary.NumberOfCritical, Component.CurrentVersion), structured arrays (Targets, DeviceManagement), nullable dtypes and _bow_* provenance columns. limit is a complete-result ceiling: the query fails rather than silently truncating.
Timestamps are appliance text in UTC without zone (parse with pd.to_datetime(..., utc=True)). Severity ids: 16 critical, 8 warning, 4 normal, 2 info. Join devices.Id to alerts.AlertDeviceId, firmware_compliance_reports.DeviceId, warranties.DeviceIdentifier=devices.DeviceServiceTag.
No SQL, raw URLs, actions or mutations exist; the account only needs the Viewer role. Absence of alerts or inventory is a coverage gap, never proof of health."""

    def get_tables(self):
        names = list(_catalog()["resources"])
        return [self.get_schema(n) for n in discovery_items(names, "tables", label=str)]

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    @staticmethod
    def _resource(name):
        if not isinstance(name, str) or name not in _catalog()["resources"]:
            raise DellOmeError("InvalidQuery", "Unknown table; use get_tables to discover the reviewed tables.")
        return _catalog()["resources"][name]

    def get_schema(self, table_name):
        r = self._resource(table_name)
        columns = [
            TableColumn(name=k, dtype=_TYPES.get(v["type"], "json"), description=v.get("description"))
            for k, v in r["fields"].items()
        ]
        names = {c.name for c in columns}
        for key, contract in list(r["parents"].items()) + list(r["optional_parents"].items()):
            if key not in names:
                columns.append(TableColumn(name=key, dtype=_TYPES.get(contract["type"], "str"),
                                           description="Explicit parent identity supplied in the query."))
        columns += [TableColumn(name=k, dtype=v) for k, v in _META.items()]
        desc = f"{r['description']} GET {r['path']}."
        if r["parents"]:
            desc += " Required parent: " + ", ".join(r["parents"]) + "."
        if r["optional_parents"]:
            desc += " Optional parent: " + ", ".join(r["optional_parents"]) + "."
        if r["filters"]:
            desc += " Filters: " + ", ".join(r["filters"]) + "."
        if "$orderby" in r["controls"]:
            desc += " Supports order_by."
        desc += f" Evidence: {r['evidence']}."
        device_rows = table_name in {"group_devices", "group_all_leaf_devices", "configuration_compliance_reports"}
        fks = [
            ForeignKey(column=c, references_name=_DEVICE_KEYS[c.name][0],
                       references_column=TableColumn(name=_DEVICE_KEYS[c.name][1], dtype="int"))
            for c in columns
            if c.name in _DEVICE_KEYS and (c.name != "Id" or device_rows) and _DEVICE_KEYS[c.name][0] != table_name
        ]
        return Table(
            name=table_name,
            description=desc,
            columns=columns,
            pks=[c for c in columns if c.name in r["identity"]],
            fks=fks,
            metadata_json={"dell_ome": {
                "endpoint": r["path"],
                "parents": list(r["parents"]),
                "parent_types": r["parents"],
                "optional_parents": list(r["optional_parents"]),
                "controls": r["controls"],
                "filters": r["filters"],
                "explode": r["explode"],
                "evidence": r["evidence"],
                "availability": self._availability.get(table_name, "unverified"),
                "contract": _catalog()["contract"],
                "collection": r["collection"],
            }},
        )

    def prompt_schema(self):
        return "\n".join(
            f"{n}: {r['description'][:150]} | parents={','.join(r['parents']) or 'none'}"
            for n, r in _catalog()["resources"].items()
        )

    def coverage_report(self):
        return {
            "contract": _catalog()["contract"],
            "sha256": _catalog()["sha256"],
            "validation": "simulator_verified_customer_validation_required",
            "tables": [{"table": n, "path": r["path"], "evidence": r["evidence"],
                        "availability": self._availability.get(n, "unverified")} for n, r in _catalog()["resources"].items()],
            "gaps": [
                "Power Manager plug-in metrics (MetricService) and report execution are excluded.",
                "Alert policies, users, discovery configuration and identity pools are excluded as non-diagnostic.",
                "Tables marked inferred/partial need confirmation on a real appliance before customer acceptance.",
            ],
        }

    # ------------------------------------------------------------- connection
    def test_connection(self):
        with self._lock:
            with self._session() as (session, deadline):
                body = self._get(session, "/api/ApplicationService/Info", {}, deadline)
        version = body.get("Version") if isinstance(body, dict) else None
        if not isinstance(version, str) or not re.fullmatch(r"\d+(\.\d+){1,3}", version.strip()):
            raise DellOmeError("InvalidResponse", "The appliance did not report an OpenManage Enterprise version.")
        major = int(version.split(".")[0])
        if major < 3:
            raise DellOmeError("UnsupportedVersion", "This connector requires OpenManage Enterprise 3.x or 4.x.")
        self._version = version.strip()
        guid = body.get("Guid")
        if isinstance(guid, str) and guid.strip():
            self._appliance = guid.strip()
        name = body.get("Name") if isinstance(body.get("Name"), str) else "OpenManage Enterprise"
        return {
            "success": True,
            "message": f"Connected to {name} {self._version}; {len(_catalog()['resources'])} read-only tables available. "
                       "Verified against a simulated appliance; confirm coverage on the real appliance.",
        }

    @contextmanager
    def _session(self):
        """Documented handshake: POST Sessions -> 201 + X-Auth-Token; DELETE Sessions('<Id>') -> 204."""
        deadline = time.monotonic() + 120
        with requests.Session() as session:
            session.trust_env = False
            session.verify = self._verify
            session.headers.update({"Accept": "application/json", "Content-Type": "application/json",
                                    "User-Agent": "bagofwords-dell-ome/1"})
            payload = {"UserName": self._username, "Password": self._password, "SessionType": "API"}
            response, body = self._send(session, "POST", _SESSIONS, None, deadline, json_body=payload, expected=201)
            token = response.headers.get("X-Auth-Token")
            session_id = body.get("Id") if isinstance(body, dict) else None
            if not token or not isinstance(session_id, str) or not session_id:
                raise DellOmeError("InvalidResponse", "The appliance did not return an X-Auth-Token session.")
            session.headers["X-Auth-Token"] = token
            try:
                yield session, deadline
            finally:
                # Session release is the only other non-GET request; it never hides a query failure.
                try:
                    self._send(session, "DELETE", f"{_SESSIONS}('{quote(session_id, safe='')}')", None,
                               time.monotonic() + 10, expected=204)
                except DellOmeError:
                    pass

    def _assert_allowed(self, method, path):
        if method == "GET" and any(p.match(path) for p in _allowed_get_patterns()):
            return
        if method == "POST" and path == _SESSIONS:
            return
        if method == "DELETE" and re.fullmatch(re.escape(_SESSIONS) + r"\('[^/']+'\)", path):
            return
        raise DellOmeError("ForbiddenOperation", "The request is outside the read-only allow-list; nothing was sent.")

    def _send(self, session, method, path, params, deadline, json_body=None, expected=200):
        self._assert_allowed(method, path)
        url = self.url + path + ("?" + urlencode(params) if params else "")
        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DellOmeError("BudgetExceeded", "Query deadline exhausted.")
            try:
                with session.request(method, url, json=json_body, timeout=min(self.timeout, remaining),
                                     allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if status in {429, 502, 503, 504} and attempt < 3 and method == "GET":
                        retry = response.headers.get("Retry-After", "")
                        delay = float(retry) if retry.isdigit() else 0.25 * 2**attempt
                        if delay >= deadline - time.monotonic():
                            raise DellOmeError("BudgetExceeded", "Retry exceeds query deadline.")
                        time.sleep(delay)
                        continue
                    if status != expected:
                        raise self._http_error(response, status, method)
                    payload = bytearray()
                    for chunk in response.iter_content(65536):
                        payload.extend(chunk)
                        if len(payload) > 32 * 1024 * 1024:
                            raise DellOmeError("BudgetExceeded", "Response exceeds 32 MiB.")
                        if time.monotonic() > deadline:
                            raise DellOmeError("BudgetExceeded", "Query deadline exhausted.")
                    if not payload:
                        return response, {}
                    try:
                        return response, json.loads(payload)
                    except (ValueError, UnicodeError):
                        raise DellOmeError("InvalidResponse", "The appliance did not return valid JSON.") from None
            except requests.exceptions.SSLError:
                raise DellOmeError("TLSFailed", "Certificate verification failed; configure the trusted CA certificate.") from None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 3 or method != "GET":
                    raise DellOmeError("TransportFailed", "The appliance could not be reached within the retry budget.") from None
                time.sleep(min(0.25 * 2**attempt, max(0, deadline - time.monotonic())))
        raise DellOmeError("BudgetExceeded", "Retry budget exhausted.")

    @staticmethod
    def _http_error(response, status, method):
        code = {
            400: "InvalidRequest",
            401: "AuthenticationFailed",
            403: "PermissionDenied",
            404: "UnsupportedResource",
            405: "ForbiddenOperation",
            429: "Throttled",
        }.get(status, "HttpError")
        message = f"OpenManage Enterprise returned HTTP {status}. Check the account role, the resource and the query scope."
        message_id = None
        # Preserve a bounded vendor MessageId without echoing response text.
        try:
            raw = json.loads(response.raw.read(8192))
            info = (raw.get("error") or {}).get("@Message.ExtendedInfo") or []
            candidate = info[0].get("MessageId") if info and isinstance(info[0], dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", candidate):
                message_id = candidate
        except (ValueError, AttributeError, TypeError, IndexError):
            pass
        if method == "POST" and status in (400, 401):
            if message_id == "CUSR1001":
                code, message = "SessionLimitReached", "The appliance refused a new API session because its session limit is reached; close idle sessions or raise the API session limit."
            elif status == 401:
                code, message = "AuthenticationFailed", "The appliance rejected the username or password."
        error = DellOmeError(code, message, status)
        error.ome_message_id = message_id
        return error

    def _get(self, session, path, params, deadline):
        _, body = self._send(session, "GET", path, params, deadline)
        return body

    # ------------------------------------------------------------------ query
    @staticmethod
    def _literal(value, contract):
        typ = contract["type"]
        if isinstance(value, bool):
            if typ != "boolean":
                raise DellOmeError("InvalidQuery", "Boolean filter value on a non-boolean field.")
            return "true" if value else "false"
        if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
            if typ not in ("integer", "number"):
                raise DellOmeError("InvalidQuery", "Numeric filter value on a non-numeric field.")
            return str(value)
        if isinstance(value, str):
            if typ != "string" or any(c in value for c in "\x00\r\n"):
                raise DellOmeError("InvalidQuery", "String filter value is invalid for this field.")
            if len(value) > 512:
                raise DellOmeError("InvalidQuery", "Filter value too long.")
            # OData string literal: single quotes are doubled.
            return "'" + value.replace("'", "''") + "'"
        if value is None:
            return "null"
        raise DellOmeError("InvalidQuery", "Filter values must be finite scalars.")

    def _term(self, field, condition, contract):
        odata_field = field.replace(".", "/")
        if not isinstance(condition, dict):
            return f"{odata_field} eq {self._literal(condition, contract)}"
        if set(condition) != {"op", "value"} or not isinstance(condition["op"], str):
            raise DellOmeError("InvalidQuery", "Use {\"op\": ..., \"value\": ...} for a comparison.")
        op, val = condition["op"], condition["value"]
        if op == "in":
            if not isinstance(val, list) or not 1 <= len(val) <= 50:
                raise DellOmeError("InvalidQuery", "in requires 1-50 values.")
            return "(" + " or ".join(f"{odata_field} eq {self._literal(v, contract)}" for v in val) + ")"
        if op == "contains":
            if contract["type"] != "string" or not isinstance(val, str):
                raise DellOmeError("InvalidQuery", "contains requires a string value on a string field.")
            return f"contains({odata_field},{self._literal(val, contract)})"
        if op not in {"eq", "ne", "gt", "ge", "lt", "le"}:
            raise DellOmeError("InvalidQuery", "Unknown comparison operator.")
        if op in {"gt", "ge", "lt", "le"} and val is None:
            raise DellOmeError("InvalidQuery", "Ordered comparison requires a value.")
        return f"{odata_field} {op} {self._literal(val, contract)}"

    def _parents(self, spec, resource):
        parents = spec.get("parent", {})
        if not isinstance(parents, dict):
            raise DellOmeError("InvalidQuery", "parent must be an object.")
        required = set(resource["parents"])
        optional = set(resource["optional_parents"])
        if set(parents) - required - optional or required - set(parents):
            raise DellOmeError("InvalidQuery", "Supply exactly the required parent identifiers from get_schema.")
        path, collection = resource["path"], resource["collection"]
        chosen = [k for k in parents if k in optional]
        if len(chosen) > 1:
            raise DellOmeError("InvalidQuery", "Only one optional parent is supported per query.")
        if chosen:
            opt = resource["optional_parents"][chosen[0]]
            path, collection = opt["path"], opt["collection"]
        bound = {}
        for key, value in parents.items():
            contract = resource["parents"].get(key) or resource["optional_parents"][key]
            if contract["type"] == "integer":
                if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]{1,18}", str(value)):
                    raise DellOmeError("InvalidQuery", f"Parent {key} must be a non-negative integer identifier.")
                value = int(value)
                if value < int(contract.get("minimum", 0)):
                    raise DellOmeError("InvalidQuery", f"Parent {key} is outside its declared range.")
                bound[key] = value
                path = path.replace("{" + key + "}", str(value))
            else:
                if not isinstance(value, str) or not re.fullmatch(contract.get("pattern", r"[A-Za-z0-9_.-]{1,64}"), value):
                    raise DellOmeError("InvalidQuery", f"Parent {key} is not a valid identifier.")
                bound[key] = value
                path = path.replace("{" + key + "}", quote(value, safe=""))
        if "{" in path or "}" in path:
            raise DellOmeError("InvalidQuery", "Unresolved resource path; no request was sent.")
        return path, collection, bound

    def execute_query(self, query=None, **kwargs):
        if query is None and kwargs:
            query = kwargs
        try:
            spec = json.loads(query) if isinstance(query, str) else query.copy() if isinstance(query, dict) else None
        except (ValueError, TypeError):
            raise DellOmeError("InvalidQuery", "Query must be a JSON object.") from None
        allowed = {"table", "fields", "filter", "parent", "order_by", "limit", "lookback"}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise DellOmeError("InvalidQuery", "Unknown query controls; use table, fields, filter, parent, order_by, limit, lookback.")
        table = spec.get("table")
        resource = self._resource(table)
        limit = self._integer(spec.get("limit", self.max_rows), "limit", 1, self.max_rows)
        path, collection, parents = self._parents(spec, resource)
        fields = spec.get("fields", list(resource["fields"]))
        if not isinstance(fields, list) or not fields or not all(isinstance(x, str) for x in fields):
            raise DellOmeError("InvalidQuery", "fields must be a nonempty list of documented field names.")
        expanded = []
        for name in fields:
            matches = [k for k in resource["fields"] if k == name or k.startswith(name + ".")]
            if not matches:
                raise DellOmeError("InvalidQuery", f"Unknown or excluded field: {name}.")
            expanded.extend(matches)
        fields = list(dict.fromkeys(expanded + resource["identity"]))
        filters = spec.get("filter", {})
        if not isinstance(filters, dict):
            raise DellOmeError("InvalidQuery", "filter must be an object.")
        terms = []
        for key, condition in filters.items():
            if key not in resource["filters"] or "$filter" not in resource["controls"]:
                raise DellOmeError("InvalidQuery", f"Unknown or unsupported filter: {key}.")
            terms.append(self._term(key, condition, resource["filters"][key]))
        now = datetime.now(UTC)
        if "lookback" in spec:
            field = _TIME_FILTER.get(table)
            if field is None or field in filters:
                raise DellOmeError("InvalidQuery", "lookback is only supported on alerts and audit_logs without an explicit time filter.")
            match = re.fullmatch(r"([1-9][0-9]{0,4})(m|h|d|w)", str(spec["lookback"]))
            if not match:
                raise DellOmeError("InvalidQuery", "lookback must be a positive number followed by m, h, d or w.")
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
            if seconds > 366 * 86400:
                raise DellOmeError("InvalidQuery", "lookback cannot exceed one year.")
            start = (now - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S.000")
            terms.append(f"{field} ge '{start}'")
        params = {}
        if terms:
            params["$filter"] = " and ".join(terms)
        if "order_by" in spec:
            if "$orderby" not in resource["controls"]:
                raise DellOmeError("InvalidQuery", "This table does not support server-side ordering; sort in pandas.")
            order = spec["order_by"]
            if not isinstance(order, list) or not order or not all(isinstance(t, str) for t in order):
                raise DellOmeError("InvalidQuery", "order_by must be a list of 'Field asc|desc' strings.")
            rendered = []
            for term in order:
                parts = term.split()
                if not 1 <= len(parts) <= 2 or parts[0] not in resource["fields"] or (len(parts) == 2 and parts[1] not in {"asc", "desc"}):
                    raise DellOmeError("InvalidQuery", "Invalid sort field or direction.")
                if resource["fields"][parts[0]]["type"] in ("array", "object"):
                    raise DellOmeError("InvalidQuery", "Cannot order by a structured field.")
                rendered.append(" ".join([parts[0].replace(".", "/")] + parts[1:]))
            params["$orderby"] = ",".join(rendered)
        if collection and "$top" in resource["controls"]:
            params["$top"] = min(_PAGE, limit)
            params["$skip"] = 0
        query_id = str(uuid.uuid4())
        with self._lock:
            try:
                with self._session() as (session, deadline):
                    records = self._pages(session, path, params, limit, deadline, collection)
                self._availability[table] = "available" if records else "empty"
            except DellOmeError as e:
                self._availability[table] = {401: "authentication_failed", 403: "permission_denied", 404: "unsupported"}.get(e.status, e.code)
                raise
        records = self._explode(records, resource["explode"])
        if len(records) > limit:
            raise DellOmeError("ResultLimitExceeded", "Narrow filters or increase the complete-result limit; nothing was truncated.")
        rows = []
        for record in records:
            row = {k: self._extract(record, k) for k in fields}
            row.update(parents)
            row.update(
                _bow_appliance=self._appliance,
                _bow_connection=getattr(self, "_bow_connection_id", None),
                _bow_retrieved_at=now.isoformat(),
                _bow_query_id=query_id,
                _bow_endpoint=resource["path"],
                _bow_contract=_catalog()["sha256"],
            )
            rows.append(row)
        columns = list(dict.fromkeys(fields + list(parents) + list(_META)))
        frame = pd.DataFrame({k: pd.Series([row.get(k) for row in rows], dtype="object") for k in columns})
        for k in fields:
            typ = resource["fields"][k]["type"]
            try:
                if typ == "integer":
                    frame[k] = pd.array(frame[k], dtype="Int64")
                elif typ == "number":
                    frame[k] = pd.array(frame[k], dtype="Float64")
                elif typ == "boolean":
                    frame[k] = pd.array(frame[k], dtype="boolean")
                elif typ == "string":
                    frame[k] = pd.array([None if v is None else str(v) for v in frame[k]], dtype="string")
            except (ValueError, TypeError, OverflowError):
                raise DellOmeError("InvalidResponse", f"Invalid value type for {k}.") from None
        for k, contract in parents.items():
            if isinstance(contract, int):
                frame[k] = pd.array(frame[k], dtype="Int64")
        frame["_bow_retrieved_at"] = pd.to_datetime(frame["_bow_retrieved_at"], utc=True)
        return frame

    @staticmethod
    def _explode(records, explode):
        if not explode:
            return records
        out = []
        key = explode.get("as") or explode["prefix"].rstrip(".")
        for record in records:
            items = record.get(explode["array"])
            if items is None:
                items = []
            if not isinstance(items, list):
                raise DellOmeError("InvalidResponse", "Expected an array to flatten.")
            base = {k: v for k, v in record.items() if k != explode["array"]}
            if not items:
                out.append(base)
                continue
            for item in items:
                if not isinstance(item, dict):
                    raise DellOmeError("InvalidResponse", "Expected object items to flatten.")
                out.append({**base, key: item})
        return out

    @staticmethod
    def _extract(record, dotted):
        current = record
        for part in dotted.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return DellOmeClient._sanitize(current)

    @staticmethod
    def _sanitize(value):
        if isinstance(value, dict):
            if set(value) >= {"Key", "Value"} and isinstance(value.get("Key"), str) and _SECRET.search(value["Key"]):
                return {k: (None if k == "Value" else DellOmeClient._sanitize(v)) for k, v in value.items() if not k.startswith("@odata")}
            return {
                k: DellOmeClient._sanitize(v)
                for k, v in value.items()
                if not k.startswith("@odata") and "@odata" not in k and not _SECRET.search(k)
            }
        if isinstance(value, list):
            return [DellOmeClient._sanitize(v) for v in value]
        return value

    def _pages(self, session, path, params, limit, deadline, collection):
        current_params = dict(params)
        seen = set()
        records = []
        last_skip = -1
        for _ in range(1000):
            marker = path + "?" + urlencode(current_params)
            if marker in seen:
                raise DellOmeError("InvalidResponse", "Repeated pagination continuation.")
            seen.add(marker)
            body = self._get(session, path, current_params, deadline)
            if not isinstance(body, dict) or body.get("error"):
                raise DellOmeError("InvalidResponse", "The appliance returned an incomplete or invalid response.")
            if not collection:
                return [body]
            batch = body.get("value")
            if not isinstance(batch, list) or not all(isinstance(r, dict) for r in batch):
                raise DellOmeError("InvalidResponse", "Expected an OData collection with a value array.")
            count = body.get("@odata.count")
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise DellOmeError("InvalidResponse", "Invalid @odata.count.")
            if count is not None and count > limit:
                raise DellOmeError("ResultLimitExceeded", "Narrow filters or increase the complete-result limit; nothing was truncated.")
            records.extend(batch)
            if len(records) > limit:
                raise DellOmeError("ResultLimitExceeded", "Narrow filters or increase the complete-result limit; nothing was truncated.")
            nxt = body.get("@odata.nextLink")
            if nxt is None or nxt == "":
                return records
            if not isinstance(nxt, str) or not batch:
                raise DellOmeError("InvalidResponse", "Invalid pagination continuation.")
            next_url = urljoin(self.url + path, nxt)
            parsed = urlsplit(next_url)
            origin = urlsplit(self.url)
            if parsed.scheme != origin.scheme or parsed.netloc != origin.netloc or parsed.path != path or parsed.fragment:
                raise DellOmeError("InvalidResponse", "Continuation escaped the selected resource.")
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            next_params = dict(pairs)
            if len(next_params) != len(pairs) or any(k not in params and k not in {"$skip", "$top"} for k in next_params):
                raise DellOmeError("InvalidResponse", "Continuation introduced unsupported query controls.")
            # OME continuations may drop $filter/$orderby; re-apply our scope and refuse changes.
            for key, val in params.items():
                if key in {"$skip", "$top"}:
                    continue
                if key in next_params and next_params[key] != str(val):
                    raise DellOmeError("InvalidResponse", "Continuation changed query scope.")
                next_params[key] = str(val)
            if not re.fullmatch(r"[0-9]{1,9}", next_params.get("$skip", "")) or not re.fullmatch(r"[1-9][0-9]{0,6}", next_params.get("$top", "")):
                raise DellOmeError("InvalidResponse", "Continuation lacks a valid $skip/$top window.")
            skip = int(next_params["$skip"])
            if skip <= last_skip or skip < len(records):
                raise DellOmeError("InvalidResponse", "Continuation did not advance.")
            last_skip = skip
            current_params = next_params
        raise DellOmeError("BudgetExceeded", "Pagination budget exhausted.")
