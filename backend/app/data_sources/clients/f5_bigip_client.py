"""Read-only F5 BIG-IP (iControl REST, TMOS 15.x-17.x) tables for load-balancer incident investigations.

The frozen catalog in f5_bigip/catalog.json is an execution allow-list: the client can
only issue GETs to catalogued collections/objects/stats, a POST to the token login
endpoint and a DELETE of its own token. Token auth follows the iControl REST User
Guide: POST /mgmt/shared/authn/login -> token.token -> X-F5-Auth-Token header ->
DELETE /mgmt/shared/authz/tokens/<token> at the end of every query session.
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
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_progress


class F5BigIpError(ValueError):
    """Stable machine code plus a safe explanation; never echoes credentials or device bodies."""

    def __init__(self, code, message, status=None):
        self.code, self.status = code, status
        self.f5_code = None
        super().__init__(f"{code}: {message}")


_SECRET_KEY = re.compile(
    r"(^|[._-])(password|passphrase|secret|private[_-]?key|access[_-]?key|secret[_-]?key|auth[_-]?key|token)([._-]|$)", re.I
)
_NUMERIC_SEGMENT = re.compile(r"[0-9]+(\.[0-9]+)*")
_FULL_PATH = re.compile(r"^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.:%-]+)+$")
_META = {
    "_bow_device": "str",
    "_bow_connection": "str",
    "_bow_retrieved_at": "datetime",
    "_bow_query_id": "str",
    "_bow_endpoint": "str",
    "_bow_contract": "str",
}
_TYPES = {"integer": "int", "number": "float", "boolean": "bool", "string": "str", "array": "json", "object": "json"}
_PAGE_SIZE = 500
_OPEN_KINDS = {"stats", "nested"}
_LIMIT_MSG = "Result exceeds limit, which is a complete-result ceiling rather than a preview: raise limit (up to max_rows) or narrow filters, then preview with pandas .head(n). Nothing was truncated."


@lru_cache(maxsize=1)
def _catalog():
    catalog = json.loads(Path(__file__).with_name("f5_bigip").joinpath("catalog.json").read_text())
    for name, table in catalog["tables"].items():
        placeholders = re.findall(r"\{([^}]+)\}", table["path"])
        if placeholders != table["parents"] or len(set(placeholders)) != len(placeholders):
            raise F5BigIpError("InvalidCatalog", f"Invalid parent contract for {name}.")
        if not table["path"].startswith("/mgmt/tm/") or "{" in re.sub(r"\{[^}]+\}", "x", table["path"]):
            raise F5BigIpError("InvalidCatalog", f"Malformed path template for {name}.")
        if table["kind"] not in {"collection", "stats", "nested"}:
            raise F5BigIpError("InvalidCatalog", f"Unknown table kind for {name}.")
    return catalog


@lru_cache(maxsize=1)
def _bases():
    """Catalogue resource bases (no object/stats segments) and allowed subcollections; the GET allow-list."""
    bases, subcollections = set(), set()
    for table in _catalog()["tables"].values():
        path = table["path"]
        path = path[: -len("/stats")] if path.endswith("/stats") else path
        if "{" in path:
            parent, sub = re.split(r"/\{[^}]+\}/", path)
            bases.add(parent)
            subcollections.add(parent + "/" + sub)
        else:
            bases.add(path)
    return bases, subcollections


class F5BigIpClient(DataSourceClient):
    # Preserve JSON evidence in the same query history as SQL/string connectors.
    capture_structured_queries = True
    relative_date_hint = (
        "Stats are live snapshots; there is no history. Compare ssl_certs.expirationDate (epoch seconds) "
        "with _bow_retrieved_at for days-to-expiry."
    )

    def __init__(
        self,
        url,
        username,
        password,
        login_provider="tmos",
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
            or parsed.path not in ("", "/mgmt")
        ):
            raise F5BigIpError(
                "InvalidConfiguration",
                "Use the management origin such as https://bigip.example:443, without a path or credentials.",
            )
        if parsed.scheme != "https" and not (allow_http and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise F5BigIpError(
                "InvalidConfiguration", "HTTPS is required. HTTP is only available for a loopback simulator."
            )
        if not username or not password or not isinstance(username, str) or not isinstance(password, str):
            raise F5BigIpError("InvalidConfiguration", "A BIG-IP account with a read-only role is required.")
        if not isinstance(login_provider, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", login_provider):
            raise F5BigIpError("InvalidConfiguration", "login_provider must be a short provider name such as tmos.")
        self.url = url[: -len("/mgmt")] if url.endswith("/mgmt") else url
        self.timeout = self._integer(timeout, "timeout", 1, 120)
        self.max_rows = self._integer(max_rows, "max_rows", 1, 100000)
        self._username, self._password, self._login_provider = username, password, login_provider
        self._verify = True
        self._ca = None
        if ca_certificate:
            self._ca = tempfile.TemporaryDirectory(prefix="bow-f5-ca-")
            cert = Path(self._ca.name) / "ca.pem"
            cert.write_text(ca_certificate)
            cert.chmod(0o600)
            self._verify = str(cert)
        self._lock = threading.RLock()
        self._device = parsed.hostname
        self._availability = {}

    def close(self):
        if self._ca:
            self._ca.cleanup()
            self._ca = None

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise F5BigIpError("InvalidQuery", f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    # ------------------------------------------------------------------ contract
    @property
    def description(self):
        return """F5 BIG-IP load-balancer investigation connector (iControl REST, TMOS 15.x-17.x; read-only; simulator-verified).
Call get_tables()/get_schema(name) for the 26 tables, then execute_query(query) with a JSON object or string. Controls: table, fields, filter, parent, name, limit.
Config tables (ltm/sys/net/cm objects): virtual_servers, pools, pool_members, nodes, irules, profiles_http, profiles_client_ssl, ssl_certs, ltm_monitors_http, cm_devices, net_interfaces, net_vlans, net_self, partitions.
Live status tables (/stats): virtual_server_stats, pool_stats, pool_member_stats, node_stats expose status.availabilityState / status.enabledState / status.statusReason, monitorStatus, sessionStatus and cumulative counters (serverside.*, clientside.*, totRequests); _object holds the object's fullPath.
System tables: sys_version, sys_clock, sys_hardware, sys_cpu, sys_memory, sys_memory_tmm, cm_failover_status, cm_sync_status.
Examples: {"table":"pool_member_stats","filter":{"status.availabilityState":"offline"}} ; {"table":"pool_members","parent":{"pool":"/Common/web_pool"}} ; {"table":"virtual_servers","filter":{"partition":"App1"},"fields":["fullPath","destination","pool","enabled"]} ; {"table":"virtual_server_stats","name":"/Common/vs_web_http"} ; {"table":"ssl_certs","fields":["fullPath","subject","expirationDate","expirationString"]}.
filter: partition is applied server-side ($filter=partition eq X); any other catalogued field is an exact-match (or list membership) filter applied client-side after a complete fetch. There is no comparison operator; compute thresholds in pandas.
parent: pool_members and pool_member_stats take {"pool":"/Partition/name"}; without it the connector crawls every pool (bounded, one request per pool for stats) and adds a `pool` column.
name: fetch one object by fullPath (/Partition/name, or /Partition/node:port for members) instead of the collection. Object names are sent as ~Partition~name.
Results are pandas DataFrames: nested scalars become dotted columns (fqdn.autopopulate, sourceAddressTranslation.type), arrays stay structured (rules, vlans, certKeyChain), plus _bow_* provenance columns. limit is a complete-result ceiling: a larger result raises ResultLimitExceeded instead of truncating.
Joins: virtual_servers.pool -> pools.fullPath; pool_members.pool -> pools.fullPath; nodes.fullPath -> pool_member_stats.nodeName; profiles_client_ssl.cert -> ssl_certs.fullPath; virtual_servers.rules[] -> irules.fullPath; *_stats._object -> fullPath of the config table.
Pitfalls: counters are cumulative since last reset (not rates; take two timestamped samples); a disabled member still reports availabilityState available with enabledState disabled; pools without monitors report unknown, not offline; expirationDate is epoch seconds UTC; sys_cpu/sys_memory key names are flattened from the device and may vary by version.
No SQL, raw URLs, tmsh/bash, transactions or mutations: the client cannot send anything except catalogued GETs and its own token login/logout."""

    def get_tables(self):
        names = list(_catalog()["tables"])
        return [self.get_schema(n) for n in discovery_items(names, "tables", label=str)]

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    def get_schema(self, table_name):
        t = self._table(table_name)
        columns = [
            TableColumn(name=k, dtype=_TYPES.get(v["type"], "json"), description=v.get("description") or None)
            for k, v in t["fields"].items()
        ]
        names = {c.name for c in columns}
        for key in t["parents"]:
            if key not in names:
                columns.append(TableColumn(name=key, dtype="str", description="Parent object fullPath."))
        columns += [TableColumn(name=k, dtype=v) for k, v in _META.items()]
        fks = []
        refs = {
            "pool": ("pools", "fullPath"),
            "cert": ("ssl_certs", "fullPath"),
            "nodeName": ("nodes", "fullPath"),
            "poolName": ("pools", "fullPath"),
            "vlan": ("net_vlans", "fullPath"),
        }
        if t["kind"] == "stats":
            base = {
                "virtual_server_stats": "virtual_servers",
                "pool_stats": "pools",
                "pool_member_stats": "pool_members",
                "node_stats": "nodes",
            }.get(table_name)
            if base:
                refs = {**refs, "_object": (base, "fullPath")}
        for c in columns:
            if c.name in refs and refs[c.name][0] != table_name and refs[c.name][0] in _catalog()["tables"]:
                fks.append(
                    ForeignKey(
                        column=c,
                        references_name=refs[c.name][0],
                        references_column=TableColumn(name=refs[c.name][1], dtype="str"),
                    )
                )
        desc = f"{t['description']} GET {t['path']}."
        if t["parents"]:
            desc += " Optional parent: " + ", ".join(t["parents"]) + " (crawled when omitted)."
        if t["kind"] == "collection":
            desc += " Supports filter.partition server-side, fields ($select), name (single object), $top/$skip paging."
        elif t["kind"] == "stats":
            desc += " Live snapshot keyed by object; counters are cumulative."
        if t.get("verified") != "verified":
            desc += f" Shape {t['verified']}: {t.get('notes') or 'see SOURCES.md'}"
        return Table(
            name=table_name,
            description=desc,
            columns=columns,
            pks=[c for c in columns if c.name in {"fullPath", "_object"}][:1],
            fks=fks,
            metadata_json={
                "f5_bigip": {
                    "endpoint": t["path"],
                    "kind": t["kind"],
                    "parents": t["parents"],
                    "object_scoped": t["object_scoped"],
                    "filters": ["partition"] + list(t["fields"]),
                    "availability": self._availability.get(table_name, "unverified"),
                    "contract": _catalog()["contract"],
                    "verified": t.get("verified", "verified"),
                }
            },
        )

    def prompt_schema(self):
        return "\n".join(
            f"{n}: {t['description'][:150]} | kind={t['kind']} | parents={','.join(t['parents']) or 'none'}"
            for n, t in _catalog()["tables"].items()
        )

    @staticmethod
    def _table(name):
        if not isinstance(name, str) or name not in _catalog()["tables"]:
            raise F5BigIpError("InvalidQuery", "Unknown table; use get_tables to discover the catalogued tables.")
        return _catalog()["tables"][name]

    def test_connection(self):
        version = self.execute_query({"table": "sys_version"})
        if version.empty or not isinstance(version.iloc[0].get("Version"), str):
            raise F5BigIpError("InvalidResponse", "sys/version did not return a TMOS version.")
        tmos = version.iloc[0]["Version"]
        major = tmos.split(".")[0]
        if not major.isdigit() or int(major) < 13:
            raise F5BigIpError(
                "UnsupportedVersion", "This connector requires TMOS 13.1 or later with iControl REST token auth."
            )
        note = "" if 15 <= int(major) <= 17 else " (outside the verified 15.x-17.x range)"
        device = ""
        try:
            devices = self.execute_query(
                {"table": "cm_devices", "fields": ["hostname", "failoverState", "selfDevice"], "limit": 100}
            )
            own = devices[devices["selfDevice"] == "true"]
            if not own.empty:
                self._device = str(own.iloc[0]["hostname"])
                device = f" {self._device} ({own.iloc[0]['failoverState']})"
        except F5BigIpError as e:
            if e.code not in {"PermissionDenied", "UnsupportedResource"}:
                raise
        return {
            "success": True,
            "message": f"Connected to F5 BIG-IP {tmos}{note}{device}. {len(_catalog()['tables'])} read-only tables available.",
        }

    # ------------------------------------------------------------------ queries
    @staticmethod
    def _object_path(value, what="name"):
        if not isinstance(value, str) or not _FULL_PATH.fullmatch(value) or ".." in value.split("/") or "." in value.split("/"):
            raise F5BigIpError("InvalidQuery", f"{what} must be a partition-qualified path such as /Common/name.")
        return quote(value.replace("/", "~"), safe="~:")

    def execute_query(self, query):
        try:
            spec = json.loads(query) if isinstance(query, str) else query.copy() if isinstance(query, dict) else None
        except (ValueError, TypeError):
            raise F5BigIpError("InvalidQuery", "Query must be a JSON object.") from None
        allowed = {"table", "fields", "filter", "parent", "name", "limit"}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise F5BigIpError("InvalidQuery", "Unknown query controls; use table, fields, filter, parent, name, limit.")
        table_name = spec.get("table")
        t = self._table(table_name)
        limit = self._integer(spec.get("limit", self.max_rows), "limit", 1, self.max_rows)
        fields = spec.get("fields")
        if fields is not None:
            if not isinstance(fields, list) or not fields or not all(isinstance(x, str) for x in fields):
                raise F5BigIpError("InvalidQuery", "fields must be a nonempty list of catalogued field names.")
            # Provenance columns are always appended; asking for them is harmless.
            fields = [f for f in fields if f not in _META]
            unknown = [f for f in fields if f not in t["fields"] and f not in t["parents"]]
            if t["kind"] in _OPEN_KINDS:
                # /stats and nested tables return whatever keys the device has; an
                # undocumented key becomes a null column instead of a rejection.
                unknown = [f for f in unknown if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", f)]
            if unknown:
                raise F5BigIpError("InvalidQuery", f"Unknown or excluded field: {unknown[0]}. Use get_schema(table) for the documented columns.")
            fields = list(dict.fromkeys(fields)) or None
        filters = spec.get("filter", {})
        if not isinstance(filters, dict):
            raise F5BigIpError("InvalidQuery", "filter must be an object of field: value.")
        for k, v in filters.items():
            if k != "partition" and k not in t["fields"] and k not in t["parents"] and not (
                t["kind"] in _OPEN_KINDS and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", k)
            ):
                raise F5BigIpError("InvalidQuery", f"Unknown or excluded filter: {k}. Use get_schema(table) for the documented columns.")
            values = v if isinstance(v, list) else [v]
            if not values or any(
                isinstance(x, (dict, list)) or (isinstance(x, float) and not math.isfinite(x)) for x in values
            ):
                raise F5BigIpError("InvalidQuery", "Filter values must be scalars or a list of scalars.")
        partition = filters.get("partition")
        if partition is not None and (not isinstance(partition, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", partition)):
            raise F5BigIpError("InvalidQuery", "partition must be a partition name such as Common.")
        parents = spec.get("parent", {})
        if not isinstance(parents, dict) or set(parents) - set(t["parents"]):
            raise F5BigIpError("InvalidQuery", "parent must only name the parents listed by get_schema.")
        parent_paths = {k: self._object_path(v, f"parent {k}") for k, v in parents.items()}
        name = spec.get("name")
        if name is not None:
            if not t["object_scoped"]:
                raise F5BigIpError("InvalidQuery", "This table has no per-object endpoint; omit name.")
            if t["parents"] and not parent_paths:
                raise F5BigIpError("InvalidQuery", "name on a member table requires its parent pool.")
            name_path = self._object_path(name)
        else:
            name_path = None

        now = datetime.now(UTC)
        query_id = str(uuid.uuid4())
        with self._lock, self._session() as session:
            deadline = time.monotonic() + 120
            if t["kind"] == "collection":
                records = self._collection(session, t, fields, partition, parent_paths, name_path, limit, deadline)
            elif t["kind"] == "stats":
                records = self._stats(session, t, parent_paths, name_path, limit, deadline)
            else:
                records = self._nested(session, t, deadline)
        self._availability[table_name] = "available" if records else "empty"

        # Client-side exact-match filters (partition on non-collection tables included).
        rows = []
        for record in records:
            flat = self._flatten(self._sanitize(record))
            if "partition" not in flat and isinstance(flat.get("_object"), str):
                flat["partition"] = flat["_object"].split("/")[1] if flat["_object"].count("/") >= 2 else None
            keep = True
            for k, v in filters.items():
                if k == "partition" and t["kind"] == "collection" and not (t["parents"] and not parents):
                    continue  # already applied server-side via $filter
                wanted = v if isinstance(v, list) else [v]
                keep = keep and any(self._same(flat.get(k), w) for w in wanted)
            if keep:
                rows.append(flat)
        if len(rows) > limit:
            raise F5BigIpError("ResultLimitExceeded", _LIMIT_MSG)

        declared = list(t["fields"])
        columns = fields or declared
        if t["kind"] in _OPEN_KINDS and not fields:
            extra = sorted({k for r in rows for k in r if k not in declared and k != "partition"})
            columns = declared + extra
        for key in t["parents"]:
            if key not in columns:
                columns.append(key)
        columns = list(dict.fromkeys(columns + list(_META)))
        for row in rows:
            row.update(
                _bow_device=self._device,
                _bow_connection=getattr(self, "_bow_connection_id", None),
                _bow_retrieved_at=now.isoformat(),
                _bow_query_id=query_id,
                _bow_endpoint=t["path"],
                _bow_contract=_catalog()["sha256"],
            )
        frame = pd.DataFrame({k: pd.Series([r.get(k) for r in rows], dtype="object") for k in columns})
        for k in columns:
            typ = t["fields"].get(k, {}).get("type")
            if typ is None and t["kind"] in _OPEN_KINDS and k not in _META:
                values = [v for v in frame[k] if v is not None]
                typ = "integer" if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values) else None
            try:
                if typ == "integer":
                    frame[k] = pd.array(
                        [self._int(v) for v in frame[k]],
                        dtype="UInt64" if any(isinstance(v, int) and v > 2**63 - 1 for v in frame[k]) else "Int64",
                    )
                elif typ == "number":
                    frame[k] = pd.array(frame[k], dtype="Float64")
                elif typ == "boolean":
                    frame[k] = pd.array([self._bool(v) for v in frame[k]], dtype="boolean")
                elif typ == "string":
                    frame[k] = pd.array([None if v is None else str(v) for v in frame[k]], dtype="string")
            except (ValueError, TypeError, OverflowError):
                raise F5BigIpError("InvalidResponse", f"Invalid value type for {k}.") from None
        frame["_bow_retrieved_at"] = pd.to_datetime(frame["_bow_retrieved_at"], utc=True)
        return frame

    @staticmethod
    def _int(v):
        if v is None or isinstance(v, bool):
            return None if v is None else int(v)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and re.fullmatch(r"-?[0-9]+", v.strip()):
            return int(v)
        if isinstance(v, float) and v.is_integer():
            return int(v)
        raise ValueError(v)

    @staticmethod
    def _bool(v):
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in {"true", "false", "yes", "no", "enabled", "disabled"}:
            return v.lower() in {"true", "yes", "enabled"}
        raise ValueError(v)

    @staticmethod
    def _same(actual, wanted):
        if actual is None:
            return False
        if isinstance(actual, bool) or isinstance(wanted, bool):
            return actual is wanted or (isinstance(actual, str) and actual.lower() == str(wanted).lower())
        if isinstance(actual, (int, float)) and isinstance(wanted, (int, float)):
            return actual == wanted
        return str(actual) == str(wanted)

    # ------------------------------------------------------------------ readers
    def _collection(self, session, t, fields, partition, parent_paths, name_path, limit, deadline):
        path = t["path"]
        for k, v in parent_paths.items():
            path = path.replace("{" + k + "}", v)
        parent_values = {k: v.replace("~", "/") for k, v in parent_paths.items()}
        params = {}
        if fields:
            top = [f.split(".")[0] for f in fields if f not in t["parents"]]
            params["$select"] = ",".join(dict.fromkeys(["name", "partition", "fullPath"] + top))
        if name_path:
            body = self._get(session, path + "/" + name_path, params, deadline)
            if not isinstance(body, dict) or "items" in body:
                raise F5BigIpError("InvalidResponse", "Expected a single object.")
            return [{**body, **parent_values}]
        if t["parents"] and not parent_paths:
            # Crawl: expand the parent collection's subcollection in bounded pages.
            parent_table = next(
                x for x in _catalog()["tables"].values() if x["kind"] == "collection" and x["path"] == path.split("/{")[0]
            )
            # The partition filter is applied to member rows client-side (see execute_query).
            crawl_params = {"expandSubcollections": "true"}
            records = []
            for parent in self._pages(session, parent_table["path"], crawl_params, self.max_rows, deadline):
                ref = parent.get("membersReference") if isinstance(parent, dict) else None
                items = ref.get("items", []) if isinstance(ref, dict) else []
                if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                    raise F5BigIpError("InvalidResponse", "Expanded subcollection is not a list of objects.")
                records.extend({**i, "pool": parent.get("fullPath")} for i in items)
                if len(records) > limit:
                    raise F5BigIpError("ResultLimitExceeded", _LIMIT_MSG)
            return records
        if partition:
            params["$filter"] = f"partition eq {partition}"
        return [{**r, **parent_values} for r in self._pages(session, path, params, limit, deadline)]

    def _stats(self, session, t, parent_paths, name_path, limit, deadline):
        path = t["path"]
        for k, v in parent_paths.items():
            path = path.replace("{" + k + "}", v)
        parent_values = {k: v.replace("~", "/") for k, v in parent_paths.items()}
        if t["parents"] and not parent_paths:
            parent_table = next(
                x for x in _catalog()["tables"].values() if x["kind"] == "collection" and x["path"] == path.split("/{")[0]
            )
            records = []
            for parent in self._pages(session, parent_table["path"], {"$select": "fullPath"}, self.max_rows, deadline):
                full = parent.get("fullPath")
                if not isinstance(full, str):
                    raise F5BigIpError("InvalidResponse", "Parent object has no fullPath.")
                sub = t["path"].replace("{" + t["parents"][0] + "}", self._object_path(full, "pool"))
                body = self._get(session, sub, {}, deadline)
                records.extend({**r, t["parents"][0]: full} for r in self._stats_rows(body))
                if len(records) > limit:
                    raise F5BigIpError("ResultLimitExceeded", _LIMIT_MSG)
            return records
        if name_path:
            base = path[: -len("/stats")]
            path = base + "/" + name_path + "/stats"
        body = self._get(session, path, {}, deadline)
        records = [{**r, **parent_values} for r in self._stats_rows(body)]
        if len(records) > limit:
            raise F5BigIpError("ResultLimitExceeded", _LIMIT_MSG)
        return records

    def _nested(self, session, t, deadline):
        body = self._get(session, t["path"], {}, deadline)
        tree = self._tree(self._entries(body), urlsplit(t["path"]).path)
        if t.get("stats_rows"):
            return self._rows(tree, t["stats_rows"], {})
        return [self._collapse(tree)]

    # ------------------------------------------------------------------ stats/nested shaping
    @staticmethod
    def _entries(body):
        entries = body.get("entries") if isinstance(body, dict) else None
        if not isinstance(entries, dict):
            raise F5BigIpError("InvalidResponse", "Expected a stats response with entries.")
        return entries

    @staticmethod
    def _leaf(value):
        if isinstance(value, dict):
            if "value" in value:
                return value["value"]
            if "description" in value:
                return value["description"]
        return value

    def _stats_rows(self, body):
        rows = []
        for key, val in self._entries(body).items():
            nested = val.get("nestedStats", {}).get("entries") if isinstance(val, dict) else None
            if not isinstance(nested, dict):
                raise F5BigIpError("InvalidResponse", "Stats entry without nestedStats.")
            row = {k: self._leaf(v) for k, v in nested.items() if not (isinstance(v, dict) and "nestedStats" in v)}
            segment = next((s for s in reversed(urlsplit(key).path.split("/")) if s.startswith("~")), None)
            row["_object"] = segment.replace("~", "/") if segment else row.get("tmName")
            rows.append(row)
        return rows

    def _tree(self, entries, parent_path):
        """Turn URL-keyed nestedStats into a dict tree; numeric path segments become __items__ lists."""
        out = {}
        for key, val in entries.items():
            if isinstance(val, dict) and "nestedStats" in val:
                path = urlsplit(key).path
                rel = path[len(parent_path) + 1 :].split("/") if path.startswith(parent_path + "/") else [path.rsplit("/", 1)[-1]]
                names = [s for s in rel if not _NUMERIC_SEGMENT.fullmatch(s)]
                child = self._tree(val["nestedStats"].get("entries", {}) or {}, path)
                if not names:
                    out.setdefault("__items__", []).append(child)
                    continue
                node = out
                for n in names[:-1]:
                    node = node.setdefault(n, {})
                existing = node.get(names[-1])
                if isinstance(existing, dict):
                    for k, v in child.items():
                        if k == "__items__":
                            existing.setdefault("__items__", []).extend(v)
                        else:
                            existing[k] = v
                else:
                    node[names[-1]] = child
            else:
                out[key] = self._leaf(val)
        return out

    def _collapse(self, node, prefix=""):
        """Flatten a tree to dotted columns; single __items__ merge up, multi become arrays."""
        items = node.get("__items__")
        group = prefix.rstrip(".").rsplit(".", 1)[-1]
        # A list whose members each carry one leaf named after the group is a value list
        # (failover-status details); otherwise a single indexed child merges into its group.
        if items is not None and len(node) == 1:
            values = [self._collapse(i) for i in items]
            if values and all(list(x) == [group] for x in values):
                return {group: [x[group] for x in values]} if prefix else {"items": [x[group] for x in values]}
            if len(items) == 1:
                return self._collapse(items[0], prefix)
        out = {}
        for k, v in node.items():
            if k == "__items__":
                values = [self._collapse(i) for i in v]
                if values and all(len(x) == 1 for x in values) and len({next(iter(x)) for x in values}) == 1:
                    values = [next(iter(x.values())) for x in values]
                out[prefix.rstrip(".") or "items"] = values
            elif isinstance(v, dict):
                out.update(self._collapse(v, prefix + k + "."))
            else:
                out[prefix + k] = v
        return out

    def _rows(self, node, group, inherited):
        scalars = {k: v for k, v in node.items() if k != "__items__" and not isinstance(v, dict)}
        inherited = {**inherited, **scalars}
        rows = []
        if group in node and isinstance(node[group], dict):
            for item in node[group].get("__items__", []):
                rows.append({**inherited, **self._collapse(item)})
        for k, v in node.items():
            if isinstance(v, dict) and k != group:
                rows.extend(self._rows(v, group, inherited))
        for item in node.get("__items__", []):
            rows.extend(self._rows(item, group, inherited))
        return rows

    @staticmethod
    def _flatten(record, prefix=""):
        out = {}
        for k, v in record.items():
            if isinstance(v, dict) and k != "__items__":
                out.update(F5BigIpClient._flatten(v, prefix + k + "."))
            else:
                out[prefix + k] = v
        return out

    @staticmethod
    def _sanitize(value):
        if isinstance(value, dict):
            return {k: F5BigIpClient._sanitize(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
        if isinstance(value, list):
            return [F5BigIpClient._sanitize(v) for v in value]
        return value

    # ------------------------------------------------------------------ HTTP
    @contextmanager
    def _session(self):
        """Token login, yield, guaranteed logout (DELETE own token)."""
        with requests.Session() as session:
            session.trust_env = False
            session.verify = self._verify
            session.headers.update({"Accept": "application/json", "User-Agent": "bagofwords-f5-bigip/1"})
            self._login(session)
            try:
                yield session
            finally:
                token = session.headers.pop("X-F5-Auth-Token", None)
                if token:
                    # Logout failures never mask a query result; the token also lapses server-side.
                    with suppress(F5BigIpError):
                        self._send(
                            session,
                            "DELETE",
                            _catalog()["token_path"] + "/" + token,
                            None,
                            None,
                            time.monotonic() + 5,
                            {"X-F5-Auth-Token": token},
                        )

    def _login(self, session):
        session.headers.pop("X-F5-Auth-Token", None)
        body = self._send(
            session,
            "POST",
            _catalog()["login_path"],
            None,
            {"username": self._username, "password": self._password, "loginProviderName": self._login_provider},
            time.monotonic() + self.timeout + 5,
        )
        token = body.get("token", {}).get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token):
            raise F5BigIpError("InvalidResponse", "Login did not return an X-F5-Auth-Token.")
        session.headers["X-F5-Auth-Token"] = token

    def _authorize(self, method, path):
        """Allow-list: catalogued GETs, token login POST, own-token DELETE. Nothing else can be sent."""
        if method == "POST":
            return path == _catalog()["login_path"]
        if method == "DELETE":
            return bool(re.fullmatch(re.escape(_catalog()["token_path"]) + r"/[A-Za-z0-9_-]{8,128}", path))
        if method != "GET" or not path.startswith("/mgmt/tm/") or "?" in path or "#" in path:
            return False
        segments = path.split("/")[1:]
        if segments[-1] == "stats":
            segments = segments[:-1]
        base = "/" + "/".join(s for s in segments if not s.startswith("~"))
        bases, subcollections = _bases()
        return base in bases or (base in subcollections and any(s.startswith("~") for s in segments))

    def _get(self, session, path, params, deadline):
        return self._send(session, "GET", path, params, None, deadline)

    def _send(self, session, method, path, params, payload, deadline, headers=None, relogin=True):
        if not self._authorize(method, path):
            raise F5BigIpError("ForbiddenOperation", "The request is outside the read-only allow-list; nothing was sent.")
        url = self.url + path + ("?" + urlencode(params, safe="$,") if params else "")
        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise F5BigIpError("BudgetExceeded", "Query deadline exhausted.")
            try:
                with session.request(
                    method,
                    url,
                    json=payload,
                    headers=headers,
                    timeout=min(self.timeout, remaining),
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    status = response.status_code
                    if status in {429, 502, 503, 504} and attempt < 3 and method == "GET":
                        retry = response.headers.get("Retry-After", "")
                        delay = float(retry) if retry.isdigit() else 0.25 * 2**attempt
                        if delay >= deadline - time.monotonic():
                            raise F5BigIpError("BudgetExceeded", "Retry exceeds query deadline.")
                        time.sleep(delay)
                        continue
                    if status == 401 and method == "GET" and relogin and "X-F5-Auth-Token" in session.headers:
                        # Token expired or was revoked: re-login once, then retry the same GET once.
                        self._login(session)
                        return self._send(session, method, path, params, payload, deadline, headers, relogin=False)
                    if status != 200:
                        code = {
                            400: "InvalidRequest",
                            401: "AuthenticationFailed",
                            403: "PermissionDenied",
                            404: "UnsupportedResource",
                            429: "Throttled",
                        }.get(status, "HttpError")
                        error = F5BigIpError(
                            code, f"BIG-IP returned HTTP {status}. Check the account role and the object path.", status
                        )
                        # Keep the numeric iControl code; never echo the message (it embeds paths/users).
                        try:
                            raw = json.loads(response.raw.read(8192))
                            if isinstance(raw.get("code"), int):
                                error.f5_code = raw["code"]
                        except (ValueError, AttributeError, TypeError):
                            pass
                        raise error
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 32 * 1024 * 1024:
                            raise F5BigIpError("BudgetExceeded", "Response exceeds 32 MiB.")
                        if time.monotonic() > deadline:
                            raise F5BigIpError("BudgetExceeded", "Query deadline exhausted.")
                    try:
                        return json.loads(body) if body else {}
                    except (ValueError, UnicodeError):
                        raise F5BigIpError("InvalidResponse", "BIG-IP did not return valid JSON.") from None
            except requests.exceptions.SSLError:
                raise F5BigIpError("TLSFailed", "Certificate verification failed; configure the trusted CA.") from None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 3 or method != "GET":
                    raise F5BigIpError("TransportFailed", "BIG-IP could not be reached within the retry budget.") from None
                time.sleep(min(0.25 * 2**attempt, max(0, deadline - time.monotonic())))
        raise F5BigIpError("BudgetExceeded", "Retry budget exhausted.")

    def _pages(self, session, path, params, limit, deadline):
        """$top/$skip paging with same-resource continuation checks; nextLink host is never followed."""
        params = {**params, "$top": min(_PAGE_SIZE, limit)}
        skip = 0
        records = []
        for _ in range(1000):
            page_params = {**params, **({"$skip": skip} if skip else {})}
            body = self._get(session, path, page_params, deadline)
            if not isinstance(body, dict) or "code" in body:
                raise F5BigIpError("InvalidResponse", "BIG-IP returned an incomplete or invalid collection.")
            items = body.get("items", [])
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise F5BigIpError("InvalidResponse", "Expected a collection of objects.")
            records.extend(items)
            if len(records) > limit:
                raise F5BigIpError("ResultLimitExceeded", _LIMIT_MSG)
            nxt = body.get("nextLink")
            if nxt is None or not items:
                return records
            if not isinstance(nxt, str):
                raise F5BigIpError("InvalidResponse", "Invalid pagination continuation.")
            parsed = urlsplit(nxt)
            if parsed.path != path or parsed.fragment:
                raise F5BigIpError("InvalidResponse", "Continuation escaped the selected resource.")
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            nxt_params = dict(pairs)
            if len(nxt_params) != len(pairs) or set(nxt_params) - set(params) - {"$skip", "ver"}:
                raise F5BigIpError("InvalidResponse", "Continuation introduced unsupported query controls.")
            for key, val in params.items():
                if key in nxt_params and nxt_params[key].replace("+", " ") != str(val).replace("+", " "):
                    raise F5BigIpError("InvalidResponse", "Continuation changed query scope.")
            try:
                next_skip = int(nxt_params.get("$skip", "0"))
            except ValueError:
                raise F5BigIpError("InvalidResponse", "Invalid pagination continuation.") from None
            if next_skip <= skip:
                raise F5BigIpError("InvalidResponse", "Repeated pagination continuation.")
            skip = next_skip
        raise F5BigIpError("BudgetExceeded", "Pagination budget exhausted.")
