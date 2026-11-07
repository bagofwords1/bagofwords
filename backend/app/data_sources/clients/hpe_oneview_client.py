"""Read-only HPE OneView / Synergy Composer diagnostic tables.

Catalog reference: OneView 9.00 (API 6800); negotiated X-API-Version is clamped
to the catalog's supported range. All network I/O is GET, except the documented
session handshake (POST/DELETE /rest/login-sessions). The frozen catalog is an
execution allow-list, not a claim of availability on a particular appliance.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_progress

_LIMIT_MSG = "Result exceeds limit, which is a complete-result ceiling rather than a preview: raise limit (up to max_rows) or narrow filters, then preview with pandas .head(n). Nothing was truncated."

class OneViewQueryError(ValueError):
    """Stable machine code plus a safe explanation; never echoes bodies or credentials."""

    def __init__(self, code, message, status=None):
        self.code, self.status = code, status
        self.oneview_code = None
        super().__init__(f"{code}: {message}")


@lru_cache(maxsize=1)
def _catalog():
    catalog = json.loads(Path(__file__).with_name("hpe_oneview").joinpath("catalog.json").read_text())
    for name, resource in catalog["resources"].items():
        placeholders = set(re.findall(r"\{([^}]+)\}", resource["path"]))
        if placeholders != set(resource["parents"]) or len(resource["parents"]) != len(set(resource["parents"])):
            raise OneViewQueryError("InvalidCatalog", f"Invalid parent contract for {name}.")
        if resource.get("parent_path") and set(re.findall(r"\{([^}]+)\}", resource["parent_path"])) != set(
            resource.get("optional_parents", [])
        ):
            raise OneViewQueryError("InvalidCatalog", f"Invalid optional parent contract for {name}.")
        if not resource["path"].startswith("/rest/") or ".." in resource["path"]:
            raise OneViewQueryError("InvalidCatalog", f"Malformed path for {name}.")
        unknown = set(resource["default_fields"]) - set(resource["fields"])
        if unknown:
            raise OneViewQueryError("InvalidCatalog", f"Unknown default fields for {name}: {sorted(unknown)}.")
    return catalog


_TYPES = {"integer": "int", "number": "float", "boolean": "bool", "string": "str", "array": "json", "object": "json"}
_META = {
    "_bow_appliance": "str",
    "_bow_connection": "str",
    "_bow_retrieved_at": "datetime",
    "_bow_query_id": "str",
    "_bow_endpoint": "str",
    "_bow_api_version": "int",
    "_bow_contract": "str",
}
# Column -> referenced table for cross-table joins on canonical URIs.
_URI_REFS = {
    "serverProfileUri": "server_profiles",
    "serverHardwareUri": "server_hardware",
    "serverHardwareTypeUri": "server_hardware_types",
    "serverProfileTemplateUri": "server_profile_templates",
    "locationUri": "enclosures",
    "enclosureUri": "enclosures",
    "enclosureGroupUri": "enclosure_groups",
    "logicalEnclosureUri": "logical_enclosures",
    "logicalInterconnectUri": "logical_interconnects",
    "interconnectUri": "interconnects",
    "storageSystemUri": "storage_systems",
    "storagePoolUri": "storage_pools",
    "firmwareBaselineUri": "firmware_drivers",
    "fwBaselineUri": "firmware_drivers",
    "port.associatedUplinkSetUri": "uplink_sets",
}
_SECRET_KEY = re.compile(
    r"password|passphrase|secret|private_?key|access_?key|auth_?key|token|session_?id|community|credential",
    re.I,
)
_FILTER_OPS = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<=", "like": "matches"}
_LOGIN_PATH = "/rest/login-sessions"
_VERSION_PATH = "/rest/version"
_PAGE_SIZE = 100


class HpeOneViewClient(DataSourceClient):
    capture_structured_queries = True
    relative_date_hint = (
        'Use JSON lookback on alerts/events/tasks/server_hardware_utilization, e.g. "lookback":"24h"; '
        "it is resolved at each execution. Do not freeze dates in saved investigations."
    )

    def __init__(
        self,
        url,
        username,
        password,
        auth_login_domain="local",
        api_version=None,
        ca_certificate=None,
        timeout=30,
        max_rows=10000,
        allow_http=False,
        **kwargs,
    ):
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
            raise OneViewQueryError(
                "InvalidConfiguration",
                "Use the appliance origin such as https://oneview.example:443, without a path or credentials.",
            )
        if parsed.scheme != "https":
            if not allow_http:
                raise OneViewQueryError(
                    "InvalidConfiguration", "HTTPS is required. HTTP is only available through the simulator option."
                )
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise OneViewQueryError("InvalidConfiguration", "HTTP is only allowed towards a loopback simulator.")
        if not username or not password:
            raise OneViewQueryError("InvalidConfiguration", "A read-only OneView account is required.")
        if not isinstance(auth_login_domain, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", auth_login_domain):
            raise OneViewQueryError("InvalidConfiguration", "authLoginDomain must be a directory name such as local.")
        contract = _catalog()["contract"]
        # The connect form stores an empty optional field as "" — treat it (and a
        # numeric string) like "negotiate from GET /rest/version".
        if isinstance(api_version, str):
            api_version = int(api_version) if re.fullmatch(r"\s*[0-9]{1,6}\s*", api_version) else None if not api_version.strip() else api_version
        if api_version is not None:
            api_version = self._integer(api_version, "api_version", contract["min_api_version"], contract["max_api_version"])
        self.url = url
        self.timeout = self._integer(timeout, "timeout", 1, 120)
        self.max_rows = self._integer(max_rows, "max_rows", 1, 100000)
        self._username, self._password, self._domain = username, password, auth_login_domain
        self._pinned_version = api_version
        self._api_version = api_version
        self._ca = None
        self._verify = True
        if ca_certificate:
            self._ca = tempfile.TemporaryDirectory(prefix="bow-oneview-ca-")
            cert = Path(self._ca.name) / "ca.pem"
            cert.write_text(ca_certificate)
            cert.chmod(0o600)
            self._verify = str(cert)
        self._lock = threading.RLock()
        self._availability = {}
        self._appliance = parsed.hostname
        self._software_version = None

    def close(self):
        if self._ca:
            self._ca.cleanup()
            self._ca = None

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise OneViewQueryError("InvalidQuery", f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    # ------------------------------------------------------------------ description / schema
    @property
    def description(self):
        return """HPE OneView / Synergy Composer read-only investigation connector (REST catalog reference OneView 9.00, API 6800; the X-API-Version is negotiated from GET /rest/version and clamped to the catalog range).
Call get_tables()/get_schema(name) to discover tables, then execute_query(query) with a JSON object or string.
Tables: appliance_version, appliance_health, server_hardware, server_hardware_firmware, server_hardware_utilization, server_hardware_types, server_profiles, server_profile_templates, enclosures, enclosure_groups, logical_enclosures, interconnects, interconnect_ports, logical_interconnects, sas_interconnects, uplink_sets, ethernet_networks, fc_networks, fcoe_networks, network_sets, racks, datacenters, power_devices, alerts, events, tasks, firmware_drivers, storage_systems, storage_pools, storage_volumes, scopes, index_resources.
Examples:
 {"table":"server_hardware","fields":["uri","name","status","powerState","serverProfileUri"],"filter":{"status":"Critical"},"limit":500}
 {"table":"alerts","filter":{"alertState":"Active","severity":{"op":"in","value":["Critical","Warning"]}},"lookback":"24h","sort":["created desc"]}
 {"table":"server_profiles","filter":{"templateCompliance":"NonCompliant"}}
 {"table":"tasks","filter":{"taskState":"Error"},"lookback":"7d"}
 {"table":"server_hardware_utilization","parent":{"id":"<server id from uri>"},"metrics":["AveragePower","AmbientTemperature"],"lookback":"24h","view":"hour"}
 {"table":"server_hardware_firmware","parent":{"id":"<server id>"}}  (omit parent to read every server's components)
 {"table":"interconnect_ports","filter":{"enclosureName":"Frame-1"}}
 {"table":"index_resources","category":["server-hardware","enclosures"],"user_query":"CZ12345"}
Filters: scalar means field='value' (exact, OneView filter syntax); explicit comparisons use {"op":"gt","value":10} with eq,ne,gt,ge,lt,le,like (OneView 'matches', % wildcard) and in (expanded into one filter per value on the same request, OR semantics via the appliance's filter list); only documented filter fields are sent. lookback/start_time/end_time map to created>=/<= on alerts, events and tasks.
fields may be "*" for every catalog column; omitted fields select a compact identity/health projection. sort is a list of "field asc|desc". limit is a complete-result ceiling: the query fails with ResultLimitExceeded instead of silently truncating, so narrow filters.
Results are pandas DataFrames with camelCase dotted columns for nested scalars (mpHostInfo.mpHostName, associatedResource.resourceName), structured arrays/objects kept as JSON, nullable dtypes and _bow_* provenance (appliance, api version, catalog contract, retrieval time).
Join in pandas on URIs: server_hardware.serverProfileUri -> server_profiles.uri, server_hardware.locationUri -> enclosures.uri, interconnects.logicalInterconnectUri -> logical_interconnects.uri, alerts.resourceUri -> any uri. Resource ids for parent queries are the last path segment of uri.
No SQL, raw URLs, POST/PUT/PATCH/DELETE, power actions or alert changes: the client cannot send a mutating request. Current inventory does not prove historical placement; alerts and events are observations, not causes; missing utilization samples are gaps, not zeros; templateCompliance is OneView's own comparison and firmware baseline versions must be compared explicitly.
Simulator-verified contract; customer appliance validation (release, permissions, scopes) remains required."""

    def get_tables(self):
        names = list(_catalog()["resources"])
        return [self.get_schema(n) for n in discovery_items(names, "tables", label=str)]

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    @staticmethod
    def _resource(name):
        if not isinstance(name, str) or name not in _catalog()["resources"]:
            raise OneViewQueryError("InvalidQuery", "Unknown table; use get_tables to discover supported resources.")
        return _catalog()["resources"][name]

    def get_schema(self, table_name):
        r = self._resource(table_name)
        columns = [
            TableColumn(name=k, dtype=_TYPES.get(v["type"], "json"), description=v.get("description"))
            for k, v in r["fields"].items()
        ]
        names = {c.name for c in columns}
        for key in r["parents"] + r.get("optional_parents", []):
            if key not in names:
                columns.append(TableColumn(name=key, dtype="str", description="Parent identity used for this query."))
        columns += [TableColumn(name=k, dtype=v) for k, v in _META.items()]
        meta = {
            "endpoint": r["path"],
            "kind": r["kind"],
            "parents": r["parents"],
            "optional_parents": r.get("optional_parents", []),
            "filters": r["filters"],
            "sortable": r["sortable"],
            "time_field": r.get("time_field"),
            "metrics": r.get("metrics"),
            "views": r.get("views"),
            "default_fields": r["default_fields"],
            "availability": self._availability.get(table_name, "unverified"),
            "contract": _catalog()["contract"]["reference_api_version"],
        }
        desc = f"{r['description']} GET {r['path']}. "
        if r["parents"]:
            desc += "Required parent: " + ", ".join(r["parents"]) + ". "
        if r.get("optional_parents"):
            desc += "Optional parent: " + ", ".join(r["optional_parents"]) + ". "
        if r["filters"]:
            desc += "Server-side filters: " + ", ".join(r["filters"]) + ". "
        if r.get("time_field"):
            desc += "Supports lookback/start_time/end_time on created. "
        desc += "Availability is unverified until queried."
        return Table(
            name=table_name,
            description=desc,
            columns=columns,
            pks=[c for c in columns if c.name == "uri"],
            fks=[
                ForeignKey(column=c, references_name=_URI_REFS[c.name], references_column=TableColumn(name="uri", dtype="str"))
                for c in columns
                if c.name in _URI_REFS and _URI_REFS[c.name] != table_name
            ],
            metadata_json={"hpe_oneview": meta},
        )

    def prompt_schema(self):
        return "\n".join(
            f"{n}: {r['description'][:150]} | parents={','.join(r['parents']) or 'none'}"
            f"{' (optional ' + ','.join(r['optional_parents']) + ')' if r.get('optional_parents') else ''}"
            for n, r in _catalog()["resources"].items()
        )

    def coverage_report(self):
        c = _catalog()
        return {
            "contract": c["contract"],
            "sha256": c["sha256"],
            "validation": "simulator_verified_awaiting_customer_validation",
            "resources": [
                {"table": n, "path": r["path"], "availability": self._availability.get(n, "unverified")}
                for n, r in c["resources"].items()
            ],
            "gaps": [
                "Users, roles, certificates, backups and audit logs are excluded (sensitive or non-diagnostic).",
                "Utilization retention and resolution depend on the appliance; missing samples are not zeros.",
                "Field availability varies with the negotiated API version; absent fields are null.",
            ],
        }

    # ------------------------------------------------------------------ connection / session
    def test_connection(self):
        with self._session() as session:
            body = self._request(session, "GET", "/rest/appliance/nodeinfo/version", time.monotonic() + self.timeout * 2)
            if not isinstance(body, dict) or not isinstance(body.get("softwareVersion"), str) or not body["softwareVersion"]:
                raise OneViewQueryError("InvalidResponse", "Appliance version information is absent.")
            self._software_version = body["softwareVersion"]
            serial = body.get("serialNumber")
            if isinstance(serial, str) and serial.strip():
                self._appliance = serial.strip()
            model = body.get("modelNumber") if isinstance(body.get("modelNumber"), str) else "HPE OneView"
        return {
            "success": True,
            "message": (
                f"Connected to {model} {self._software_version} (X-API-Version {self._api_version}, catalog reference "
                f"{_catalog()['contract']['reference_api_version']}). {len(_catalog()['resources'])} read-only tables available."
            ),
        }

    def _negotiate_version(self, session, deadline):
        contract = _catalog()["contract"]
        body = self._request(session, "GET", _VERSION_PATH, deadline, versioned=False)
        current, minimum = (body or {}).get("currentVersion"), (body or {}).get("minimumVersion")
        if not isinstance(current, int) or isinstance(current, bool):
            raise OneViewQueryError("InvalidResponse", "The appliance did not report an API version.")
        if not isinstance(minimum, int) or isinstance(minimum, bool):
            minimum = current
        if self._pinned_version is not None:
            if not minimum <= self._pinned_version <= current:
                raise OneViewQueryError(
                    "UnsupportedVersion",
                    f"Pinned X-API-Version {self._pinned_version} is outside the appliance range {minimum}-{current}.",
                )
            return self._pinned_version
        negotiated = min(current, contract["max_api_version"])
        if negotiated < max(minimum, contract["min_api_version"]):
            raise OneViewQueryError(
                "UnsupportedVersion",
                f"Appliance API range {minimum}-{current} does not overlap the catalog range "
                f"{contract['min_api_version']}-{contract['max_api_version']}.",
            )
        return negotiated

    @contextmanager
    def _session(self):
        with self._lock:
            deadline = time.monotonic() + 120
            with requests.Session() as session:
                session.trust_env = False
                session.verify = self._verify
                session.headers.update({"Accept": "application/json", "User-Agent": "bagofwords-hpe-oneview/1"})
                self._api_version = self._negotiate_version(session, deadline)
                session.headers["X-API-Version"] = str(self._api_version)
                body = self._request(
                    session,
                    "POST",
                    _LOGIN_PATH,
                    deadline,
                    payload={
                        "userName": self._username,
                        "password": self._password,
                        "authLoginDomain": self._domain,
                        "loginMsgAck": True,
                    },
                )
                token = (body or {}).get("sessionID") if isinstance(body, dict) else None
                if not isinstance(token, str) or not token:
                    raise OneViewQueryError("InvalidResponse", "The appliance did not return a session identifier.")
                # OneView reads the session token from the literal `Auth` header (not Authorization).
                session.headers["Auth"] = token
                try:
                    yield session
                finally:
                    with suppress(OneViewQueryError):
                        self._request(session, "DELETE", _LOGIN_PATH, time.monotonic() + 10)
                    session.headers.pop("Auth", None)

    def _request(self, session, method, path, deadline, payload=None, versioned=True):
        """The ONLY network primitive. GET is confined to catalog paths; POST/DELETE to the login session."""
        allowed_get = {r["path"].split("{")[0].rstrip("/*") for r in _catalog()["resources"].values()} | {
            _VERSION_PATH,
            "/rest/appliance/nodeinfo/version",
        }
        allowed_get |= {r["parent_path"].split("{")[0].rstrip("/") for r in _catalog()["resources"].values() if r.get("parent_path")}
        bare = path.split("?", 1)[0]
        if method == "GET":
            if ".." in bare or not any(bare == p or bare.startswith(p + "/") for p in allowed_get):
                raise OneViewQueryError("ForbiddenOperation", "The request is outside the read-only allow-list.")
        elif method in {"POST", "DELETE"} and bare == _LOGIN_PATH:
            pass
        else:
            raise OneViewQueryError("ForbiddenOperation", "Only read-only requests are permitted.")
        headers = {}
        if not versioned:
            headers["X-API-Version"] = None  # /rest/version is served without a version header.
        if payload is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OneViewQueryError("BudgetExceeded", "Query deadline exhausted.")
            try:
                with session.request(
                    method,
                    self.url + path,
                    data=json.dumps(payload) if payload is not None else None,
                    headers=headers,
                    timeout=min(self.timeout, remaining),
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    status = response.status_code
                    if status in {429, 502, 503, 504} and method == "GET" and attempt < 3:
                        retry = response.headers.get("Retry-After", "")
                        delay = float(retry) if retry.isdigit() else 0.25 * 2**attempt
                        if delay >= deadline - time.monotonic():
                            raise OneViewQueryError("BudgetExceeded", "Retry exceeds query deadline.")
                        time.sleep(delay)
                        continue
                    if status == 204 and method == "DELETE":
                        return None
                    if status != 200:
                        code = {
                            400: "InvalidRequest",
                            401: "AuthenticationFailed",
                            403: "PermissionDenied",
                            404: "UnsupportedResource",
                            405: "ForbiddenOperation",
                            412: "UnsupportedVersion",
                            429: "Throttled",
                        }.get(status, "HttpError")
                        if 300 <= status < 400:
                            code = "HttpError"
                        if bare == _LOGIN_PATH and status in {400, 401}:
                            code = "AuthenticationFailed"
                        error = OneViewQueryError(
                            code, f"OneView returned HTTP {status} for {method} {bare}. Check permissions, version and scope.", status
                        )
                        # Keep only the bounded vendor errorCode; never the message/details (may echo input).
                        try:
                            vendor = json.loads(response.raw.read(8192)).get("errorCode")
                            if isinstance(vendor, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", vendor):
                                error.oneview_code = vendor
                        except (ValueError, AttributeError, TypeError):
                            pass
                        raise error
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 32 * 1024 * 1024:
                            raise OneViewQueryError("BudgetExceeded", "Response exceeds 32 MiB.")
                        if time.monotonic() > deadline:
                            raise OneViewQueryError("BudgetExceeded", "Query deadline exhausted.")
                    if not body:
                        return None
                    try:
                        return json.loads(body)
                    except (ValueError, UnicodeError):
                        raise OneViewQueryError("InvalidResponse", "OneView did not return valid JSON.") from None
            except requests.exceptions.SSLError:
                raise OneViewQueryError("TLSFailed", "Certificate verification failed; configure the trusted CA.") from None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 3 or method != "GET":
                    raise OneViewQueryError("TransportFailed", "OneView could not be reached within the retry budget.") from None
                time.sleep(min(0.25 * 2**attempt, max(0, deadline - time.monotonic())))
        raise OneViewQueryError("BudgetExceeded", "Retry budget exhausted.")

    # ------------------------------------------------------------------ query validation helpers
    @staticmethod
    def _expand(requested, resource):
        if requested == "*" or requested == ["*"]:
            return list(resource["fields"])
        if not isinstance(requested, list) or not requested or not all(isinstance(x, str) for x in requested):
            raise OneViewQueryError("InvalidQuery", 'fields must be a nonempty list of documented field names or "*".')
        expanded = []
        for name in requested:
            if name in _META:
                continue  # provenance columns are always appended
            matches = [k for k in resource["fields"] if k == name or k.startswith(name + ".")]
            if not matches:
                raise OneViewQueryError("InvalidQuery", f"Unknown or excluded field: {name}.")
            expanded.extend(matches)
        return list(dict.fromkeys(expanded))

    @staticmethod
    def _literal(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return repr(value) if isinstance(value, float) else str(value)
        if not isinstance(value, str):
            raise OneViewQueryError("InvalidQuery", "Filter values must be finite scalars.")
        # OneView filter literals are single-quoted and the API does not support an embedded single quote.
        if any(c in value for c in "'\"\\\x00\n\r") or len(value) > 512:
            raise OneViewQueryError("InvalidQuery", "Filter values cannot contain quotes, backslashes or control characters.")
        return "'" + value + "'"

    @classmethod
    def _local_match(cls, actual, spec):
        """Client-side equivalent of the server-side filter vocabulary."""
        if isinstance(spec, dict):
            op, value = spec["op"], spec["value"]
        else:
            op, value = "eq", spec
        if op == "in":
            return any(cls._local_match(actual, x) for x in value)
        if actual is None:
            return False
        if op == "eq":
            return str(actual) == str(value) or actual == value
        if op == "ne":
            return not (str(actual) == str(value) or actual == value)
        if op == "like":
            pattern = "^" + ".*".join(re.escape(part) for part in str(value).split("%")) + "$"
            return re.match(pattern, str(actual), re.IGNORECASE) is not None
        try:
            left, right = float(actual), float(value)
        except (TypeError, ValueError):
            left, right = str(actual), str(value)
        return {"gt": left > right, "ge": left >= right, "lt": left < right, "le": left <= right}[op]

    def _filter_terms(self, field, value):
        """Return the list of OneView filter expressions for one field (several for `in`)."""
        if not isinstance(value, dict):
            return [f"{field}={self._literal(value)}"]
        if set(value) != {"op", "value"} or not isinstance(value["op"], str):
            raise OneViewQueryError("InvalidQuery", "Comparisons use {op, value}.")
        op, val = value["op"], value["value"]
        if op == "in":
            if not isinstance(val, list) or not 1 <= len(val) <= 50:
                raise OneViewQueryError("InvalidQuery", "in requires 1-50 values.")
            return [" OR ".join(f"{field}={self._literal(x)}" for x in val)]
        if op not in _FILTER_OPS:
            raise OneViewQueryError("InvalidQuery", "Unknown comparison operator.")
        if op == "like" and not isinstance(val, str):
            raise OneViewQueryError("InvalidQuery", "like requires a string pattern using % wildcards.")
        return [f"{field} {_FILTER_OPS[op]} {self._literal(val)}" if op == "like" else f"{field}{_FILTER_OPS[op]}{self._literal(val)}"]

    @staticmethod
    def _date(value):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError()
            return dt.astimezone(UTC)
        except (ValueError, AttributeError, TypeError):
            raise OneViewQueryError("InvalidQuery", "Times require an ISO-8601 timestamp with timezone.") from None

    @staticmethod
    def _iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _time_window(self, spec, now, required):
        keys = {"lookback", "start_time", "end_time"} & set(spec)
        if not keys:
            if required:
                return now - timedelta(hours=24), now
            return None
        if "lookback" in spec:
            if "start_time" in spec or "end_time" in spec:
                raise OneViewQueryError("InvalidQuery", "Use relative or absolute time, not both.")
            match = re.fullmatch(r"([1-9][0-9]*)(m|h|d|w)", str(spec["lookback"]))
            if not match:
                raise OneViewQueryError("InvalidQuery", "lookback must be a positive number followed by m, h, d or w.")
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
            if seconds > 3 * 31536000:
                raise OneViewQueryError("InvalidQuery", "lookback exceeds three years.")
            start, end = now - timedelta(seconds=seconds), now
        else:
            if "start_time" not in spec:
                raise OneViewQueryError("InvalidQuery", "start_time is required with end_time.")
            start = self._date(spec["start_time"])
            # A future end_time is clamped to now: planners write open-ended windows.
            end = min(self._date(spec["end_time"]), now) if "end_time" in spec else now
        if start >= end:
            raise OneViewQueryError("InvalidQuery", "Time range must be ordered and start before now.")
        return start, end

    @staticmethod
    def _parent_value(value, key):
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or not re.fullmatch(r"[A-Za-z0-9._:-]+", value)
            or value in {".", ".."}
        ):
            raise OneViewQueryError("InvalidQuery", f"Parent {key} must be a resource id (the last segment of its uri).")
        return value

    # ------------------------------------------------------------------ execute
    def execute_query(self, query):
        try:
            spec = json.loads(query) if isinstance(query, str) else dict(query) if isinstance(query, dict) else None
        except (ValueError, TypeError):
            raise OneViewQueryError("InvalidQuery", "Query must be a JSON object.") from None
        allowed = {
            "table", "fields", "filter", "parent", "sort", "limit",
            "lookback", "start_time", "end_time",
            "metrics", "view",  # utilization
            "category", "user_query",  # index
        }
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise OneViewQueryError("InvalidQuery", "Unknown query controls.")
        resource = self._resource(spec.get("table"))
        kind = resource["kind"]
        limit = self._integer(spec.get("limit", self.max_rows), "limit", 1, self.max_rows)
        fields = self._expand(spec["fields"] if "fields" in spec else resource["default_fields"], resource)
        # Always carry identity columns that other tables join on.
        fields = list(dict.fromkeys(fields + [k for k in ("uri",) if k in resource["fields"]]))

        parents = spec.get("parent", {})
        if not isinstance(parents, dict) or not all(isinstance(k, str) for k in parents):
            raise OneViewQueryError("InvalidQuery", "parent must be an object of identifiers.")
        required, optional = set(resource["parents"]), set(resource.get("optional_parents", []))
        if set(parents) - required - optional or required - set(parents):
            raise OneViewQueryError("InvalidQuery", "Supply exactly the parent parameters from get_schema.")
        path = resource["parent_path"] if parents and optional and set(parents) == optional else resource["path"]
        for k, v in parents.items():
            path = path.replace("{" + k + "}", quote(self._parent_value(v, k), safe=""))
        if "{" in path or "}" in path:
            raise OneViewQueryError("InvalidQuery", "Unresolved resource path; no request was sent.")
        singleton = kind in {"singleton", "singleton_members", "utilization"} or (parents and optional and kind == "collection_flatten")

        params = []
        filters = spec.get("filter", {})
        if not isinstance(filters, dict):
            raise OneViewQueryError("InvalidQuery", "filter must be an object.")
        if filters and singleton:
            raise OneViewQueryError("InvalidQuery", "This query shape does not support server-side filters.")
        local_filters = {}
        for k, v in filters.items():
            if k in resource["filters"]:
                for term in self._filter_terms(k, v):
                    params.append(("filter", '"' + term + '"'))
            elif k in resource["fields"]:
                # OneView only filters server-side on indexed attributes; any other
                # catalogued column (uri, interconnectUri, ...) is filtered here after
                # the complete fetch, with the same comparison vocabulary.
                self._filter_terms(k, v)  # validates the value shape before any I/O
                local_filters[k] = v
            else:
                raise OneViewQueryError(
                    "InvalidQuery", f"Unknown or excluded filter: {k}. Use get_schema(table) for the documented columns."
                )

        now = datetime.now(UTC)
        for control in ("lookback", "start_time", "end_time"):
            if control in spec and not resource.get("time_field") and kind != "utilization":
                raise OneViewQueryError("InvalidQuery", "This table does not support time controls.")
        if resource.get("time_field"):
            window = self._time_window(spec, now, required=False)
            if window:
                if resource["time_field"] in filters:
                    raise OneViewQueryError("InvalidQuery", "Time controls cannot be combined with a created filter.")
                params.append(("filter", f'"{resource["time_field"]}>=\'{self._iso(window[0])}\'"'))
                params.append(("filter", f'"{resource["time_field"]}<=\'{self._iso(window[1])}\'"'))

        if "sort" in spec:
            if not resource["sortable"] or singleton:
                raise OneViewQueryError("InvalidQuery", "This table does not support sorting.")
            order = spec["sort"]
            if not isinstance(order, list) or not order or not all(isinstance(x, str) for x in order):
                raise OneViewQueryError("InvalidQuery", "sort must be a list of 'field asc|desc' strings.")
            terms = []
            for term in order:
                parts = term.split()
                if not 1 <= len(parts) <= 2 or parts[0] not in resource["fields"] or "." in parts[0] or (
                    len(parts) == 2 and parts[1] not in {"asc", "desc"}
                ):
                    raise OneViewQueryError("InvalidQuery", "Invalid sort field or direction.")
                terms.append(f"{parts[0]}:{parts[1] if len(parts) == 2 else 'asc'}")
            params.append(("sort", ",".join(terms)))

        for control in ("metrics", "view"):
            if control in spec and kind != "utilization":
                raise OneViewQueryError("InvalidQuery", f"{control} is only valid for server_hardware_utilization.")
        for control in ("category", "user_query"):
            if control in spec and kind != "index":
                raise OneViewQueryError("InvalidQuery", f"{control} is only valid for index_resources.")

        if kind == "utilization":
            metrics = spec.get("metrics", resource["metrics"])
            if (
                not isinstance(metrics, list)
                or not metrics
                or not all(isinstance(m, str) for m in metrics)
                or set(metrics) - set(resource["metrics"])
            ):
                raise OneViewQueryError("InvalidQuery", f"metrics must be a subset of {resource['metrics']}.")
            view = spec.get("view", "hour")
            if view not in resource["views"]:
                raise OneViewQueryError("InvalidQuery", f"view must be one of {resource['views']}.")
            start, end = self._time_window(spec, now, required=True)
            params += [
                ("fields", ",".join(dict.fromkeys(metrics))),
                ("filter", f"startDate={self._iso(start)},endDate={self._iso(end)}"),
                ("view", view),
            ]
        if kind == "index":
            categories = spec.get("category", [])
            if isinstance(categories, str):
                categories = [categories]
            if not isinstance(categories, list) or not all(
                isinstance(c, str) and re.fullmatch(r"[a-z0-9-]{1,64}", c) for c in categories
            ):
                raise OneViewQueryError("InvalidQuery", "category must be a list of lowercase category names.")
            params += [("category", c) for c in categories]
            if "user_query" in spec:
                uq = spec["user_query"]
                if not isinstance(uq, str) or not uq.strip() or len(uq) > 256 or any(c in uq for c in "\x00\n\r"):
                    raise OneViewQueryError("InvalidQuery", "user_query must be a short text string.")
                params.append(("userQuery", uq))

        query_id = str(uuid.uuid4())
        endpoint = path
        with self._session() as session:
            deadline = time.monotonic() + 120
            try:
                if singleton:
                    body = self._request(session, "GET", path + ("?" + urlencode(params) if params else ""), deadline)
                    records = self._singleton_rows(body, resource, kind, parents)
                else:
                    records = self._pages(session, path, params, limit, deadline)
                self._availability[spec["table"]] = "available" if records else "empty"
            except OneViewQueryError as e:
                self._availability[spec["table"]] = {
                    401: "authentication_failed",
                    403: "permission_denied",
                    404: "unsupported",
                }.get(e.status, e.code)
                raise
        if kind == "collection_flatten" or (kind == "collection_flatten" and singleton):
            records = self._flatten(records, resource)
        if local_filters:
            records = [
                r for r in records
                if all(self._local_match(self._extract(r, k), v) for k, v in local_filters.items())
            ]
        if len(records) > limit:
            raise OneViewQueryError("ResultLimitExceeded", _LIMIT_MSG)
        rows = []
        for record in records:
            row = {k: self._extract(record, k) for k in fields}
            row.update(parents)
            row.update(
                _bow_appliance=self._appliance,
                _bow_connection=getattr(self, "_bow_connection_id", None),
                _bow_retrieved_at=now.isoformat(),
                _bow_query_id=query_id,
                _bow_endpoint=endpoint,
                _bow_api_version=self._api_version,
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
                    frame[k] = pd.array(frame[k], dtype="string")
            except (ValueError, TypeError, OverflowError):
                raise OneViewQueryError("InvalidResponse", f"Invalid value type for {k}.") from None
        frame["_bow_retrieved_at"] = pd.to_datetime(frame["_bow_retrieved_at"], utc=True)
        frame["_bow_api_version"] = pd.array(frame["_bow_api_version"], dtype="Int64")
        return frame

    def _singleton_rows(self, body, resource, kind, parents):
        if not isinstance(body, dict):
            raise OneViewQueryError("InvalidResponse", "Expected a JSON object.")
        if kind == "utilization":
            return self._utilization_rows(body, parents)
        if kind == "singleton_members":
            members = body.get(resource["members_key"])
            if members is None:
                return [{"member": body, **{k: body.get(k) for k in resource["fields"] if k != "member"}}]
            if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
                raise OneViewQueryError("InvalidResponse", "Expected a members list.")
            return [{**m, "member": m} for m in members]
        if kind == "collection_flatten":
            return [body]
        return [body]

    @staticmethod
    def _utilization_rows(body, parents):
        metrics = body.get("metricList")
        if not isinstance(metrics, list):
            raise OneViewQueryError("InvalidResponse", "Utilization response is missing metricList.")
        context = {
            k: body.get(k)
            for k in ("resolution", "sliceStartTime", "sliceEndTime", "newestSampleTime", "oldestSampleTime", "isFresh", "refreshTaskUri")
        }
        rows = []
        for metric in metrics:
            if not isinstance(metric, dict) or not isinstance(metric.get("metricSamples"), list):
                raise OneViewQueryError("InvalidResponse", "Malformed utilization metric.")
            for sample in metric["metricSamples"]:
                if not isinstance(sample, list) or len(sample) != 2 or isinstance(sample[0], bool) or not isinstance(sample[0], (int, float)):
                    raise OneViewQueryError("InvalidResponse", "Malformed utilization sample.")
                value = sample[1]
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise OneViewQueryError("InvalidResponse", "Malformed utilization sample value.")
                rows.append(
                    {
                        **parents,
                        "metricName": metric.get("metricName"),
                        "sampleTime": datetime.fromtimestamp(sample[0] / 1000, UTC).isoformat(),
                        "value": None if value is None else float(value),
                        "metricCapacity": metric.get("metricCapacity"),
                        **context,
                    }
                )
        return rows

    @staticmethod
    def _flatten(records, resource):
        key, alias = resource["flatten"], resource["flatten_as"]
        carry = resource.get("carry", {})
        rows = []
        for record in records:
            items = record.get(key)
            if items is None:
                items = []
            if not isinstance(items, list):
                raise OneViewQueryError("InvalidResponse", f"Expected a list in {key}.")
            base = {k: v for k, v in record.items() if k != key}
            for item in items:
                if not isinstance(item, dict):
                    raise OneViewQueryError("InvalidResponse", f"Expected objects in {key}.")
                row = dict(base)
                row.update({dst: record.get(src) for dst, src in carry.items()})
                row[alias] = item
                rows.append(row)
        return rows

    @staticmethod
    def _extract(record, dotted):
        current = record
        for part in dotted.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return HpeOneViewClient._sanitize(current)

    @staticmethod
    def _sanitize(value):
        if isinstance(value, dict):
            return {k: HpeOneViewClient._sanitize(v) for k, v in value.items() if not _SECRET_KEY.search(k)}
        if isinstance(value, list):
            return [HpeOneViewClient._sanitize(v) for v in value]
        return value

    def _pages(self, session, path, params, limit, deadline):
        page = [("start", "0"), ("count", str(min(_PAGE_SIZE, limit)))]
        current = path + "?" + urlencode(page + params)
        seen = set()
        records = []
        for _ in range(1000):
            if current in seen:
                raise OneViewQueryError("InvalidResponse", "Repeated pagination continuation.")
            seen.add(current)
            body = self._request(session, "GET", current, deadline)
            if not isinstance(body, dict) or "errorCode" in body:
                raise OneViewQueryError("InvalidResponse", "OneView returned an incomplete or invalid collection.")
            batch = body.get("members")
            if not isinstance(batch, list) or not all(isinstance(r, dict) for r in batch):
                raise OneViewQueryError("InvalidResponse", "Expected a members collection.")
            total = body.get("total")
            if isinstance(total, int) and not isinstance(total, bool) and total > limit:
                raise OneViewQueryError(
                    "ResultLimitExceeded", f"The appliance reports {total} matching rows. " + _LIMIT_MSG
                )
            records.extend(batch)
            if len(records) > limit:
                raise OneViewQueryError("ResultLimitExceeded", _LIMIT_MSG)
            nxt = body.get("nextPageUri")
            if nxt is None or nxt == body.get("uri") or not batch:
                return records
            if not isinstance(nxt, str):
                raise OneViewQueryError("InvalidResponse", "Invalid pagination continuation.")
            next_url = urljoin(self.url + current, nxt)
            parsed = urlsplit(next_url)
            origin = urlsplit(self.url)
            if parsed.scheme != origin.scheme or parsed.netloc != origin.netloc or parsed.path != path or parsed.fragment:
                raise OneViewQueryError("InvalidResponse", "Continuation escaped the selected resource.")
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            sent = {k for k, _ in params} | {"start", "count"}
            if any(k not in sent for k, _ in pairs):
                raise OneViewQueryError("InvalidResponse", "Continuation introduced unsupported query controls.")
            theirs = sorted((k, v) for k, v in pairs if k not in {"start", "count"})
            if theirs != sorted(params):
                raise OneViewQueryError("InvalidResponse", "Continuation changed query scope.")
            start = dict(pairs).get("start", "")
            if not re.fullmatch(r"[0-9]{1,9}", start) or int(start) <= int(dict(page)["start"]):
                raise OneViewQueryError("InvalidResponse", "Continuation did not advance.")
            page = [("start", start), ("count", dict(page)["count"])]
            current = path + "?" + urlencode(page + params)
        raise OneViewQueryError("BudgetExceeded", "Pagination budget exhausted.")
