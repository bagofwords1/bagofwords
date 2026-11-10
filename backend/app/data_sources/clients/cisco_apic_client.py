"""Read-only Cisco ACI APIC diagnostic tables.

Every table is one APIC model class from the frozen catalog
(``cisco_apic/catalog.json``, derived from Cisco's published model metadata).
Reads are ``GET /api/class/<class>.json`` or the node-scoped
``GET /api/node/class/topology/pod-<p>/node-<n>/<class>.json`` with only
``query-target-filter``, ``order-by``, ``page`` and ``page-size`` controls, all
built by this client from a validated JSON query.

Authentication is either certificate signatures (no session: every request is
signed with the user's private key) or a password session (``POST
/api/aaaLogin.json`` per operation, ``POST /api/aaaLogout.json`` in ``finally``).
Those two POSTs are the only non-GET requests the client can build; writes,
subscriptions, token refresh, file downloads and raw URLs are unreachable by
construction.
"""

from __future__ import annotations

import base64
import difflib
import json
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import Table, TableColumn
from app.core.egress import pinned_session
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_progress

_LIMIT_MSG = (
    "limit is a complete-result ceiling, not a preview size: drop limit (the default is max_rows) and use "
    ".head(n) on the DataFrame, or narrow filter/node/lookback. Nothing was returned or truncated."
)


class ApicQueryError(ValueError):
    """Stable machine code plus a safe explanation; never echoes credentials."""

    def __init__(self, code, message, status=None, apic_code=None):
        self.code, self.status, self.apic_code = code, status, apic_code
        super().__init__(f"{code}: {message}")


_TYPES = {"integer": "int", "number": "float", "datetime": "datetime", "string": "str"}
_DERIVED = {
    "pod": ("int", "Pod id parsed from dn (topology/pod-<p>/...)."),
    "node": ("int", "Node id parsed from dn (topology/pod-<p>/node-<n>/...)."),
    "parent_dn": ("str", "The dn minus its last relative name: the object a fault, health score, interface status or binding belongs to."),
}
_META = {
    "_bow_apic": "str",
    "_bow_connection": "str",
    "_bow_retrieved_at": "datetime",
    "_bow_query_id": "str",
    "_bow_class": "str",
    "_bow_window_start": "datetime",
    "_bow_window_end": "datetime",
    "_bow_contract": "str",
}
_OPS = {"eq", "ne", "gt", "ge", "lt", "le"}
_CLASS_PATH = re.compile(r"/api/(?:class|node/class/topology/pod-[1-9][0-9]{0,2}/node-[1-9][0-9]{0,4})/([A-Za-z][A-Za-z0-9]{1,63})\.json")
_LOGIN, _LOGOUT = "/api/aaaLogin.json", "/api/aaaLogout.json"
_QUERY_PARAMS = ("query-target-filter", "order-by", "page", "page-size")

_MAX_BODY = 32 * 1024 * 1024


@lru_cache(maxsize=1)
def _catalog():
    catalog = json.loads(Path(__file__).with_name("cisco_apic").joinpath("catalog.json").read_text())
    for name, table in catalog["tables"].items():
        cls = table["class"]
        if not re.fullmatch(r"[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*", cls):
            raise ApicQueryError("InvalidCatalog", f"Table {name} has an invalid class name.")
        if not table["fields"] or "dn" not in table["fields"]:
            raise ApicQueryError("InvalidCatalog", f"Table {name} must expose dn.")
        if set(table["default_fields"]) - set(table["fields"]):
            raise ApicQueryError("InvalidCatalog", f"Table {name} has unknown default fields.")
        if set(table["fields"]) & set(_DERIVED):
            raise ApicQueryError("InvalidCatalog", f"Table {name} collides with a derived column.")
        if table["time_field"] and table["fields"].get(table["time_field"], {}).get("type") != "datetime":
            raise ApicQueryError("InvalidCatalog", f"Table {name} has a non-time window field.")
        order_cls = table["order_by"].split(".", 1)[0]
        if order_cls != cls or table["order_by"].split(".", 1)[1].split("|")[0] not in table["fields"]:
            raise ApicQueryError("InvalidCatalog", f"Table {name} has an invalid order-by.")
    return catalog


def _split_dn(dn):
    """Split a DN into relative names, respecting nested [...] (DNs like phys-[eth1/1])."""
    parts, depth, current = [], 0, []
    for ch in dn:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(depth - 1, 0)
        if ch == "/" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _derived(dn):
    """pod, node and parent_dn for a DN (bracket-aware)."""
    m = re.match(r"topology/pod-([0-9]+)(?:/node-([0-9]+))?", dn or "")
    parts = _split_dn(dn or "")
    return {
        "pod": int(m.group(1)) if m else None,
        "node": int(m.group(2)) if m and m.group(2) else None,
        "parent_dn": "/".join(parts[:-1]) if len(parts) > 1 else None,
    }


def _unknown_property(name, table, prop):
    """An actionable error: the sandbox has no get_schema, so name the valid properties."""
    close = difflib.get_close_matches(prop, list(table["fields"]) + list(_DERIVED), n=4, cutoff=0.5)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    return ApicQueryError(
        "InvalidQuery",
        f"Unknown property {prop!r} for table {name}.{hint} Main properties: {', '.join(table['default_fields'])}; "
        f"plus pod, node, parent_dn. fields \"*\" returns all {len(table['fields'])} properties.",
    )


class CiscoApicClient(DataSourceClient):
    capture_structured_queries = True
    relative_date_hint = (
        'Use JSON "lookback":"24h" on faults, fault_delegates, fault_history, events, audit_log, health_history and '
        "fabric_health_history; it is resolved at each execution. Do not freeze dates in saved investigations."
    )

    def __init__(
        self,
        url,
        username,
        password=None,
        private_key=None,
        certificate_name=None,
        login_domain=None,
        fallback_urls=None,
        ca_certificate=None,
        timeout=30,
        max_rows=10000,
        allow_http=False,
        **kwargs,
    ):
        self._urls = [self._origin(url, allow_http)]
        for extra in self._split_urls(fallback_urls):
            origin = self._origin(extra, allow_http)
            if origin not in self._urls:
                self._urls.append(origin)
        if len(self._urls) > 5:
            raise ApicQueryError("InvalidConfiguration", "Configure at most five APIC addresses.")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}", username.strip()):
            raise ApicQueryError("InvalidConfiguration", "Username must be an APIC user name (letters, digits, . _ @ -).")
        self._username = username.strip()
        password = password or None
        private_key = private_key.strip() if isinstance(private_key, str) and private_key.strip() else None
        if bool(password) == bool(private_key):
            raise ApicQueryError("InvalidConfiguration", "Supply either a password or a certificate private key, not both and not neither.")
        login_domain = login_domain.strip() if isinstance(login_domain, str) and login_domain.strip() else None
        if login_domain and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", login_domain):
            raise ApicQueryError("InvalidConfiguration", "Login domain must be an APIC AAA login domain name.")
        self._password = password
        self._login_domain = login_domain
        self._key = None
        self._cert_dn = None
        if private_key:
            if login_domain:
                raise ApicQueryError("InvalidConfiguration", "Certificate authentication is for local users; leave the login domain empty.")
            cert_name = certificate_name.strip() if isinstance(certificate_name, str) else ""
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cert_name):
                raise ApicQueryError("InvalidConfiguration", "Certificate name must be the name of the X.509 certificate attached to the APIC user.")
            self._key = self._load_key(private_key)
            self._cert_dn = f"uni/userext/user-{self._username}/usercert-{cert_name}"
        self.timeout = self._integer(timeout, "timeout", 1, 120)
        self.max_rows = self._integer(max_rows, "max_rows", 1, 100000)
        self._ca = None
        self._verify = True
        if ca_certificate:
            self._ca = tempfile.TemporaryDirectory(prefix="bow-apic-ca-")
            cert = Path(self._ca.name) / "ca.pem"
            cert.write_text(ca_certificate)
            cert.chmod(0o600)
            self._verify = str(cert)
        self._lock = threading.RLock()
        self._availability = {}
        self._apic = urlsplit(self._urls[0]).hostname
        self._version = None

    # ------------------------------------------------------------------ configuration helpers
    @staticmethod
    def _split_urls(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            items = value
        elif isinstance(value, str):
            items = re.split(r"[\s,;]+", value)
        else:
            raise ApicQueryError("InvalidConfiguration", "Additional APIC addresses must be a comma-separated list.")
        return [str(x).strip() for x in items if str(x).strip()]

    @staticmethod
    def _origin(url, allow_http):
        url = str(url or "").strip().rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ApicQueryError("InvalidConfiguration", "Use an APIC origin such as https://apic1.example.com, without a path or credentials.")
        if parsed.scheme != "https":
            if not allow_http:
                raise ApicQueryError("InvalidConfiguration", "HTTPS is required. HTTP is only available through the simulator option.")
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ApicQueryError("InvalidConfiguration", "HTTP is only allowed towards a loopback simulator.")
        return url

    @staticmethod
    def _load_key(pem):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        try:
            key = serialization.load_pem_private_key(pem.encode(), password=None)
        except (ValueError, TypeError):
            raise ApicQueryError(
                "InvalidConfiguration", "The private key must be an unencrypted PEM RSA key (BEGIN PRIVATE KEY / BEGIN RSA PRIVATE KEY)."
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ApicQueryError("InvalidConfiguration", "APIC signature authentication needs an RSA private key of at least 2048 bits.")
        return key

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if isinstance(value, str) and re.fullmatch(r"\s*[0-9]{1,9}\s*", value):
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ApicQueryError("InvalidQuery", f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    def close(self):
        if self._ca:
            self._ca.cleanup()
            self._ca = None

    def __repr__(self):
        return f"CiscoApicClient(apic={self._apic!r}, user={self._username!r}, auth={'certificate' if self._key else 'password'})"

    # ------------------------------------------------------------------ description / schema
    @property
    def description(self):
        names = ", ".join(_catalog()["tables"])
        return f"""Cisco ACI APIC read-only investigation connector (APIC 6.x model; one table per APIC class, read through the REST API).
Call get_tables()/get_schema(name) to discover tables, then execute_query(query) with a JSON object or string.
Tables: {names}.
Examples:
 {{"table":"fabric_nodes"}}  (node ids, names, roles, versions; start here)
 {{"table":"faults","filter":{{"severity":{{"op":"in","value":["critical","major"]}}}},"lookback":"24h"}}
 {{"table":"faults","node":303}}  (faults on one switch; node needs a node id from fabric_nodes)
 {{"table":"audit_log","lookback":"2h"}}  (who changed what; compare created with the incident start)
 {{"table":"events","lookback":"1h","filter":{{"cause":"port-down"}}}}
 {{"table":"interface_status","node":303,"filter":{{"operSt":"down"}}}}  (operStQual says why a port is down)
 {{"table":"endpoints","filter":{{"ip":"10.10.30.21"}}}}  (where is an IP/MAC: fabricPathDn = leaf/port/vPC, parent_dn = EPG)
 {{"table":"bgp_peers","filter":{{"operSt":{{"op":"ne","value":"established"}}}}}}
 {{"table":"switch_firmware","fields":["dn","version","biosVer"]}}
 {{"table":"filter_entries","filter":{{"dn":{{"op":"contains","value":"tn-retail/"}}}}}}
Filters run on the APIC (query-target-filter): a scalar means eq; {{"op":..,"value":..}} with eq, ne, gt, ge, lt, le, in (list), contains (substring of the text). Values are compared as the APIC stores them: enums are lowercase words (critical, major, up, down, established), yes/no flags are "yes"/"no", numbers and times compare numerically.
lookback ("30m","24h","7d") or start_time/end_time (ISO-8601 with timezone) apply to the table's time column. fault_history, events, audit_log, health_history and fabric_health_history always use a window: the last 24h when none is given (see _bow_window_start/_bow_window_end).
node (int or list of up to 20 ints) and optional pod (default 1) read a switch-resident class from those nodes only; it is faster and avoids a fabric-wide fan-out.
fields: list of class properties or "*"; omitted fields select a compact RCA projection. sort: list of "property asc|desc". Do not pass limit to preview: it is a complete-result ceiling (fails with ResultLimitExceeded, never truncates); omit it and use .head(n).
Every row has pod, node (when the dn is under topology/pod-<p>/node-<n>) and parent_dn (the dn minus its last part: the object a fault, health score, interface status, contract or binding belongs to); you can filter on all three. Times are UTC.
RCA tips: faults + events + audit_log in the incident window first; a config change shortly before symptoms is a prime suspect; interface_status.operStQual and bgp_peers.lastFlapTs explain link/peer failures; compare switch_firmware versions across nodes; contracts -> contract_subjects -> subject_filters -> filter_entries show which ports a contract permits, epg_provided_contracts / epg_consumed_contracts show who uses it.
Faults, fault history, events, audit log and statistics tables are readable only by accounts with admin read access on the APIC: a read-all account gets 0 rows there, which is not evidence of a healthy fabric.
No raw URLs, DNs paths, POST/DELETE, subscriptions or configuration changes: the client cannot send a mutating request."""

    def get_tables(self):
        names = list(_catalog()["tables"])
        return [self.get_schema(n) for n in discovery_items(names, "tables", label=str)]

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    @staticmethod
    def _table(name):
        if not isinstance(name, str) or name not in _catalog()["tables"]:
            raise ApicQueryError("InvalidQuery", "Unknown table; use get_tables to discover supported tables.")
        return _catalog()["tables"][name]

    def get_schema(self, table_name):
        t = self._table(table_name)
        columns = []
        for name, spec in t["fields"].items():
            desc = spec.get("description") or spec.get("label") or name
            if spec.get("values"):
                desc = f"{desc} Values: {', '.join(spec['values'][:25])}."
            columns.append(TableColumn(name=name, dtype=_TYPES.get(spec["type"], "str"), description=desc[:400]))
        columns += [TableColumn(name=k, dtype=v[0], description=v[1]) for k, v in _DERIVED.items()]
        columns += [TableColumn(name=k, dtype=v) for k, v in _META.items()]
        desc = f"{t['description']} APIC class {t['class']}."
        if t["node_scoped"]:
            desc += " Supports node (switch id) scoping."
        if t["time_field"]:
            desc += f" Time column {t['time_field']}" + (" (window always applied, default 24h)." if t["window_required"] else " (lookback optional).")
        if t["admin_read_only"]:
            desc += " Needs admin read access on the APIC; a read-all account sees 0 rows."
        meta = {
            "class": t["class"],
            "node_scoped": t["node_scoped"],
            "time_field": t["time_field"],
            "window_required": t["window_required"],
            "default_fields": t["default_fields"],
            "read_access": t["read_access"],
            "admin_read_only": t["admin_read_only"],
            "dn_example": t["dn_example"],
            "availability": self._availability.get(table_name, "unverified"),
            "contract": _catalog()["sha256"],
        }
        return Table(
            name=table_name,
            description=desc,
            columns=columns,
            pks=[c for c in columns if c.name == "dn"],
            fks=[],
            metadata_json={"cisco_apic": meta},
        )

    def prompt_schema(self):
        return "\n".join(
            f"{n}: {t['description'][:140]} | class={t['class']}{' node' if t['node_scoped'] else ''}"
            f"{' window=' + t['time_field'] if t['time_field'] else ''}"
            for n, t in _catalog()["tables"].items()
        )

    def coverage_report(self):
        c = _catalog()
        return {
            "contract": c["contract"],
            "sha256": c["sha256"],
            "validation": "simulator_verified_awaiting_customer_validation",
            "tables": [
                {"table": n, "class": t["class"], "availability": self._availability.get(n, "unverified")}
                for n, t in c["tables"].items()
            ],
            "gaps": [
                "AAA users, certificates, keys, backups and tech-support files are excluded.",
                "Properties flagged secure in the APIC model are never requested or returned.",
                "Statistics tables show the current 5-minute interval only.",
            ],
        }

    # ------------------------------------------------------------------ transport
    def _signature_cookie(self, method, uri, body):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload = (method + uri).encode() + (body or b"")
        signature = base64.b64encode(self._key.sign(payload, padding.PKCS1v15(), hashes.SHA256())).decode()
        return (
            f"APIC-Certificate-Algorithm=v1.0; APIC-Certificate-DN={self._cert_dn}; "
            f"APIC-Certificate-Fingerprint=fingerprint; APIC-Request-Signature={signature}"
        )

    def _request(self, session, method, uri, deadline, payload=None):
        """The ONLY network primitive: GET on a catalog class path, or the login/logout POST."""
        path, _, query = uri.partition("?")
        if method == "GET":
            m = _CLASS_PATH.fullmatch(path)
            if not m or m.group(1) not in {t["class"] for t in _catalog()["tables"].values()}:
                raise ApicQueryError("ForbiddenOperation", "The request is outside the read-only allow-list.")
            for pair in query.split("&") if query else []:
                if pair.split("=", 1)[0] not in _QUERY_PARAMS:
                    raise ApicQueryError("ForbiddenOperation", "Unsupported query control; no request was sent.")
        elif method == "POST" and path in {_LOGIN, _LOGOUT} and not query and self._password:
            pass
        else:
            raise ApicQueryError("ForbiddenOperation", "Only read-only requests are permitted.")
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self._key:
            headers["Cookie"] = self._signature_cookie(method, uri, body)
        elif session.token and path != _LOGIN:
            headers["Cookie"] = f"APIC-cookie={session.token}"
        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ApicQueryError("BudgetExceeded", "Query deadline exhausted.")
            try:
                with session.http.request(
                    method,
                    session.base + uri,
                    data=body,
                    headers=headers,
                    timeout=min(self.timeout, remaining),
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    status = response.status_code
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw) > _MAX_BODY:
                            raise ApicQueryError("BudgetExceeded", "Response exceeds 32 MiB; narrow the query.")
                        if time.monotonic() > deadline:
                            raise ApicQueryError("BudgetExceeded", "Query deadline exhausted.")
            except requests.exceptions.SSLError:
                raise ApicQueryError("TLSFailed", "Certificate verification failed; configure the trusted CA.") from None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 3 or method != "GET":
                    raise ApicQueryError("TransportFailed", "The APIC could not be reached within the retry budget.") from None
                time.sleep(min(0.25 * 2**attempt, max(0, deadline - time.monotonic())))
                continue
            if status in {429, 502, 503, 504} and method == "GET" and attempt < 3:
                retry = response.headers.get("Retry-After", "")
                delay = float(retry) if retry.isdigit() else 0.5 * 2**attempt
                if delay >= deadline - time.monotonic():
                    raise ApicQueryError("BudgetExceeded", "Retry exceeds query deadline.")
                time.sleep(delay)
                continue
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                data = None
            apic_code, apic_text = self._apic_error(data)
            if status != 200 or apic_code is not None:
                raise self._http_error(method, path, status, apic_code, apic_text)
            if not isinstance(data, dict):
                raise ApicQueryError("InvalidResponse", "The APIC did not return a JSON object.")
            return data
        raise ApicQueryError("ApicUnavailable", "The APIC kept answering busy (429/503). Narrow the query (node, filter, lookback) and retry.", 503)

    @staticmethod
    def _apic_error(data):
        try:
            item = data["imdata"][0]["error"]["attributes"]
        except (KeyError, IndexError, TypeError):
            return None, None
        code = str(item.get("code", ""))[:16]
        text = re.sub(r"[\x00-\x1f]+", " ", str(item.get("text", "")))[:240]
        return code, text

    @staticmethod
    def _http_error(method, path, status, apic_code, apic_text):
        if path == _LOGIN:
            if status in {400, 401, 403}:
                return ApicQueryError("AuthenticationFailed", "The APIC rejected the user name, password or login domain.", status, apic_code)
            return ApicQueryError("HttpError", f"APIC login returned HTTP {status}.", status, apic_code)
        if status == 403:
            return ApicQueryError(
                "PermissionDenied",
                "The APIC refused the request (expired session, invalid certificate signature or missing read privilege).",
                status,
                apic_code,
            )
        if status == 400:
            detail = f" APIC said: {apic_text}" if apic_text else ""
            return ApicQueryError("InvalidQuery", f"The APIC rejected the query.{detail}", status, apic_code)
        if status in {429, 503}:
            return ApicQueryError(
                "ApicUnavailable", "The APIC is busy or the result set is too big. Narrow the query (node, filter, lookback).", status, apic_code
            )
        return ApicQueryError("HttpError", f"The APIC returned HTTP {status} for {method} {path}.", status, apic_code)

    class _Session:
        def __init__(self, http, base):
            self.http, self.base, self.token = http, base, None

    @contextmanager
    def _session(self, base):
        with pinned_session() as http:
            http.trust_env = False
            http.verify = self._verify
            http.headers.update({"User-Agent": "bagofwords-cisco-apic/1"})
            session = self._Session(http, base)
            if not self._password:
                yield session
                return
            name = f"apic:{self._login_domain}\\\\{self._username}" if self._login_domain else self._username
            deadline = time.monotonic() + self.timeout * 2
            body = self._request(session, "POST", _LOGIN, deadline, {"aaaUser": {"attributes": {"name": name, "pwd": self._password}}})
            try:
                attrs = body["imdata"][0]["aaaLogin"]["attributes"]
                token = attrs["token"]
            except (KeyError, IndexError, TypeError):
                raise ApicQueryError("InvalidResponse", "The APIC login response carried no session token.") from None
            if not isinstance(token, str) or not token or len(token) > 4096:
                raise ApicQueryError("InvalidResponse", "The APIC login response carried no session token.")
            if isinstance(attrs.get("version"), str):
                self._version = attrs["version"][:32]
            session.token = token
            try:
                yield session
            finally:
                with suppress(ApicQueryError):
                    self._request(session, "POST", _LOGOUT, time.monotonic() + 10, {"aaaUser": {"attributes": {"name": name}}})
                session.token = None

    def _with_failover(self, operation):
        """Run operation(session) against the first reachable APIC of the cluster."""
        last = None
        with self._lock:
            for base in self._urls:
                try:
                    with self._session(base) as session:
                        result = operation(session)
                    self._apic = urlsplit(base).hostname
                    return result
                except ApicQueryError as e:
                    if e.code not in {"TransportFailed", "ApicUnavailable"}:
                        raise
                    last = e
        raise last

    # ------------------------------------------------------------------ connection test
    def test_connection(self):
        deadline = time.monotonic() + self.timeout * 4

        def probe(session):
            nodes = self._get(session, "fabricNode", None, {"page": "0", "page-size": "200", "order-by": "fabricNode.id|asc"}, deadline)
            faults = self._get(session, "faultInst", None, {"page": "0", "page-size": "1"}, deadline)
            return nodes, faults

        nodes, faults = self._with_failover(probe)
        rows = self._rows(nodes, "fabricNode")
        controllers = [r for r in rows if r.get("role") == "controller"]
        switches = [r for r in rows if r.get("role") in {"spine", "leaf"}]
        version = self._version or next((r.get("version") for r in controllers if r.get("version")), None)
        if not rows:
            raise ApicQueryError(
                "InvalidResponse", "The APIC returned no fabric nodes; check that the account has read access (read-all role) to the fabric."
            )
        message = (
            f"Connected to APIC {version or 'unknown version'} at {self._apic}: {len(controllers)} controllers and "
            f"{len(switches)} switches. {len(_catalog()['tables'])} read-only tables available."
        )
        if str(faults.get("totalCount", "0")) == "0":
            message += (
                " Note: no faults are visible. If the fabric has faults, this account lacks admin read access "
                "and the faults, events, audit and statistics tables will be empty."
            )
        return {"success": True, "message": message}

    # ------------------------------------------------------------------ query building
    def _get(self, session, cls, scope, params, deadline):
        base = f"/api/node/class/{scope}/{cls}.json" if scope else f"/api/class/{cls}.json"
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        return self._request(session, "GET", base + ("?" + query if query else ""), deadline)

    @staticmethod
    def _rows(body, cls):
        imdata = body.get("imdata")
        if not isinstance(imdata, list):
            raise ApicQueryError("InvalidResponse", "The APIC response has no imdata list.")
        rows = []
        for item in imdata:
            if not isinstance(item, dict) or len(item) != 1:
                raise ApicQueryError("InvalidResponse", "Malformed imdata entry.")
            (key, value), = item.items()
            attrs = value.get("attributes") if isinstance(value, dict) else None
            if not isinstance(attrs, dict):
                raise ApicQueryError("InvalidResponse", "Malformed imdata entry.")
            if key != cls:
                continue  # a subclass of the queried class; not part of this table's contract
            rows.append(attrs)
        return rows

    @staticmethod
    def _literal(value):
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value != value or value in (float("inf"), float("-inf")):
                raise ApicQueryError("InvalidQuery", "Filter values must be finite.")
            return str(value)
        if not isinstance(value, str):
            raise ApicQueryError("InvalidQuery", "Filter values must be strings, numbers or booleans.")
        if any(c in value for c in '"\\') or re.search(r"[\x00-\x1f]", value) or len(value) > 256:
            raise ApicQueryError("InvalidQuery", "Filter values cannot contain quotes, backslashes or control characters.")
        return value

    def _terms(self, name, table, filters):
        """APIC filter expressions, plus row predicates re-applied locally.

        `contains` and the derived columns (pod, node, parent_dn) are sent to the APIC as a
        superset `wcard` on the property or dn and checked exactly on the returned rows.
        """
        cls = table["class"]
        terms, local = [], []
        for prop, spec in filters.items():
            if prop not in table["fields"] and prop not in _DERIVED:
                raise _unknown_property(name, table, prop)
            if isinstance(spec, dict):
                if set(spec) != {"op", "value"}:
                    raise ApicQueryError("InvalidQuery", "Comparisons use {op, value}.")
                op, value = spec["op"], spec["value"]
            else:
                op, value = "eq", spec
            if op not in _OPS | {"in", "contains"}:
                raise ApicQueryError("InvalidQuery", "Unknown operator; use eq, ne, gt, ge, lt, le, in or contains.")
            if op == "in" and (not isinstance(value, list) or not 1 <= len(value) <= 20):
                raise ApicQueryError("InvalidQuery", "in requires a list of 1-20 values.")
            if op == "contains" and (not isinstance(value, str) or not value):
                raise ApicQueryError("InvalidQuery", "contains requires a non-empty string.")
            if prop in _DERIVED:
                term, predicate = self._derived_filter(cls, prop, op, value)
                if term:
                    terms.append(term)
                local.append(predicate)
                continue
            ftype = table["fields"][prop]["type"]
            if op == "in":
                parts = [f'eq({cls}.{prop},"{self._value(ftype, v)}")' for v in value]
                terms.append(parts[0] if len(parts) == 1 else f"or({','.join(parts)})")
            elif op == "contains":
                literal = self._literal(value)
                terms.append(f'wcard({cls}.{prop},"{self._wcard_pattern(literal)}")')
                local.append(lambda r, p=prop, t=literal: t in str(r.get(p, "")))
            else:
                terms.append(f'{op}({cls}.{prop},"{self._value(ftype, value)}")')
        return terms, local

    def _derived_filter(self, cls, prop, op, value):
        """Server-side superset term (or None) and the exact local predicate for pod/node/parent_dn."""
        values = value if op == "in" else [value]
        if prop in {"pod", "node"}:
            nums = [self._integer(v, prop, 0, 16000) for v in values] if op != "contains" else None
            if nums is None:
                raise ApicQueryError("InvalidQuery", f"{prop} is a number; use eq, ne, gt, ge, lt, le or in.")
            pattern = (lambda n: f"topology/pod-{n}/") if prop == "pod" else (lambda n: f"/node-{n}/")
            wc = [f'wcard({cls}.dn,"{pattern(n)}")' for n in nums]
            term = (wc[0] if len(wc) == 1 else f"or({','.join(wc)})") if op in {"eq", "in"} else None

            def predicate(r, p=prop, o=op, ns=nums):
                actual = _derived(r.get("dn"))[p]
                if actual is None:
                    return o == "ne"
                return {"eq": actual == ns[0], "in": actual in ns, "ne": actual != ns[0], "gt": actual > ns[0],
                        "ge": actual >= ns[0], "lt": actual < ns[0], "le": actual <= ns[0]}[o]

            return term, predicate
        texts = [self._literal(v) for v in values]
        if op in {"gt", "ge", "lt", "le"}:
            raise ApicQueryError("InvalidQuery", "parent_dn takes eq, ne, in or contains.")
        if op in {"eq", "in"}:
            wc = [f'wcard({cls}.dn,"{self._wcard_pattern(t + "/")}")' for t in texts]
            term = wc[0] if len(wc) == 1 else f"or({','.join(wc)})"
        elif op == "contains":
            term = f'wcard({cls}.dn,"{self._wcard_pattern(texts[0])}")'
        else:
            term = None

        def predicate(r, o=op, ts=texts):
            actual = _derived(r.get("dn"))["parent_dn"] or ""
            return {"eq": actual == ts[0], "in": actual in ts, "ne": actual != ts[0], "contains": ts[0] in actual}[o]

        return term, predicate

    @staticmethod
    def _wcard_pattern(text):
        # The wcard operand is a regular expression and a quoted filter value cannot carry a
        # backslash, so each regex metacharacter becomes "." on the APIC (a superset match) and
        # the exact substring is re-checked on the returned rows.
        return "".join("." if ch in ".^$|?*+(){}[]" else ch for ch in text)

    def _value(self, ftype, value):
        if ftype == "datetime":
            return self._apic_time(self._date(value))
        if ftype in {"integer", "number"} and isinstance(value, str) and not re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", value):
            raise ApicQueryError("InvalidQuery", "Numeric properties compare against numbers.")
        return self._literal(value)

    @staticmethod
    def _date(value):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError()
            return dt.astimezone(UTC)
        except (ValueError, AttributeError, TypeError):
            raise ApicQueryError("InvalidQuery", "Times require an ISO-8601 timestamp with timezone.") from None

    @staticmethod
    def _apic_time(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}+00:00"

    def _window(self, spec, table, now):
        keys = {"lookback", "start_time", "end_time"} & set(spec)
        if keys and not table["time_field"]:
            raise ApicQueryError("InvalidQuery", "This table has no time column; lookback/start_time/end_time do not apply.")
        if not keys:
            return (now - timedelta(hours=24), now) if table["window_required"] else None
        if "lookback" in spec:
            if "start_time" in spec or "end_time" in spec:
                raise ApicQueryError("InvalidQuery", "Use lookback or start_time/end_time, not both.")
            match = re.fullmatch(r"([1-9][0-9]{0,4})(m|h|d|w)", str(spec["lookback"]).strip())
            if not match:
                raise ApicQueryError("InvalidQuery", "lookback must be a positive number followed by m, h, d or w (for example 24h).")
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
            if seconds > 366 * 86400:
                raise ApicQueryError("InvalidQuery", "lookback cannot exceed one year.")
            return now - timedelta(seconds=seconds), now
        if "start_time" not in spec:
            raise ApicQueryError("InvalidQuery", "start_time is required with end_time.")
        start = self._date(spec["start_time"])
        end = min(self._date(spec["end_time"]), now) if "end_time" in spec else now
        if start >= end:
            raise ApicQueryError("InvalidQuery", "start_time must be before end_time and before now.")
        return start, end

    def _nodes(self, spec, table):
        if "pod" in spec and "node" not in spec:
            raise ApicQueryError("InvalidQuery", "pod is only used together with node.")
        if "node" not in spec:
            return [None]
        if not table["node_scoped"]:
            raise ApicQueryError(
                "InvalidQuery", "This table is not stored on switches; filter it instead (for example on dn with contains)."
            )
        nodes = spec["node"] if isinstance(spec["node"], list) else [spec["node"]]
        if not 1 <= len(nodes) <= 20:
            raise ApicQueryError("InvalidQuery", "node takes one node id or a list of 1-20 node ids.")
        pod = self._integer(spec.get("pod", 1), "pod", 1, 255)
        return [f"topology/pod-{pod}/node-{self._integer(n, 'node', 1, 16000)}" for n in dict.fromkeys(nodes)]

    @staticmethod
    def _table_name(table):
        return next(k for k, v in _catalog()["tables"].items() if v is table)

    def _fields(self, requested, table):
        if requested == "*" or requested == ["*"]:
            return list(table["fields"])
        if not isinstance(requested, list) or not requested or not all(isinstance(x, str) for x in requested):
            raise ApicQueryError("InvalidQuery", 'fields must be a non-empty list of property names or "*".')
        out = []
        for name in requested:
            if name in _META or name in _DERIVED:
                continue
            if name not in table["fields"]:
                raise _unknown_property(self._table_name(table), table, name)
            out.append(name)
        return list(dict.fromkeys(["dn"] + out))

    def _order(self, spec, table):
        if "sort" not in spec:
            return table["order_by"]
        order = spec["sort"]
        if isinstance(order, str):
            order = [order]
        if not isinstance(order, list) or not 1 <= len(order) <= 3 or not all(isinstance(x, str) for x in order):
            raise ApicQueryError("InvalidQuery", "sort must be a list of 'property asc|desc' strings.")
        terms = []
        for term in order:
            parts = term.split()
            if not 1 <= len(parts) <= 2 or (len(parts) == 2 and parts[1] not in {"asc", "desc"}):
                raise ApicQueryError("InvalidQuery", "sort entries are 'property asc|desc'.")
            if parts[0] not in table["fields"]:
                raise _unknown_property(self._table_name(table), table, parts[0])
            terms.append(f"{table['class']}.{parts[0]}|{parts[1] if len(parts) == 2 else 'asc'}")
        return ",".join(terms)

    # ------------------------------------------------------------------ execute
    def execute_query(self, query):
        try:
            spec = json.loads(query) if isinstance(query, str) else dict(query) if isinstance(query, dict) else None
        except (ValueError, TypeError):
            raise ApicQueryError(
                "InvalidQuery",
                'This connector takes a JSON query, not SQL, for example {"table": "faults", "lookback": "24h"}; '
                "use get_tables() for the table names.",
            ) from None
        allowed = {"table", "fields", "filter", "node", "pod", "lookback", "start_time", "end_time", "sort", "limit"}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ApicQueryError("InvalidQuery", f"Unknown query controls; use {sorted(allowed)}.")
        name = spec.get("table")
        table = self._table(name)
        cls = table["class"]
        limit = self._integer(spec.get("limit", self.max_rows), "limit", 1, self.max_rows)
        fields = self._fields(spec.get("fields", table["default_fields"]), table)
        filters = spec.get("filter", {})
        if not isinstance(filters, dict):
            raise ApicQueryError("InvalidQuery", "filter must be an object of property: value or {op, value}.")
        now = datetime.now(UTC)
        window = self._window(spec, table, now)
        terms, local = self._terms(name, table, filters)
        if window:
            if table["time_field"] in filters:
                raise ApicQueryError("InvalidQuery", f"Use lookback/start_time/end_time instead of filtering {table['time_field']} directly.")
            terms.append(f'ge({cls}.{table["time_field"]},"{self._apic_time(window[0])}")')
            terms.append(f'le({cls}.{table["time_field"]},"{self._apic_time(window[1])}")')
        if len(terms) + sum(t.count("eq(") - 1 for t in terms if t.startswith("or(")) > _catalog()["contract"]["max_filter_terms"]:
            raise ApicQueryError("InvalidQuery", "The APIC accepts at most 20 filter expressions.")
        params = {}
        if terms:
            params["query-target-filter"] = terms[0] if len(terms) == 1 else f"and({','.join(terms)})"
        params["order-by"] = self._order(spec, table)
        scopes = self._nodes(spec, table)
        query_id = str(uuid.uuid4())
        page_size = min(_catalog()["contract"]["max_page_size"], limit + 1)
        deadline = time.monotonic() + 120

        def run(session):
            records = []
            for scope in scopes:
                page = 0
                while True:
                    body = self._get(session, cls, scope, {**params, "page": str(page), "page-size": str(page_size)}, deadline)
                    total = body.get("totalCount")
                    if not isinstance(total, (str, int)) or not re.fullmatch(r"[0-9]{1,9}", str(total)):
                        raise ApicQueryError("InvalidResponse", "The APIC response has no valid totalCount.")
                    total = int(total)
                    if page == 0 and len(records) + total > limit:
                        raise ApicQueryError(
                            "ResultLimitExceeded", f"The APIC reports {total} matching objects but limit is {limit}. " + _LIMIT_MSG
                        )
                    batch = self._rows(body, cls)
                    records.extend(batch)
                    if len(records) > limit:
                        raise ApicQueryError("ResultLimitExceeded", _LIMIT_MSG)
                    page += 1
                    if not body.get("imdata") or page * page_size >= total:
                        break
                    if page > 1000:
                        raise ApicQueryError("BudgetExceeded", "Pagination budget exhausted.")
            return records

        try:
            records = self._with_failover(run)
            records = [r for r in records if all(predicate(r) for predicate in local)]
            self._availability[name] = "available" if records else ("empty_or_admin_only" if table["admin_read_only"] else "empty")
        except ApicQueryError as e:
            self._availability[name] = {403: "permission_denied", 400: "rejected"}.get(e.status, e.code)
            raise
        return self._frame(records, table, fields, now, window, query_id)

    def _frame(self, records, table, fields, now, window, query_id):
        rows = []
        for record in records:
            row = {k: record.get(k) if record.get(k) != "" or table["fields"][k]["type"] == "string" else None for k in fields}
            row.update(_derived(record.get("dn")))
            rows.append(row)
        meta = {
            "_bow_apic": self._apic,
            "_bow_connection": getattr(self, "_bow_connection_id", None),
            "_bow_retrieved_at": now,
            "_bow_query_id": query_id,
            "_bow_class": table["class"],
            "_bow_window_start": window[0] if window else None,
            "_bow_window_end": window[1] if window else None,
            "_bow_contract": _catalog()["sha256"],
        }
        columns = list(dict.fromkeys(fields + list(_DERIVED)))
        frame = pd.DataFrame({k: pd.Series([r.get(k) for r in rows], dtype="object") for k in columns})
        for k in fields:
            typ = table["fields"][k]["type"]
            if typ == "integer":
                numeric = pd.to_numeric(frame[k], errors="coerce")
                if numeric.notna().sum() == frame[k].notna().sum() and (numeric.dropna().abs() < 2**63).all():
                    frame[k] = pd.array(numeric.astype("Float64").round().astype("Int64") if len(numeric) else numeric, dtype="Int64")
                else:
                    frame[k] = frame[k].astype("string")
            elif typ == "number":
                numeric = pd.to_numeric(frame[k], errors="coerce")
                frame[k] = pd.array(numeric, dtype="Float64") if numeric.notna().sum() == frame[k].notna().sum() else frame[k].astype("string")
            elif typ == "datetime":
                parsed = pd.to_datetime(frame[k], utc=True, errors="coerce", format="ISO8601")
                epoch = parsed == pd.Timestamp(0, tz="UTC")
                frame[k] = parsed.mask(epoch)  # 1970-01-01 is the APIC's "never"
            else:
                frame[k] = frame[k].astype("string")
        frame["pod"] = pd.array(frame["pod"], dtype="Int64")
        frame["node"] = pd.array(frame["node"], dtype="Int64")
        frame["parent_dn"] = frame["parent_dn"].astype("string")
        for k, v in meta.items():
            frame[k] = pd.Series([v] * len(frame), dtype="object")
        for k in ("_bow_retrieved_at", "_bow_window_start", "_bow_window_end"):
            frame[k] = pd.to_datetime(frame[k], utc=True)
        return frame
