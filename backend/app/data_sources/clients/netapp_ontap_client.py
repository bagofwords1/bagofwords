"""Read-only ONTAP 9.14.1 diagnostic tables for root-cause investigations.

No MCP dependency. The frozen Swagger catalog is an execution allowlist, not a
claim of availability on a particular appliance. All network I/O is GET-only.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import discovery_items, discovery_progress


class OntapQueryError(ValueError):
    """Stable machine code plus a safe explanation; never includes credentials."""

    def __init__(self, code, message, status=None):
        self.code, self.status = code, status
        self.ontap_code = None
        super().__init__(f"{code}: {message}")


@lru_cache(maxsize=1)
def _catalog():
    catalog = json.loads(Path(__file__).with_name("netapp").joinpath("catalog.json").read_text())
    for name, resource in catalog["resources"].items():
        placeholders = re.findall(r"\{([^}]+)\}", resource["path"])
        if set(placeholders) != set(resource["parents"]) or len(resource["parents"]) != len(set(resource["parents"])):
            raise OntapQueryError("InvalidCatalog", f"Invalid parent contract for {name}.")
        bound = re.sub(r"\{[^}]+\}", "bound", resource["path"])
        if "{" in bound or "}" in bound:
            raise OntapQueryError("InvalidCatalog", f"Malformed path template for {name}.")
        if resource["history"] and (
            not {"timestamp", "time"}.intersection(resource["filters"]) or "interval" not in resource["filters"]
        ):
            raise OntapQueryError("InvalidCatalog", f"Invalid historical time contract for {name}.")
    catalog["resources"]["volume_constituents"] = {
        **catalog["resources"]["volumes"],
        "description": "FlexGroup constituent volumes; physical layout diagnostics, excluded from logical volume totals.",
    }
    return catalog


_TYPES = {"integer": "int", "number": "float", "boolean": "bool", "string": "str", "array": "json", "object": "json"}
_KEYS = {
    "uuid": "uuid",
    "svm.uuid": "svms",
    "node.uuid": "nodes",
    "volume.uuid": "volumes",
    "location.volume.uuid": "volumes",
    "aggregate.uuid": "aggregates",
    "lun.uuid": "luns",
    "igroup.uuid": "igroups",
}
_META = {
    "_bow_cluster": "str",
    "_bow_connection": "str",
    "_bow_retrieved_at": "datetime",
    "_bow_query_id": "str",
    "_bow_endpoint": "str",
    "_bow_contract": "str",
}
_TIERS = [("1h", 3600), ("1d", 86400), ("1w", 604800), ("1m", 2592000), ("1y", 31536000)]


class NetAppOntapClient(DataSourceClient):
    # Preserve JSON evidence in the same query history as SQL/string connectors.
    capture_structured_queries = True
    relative_date_hint = 'Use JSON lookback, e.g. "lookback":"24h"; resolved at each execution. Do not freeze dates in saved investigations.'

    def __init__(
        self, url, username, password, ca_certificate=None, allow_http=False, timeout=30, max_rows=10000, **kwargs
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
            raise OntapQueryError(
                "InvalidConfiguration",
                "Use a management origin such as https://cluster.example:443, without a path or credentials.",
            )
        if parsed.scheme != "https" and not allow_http:
            raise OntapQueryError(
                "InvalidConfiguration",
                "HTTPS is required. HTTP is only available through the explicit simulator option.",
            )
        if not username or not password:
            raise OntapQueryError("InvalidConfiguration", "An HTTP-enabled read-only account is required.")
        self.url = url
        self.timeout = self._integer(timeout, "timeout", 1, 120)
        self.max_rows = self._integer(max_rows, "max_rows", 1, 100000)
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.auth = (username, password)
        self._session.headers.update({"Accept": "application/json", "User-Agent": "bagofwords-netapp/1"})
        self._ca = None
        if ca_certificate:
            self._ca = tempfile.TemporaryDirectory(prefix="bow-ontap-ca-")
            cert = Path(self._ca.name) / "ca.pem"
            cert.write_text(ca_certificate)
            cert.chmod(0o600)
            self._session.verify = str(cert)
        self._lock = threading.RLock()
        self._availability = {}
        self._cluster = parsed.hostname

    def close(self):
        self._session.close()
        if self._ca:
            self._ca.cleanup()

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise OntapQueryError("InvalidQuery", f"{name} must be an integer from {minimum} to {maximum}.")
        return value

    @property
    def description(self):
        return """NetApp ONTAP root-cause investigation connector (REST 9.14.1 contract; live coverage depends on the appliance).
Call get_tables()/get_schema(name) to discover curated and diag_ tables, then execute_query(query) with a JSON object/string.
Example: {"table":"volumes","fields":["uuid","name","space.used","space.size"],"filter":{"state":"online"},"limit":500}.
Parent-scoped example: {"table":"volume_metrics","parent":{"volume.uuid":"UUID"},"lookback":"24h","interval":"auto"}.
Filter equality uses scalar values; explicit comparisons use {"op":"gt","value":10}; operators eq,ne,gt,ge,lt,le,like,in are supported only for documented fields.
Results are pandas DataFrames with dotted columns, structured arrays, nullable types, source and retrieval provenance. limit is a complete-result ceiling: narrow the query if exceeded.
No SQL, arbitrary URLs, mutations or automatic parent crawling. Retrieve parent UUIDs first; identifiers require cluster/SVM context. Every diagnostic table follows the same restrictions.
Join and aggregate in pandas. Preserve units and timestamp/duration/status, do not treat missing metrics as zero, and do not multiply logical volume totals by constituents or aggregate membership.
Current topology does not prove historical placement; events and correlation do not prove causes. Native performance history does not supply historical capacity/configuration.
Counter tables expose raw samples and definitions: use counter metadata and two timestamped samples where required, not an assumed IOPS formula. Missing permission/history is a gap, never evidence of health.
Use coverage_report() for documented exclusions and observed availability; this connector has simulated-API verification, not appliance certification."""

    def get_tables(self):
        # The catalog is a local JSON contract, so every table is known up front
        # and discovery reports a bounded count rather than an open spinner.
        names = list(_catalog()["resources"]) + ["volume_aggregates"]
        return [self.get_schema(n) for n in discovery_items(names, "tables", label=str)]

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        return self.get_tables()

    def get_schema(self, table_name):
        if table_name == "volume_aggregates":
            return Table(
                name=table_name,
                description="Volume to aggregate membership; never sum volume capacity after this join.",
                columns=[TableColumn(name=k, dtype="str") for k in ["volume.uuid", "aggregate.uuid", "aggregate.name"]]
                + [TableColumn(name=k, dtype=v) for k, v in _META.items()],
                pks=[],
                fks=[
                    ForeignKey(
                        column=TableColumn(name="volume.uuid", dtype="str"),
                        references_name="volumes",
                        references_column=TableColumn(name="uuid", dtype="str"),
                    )
                ],
            )
        r = self._resource(table_name)
        columns = [
            TableColumn(name=k, dtype=_TYPES.get(v["type"], "json"), description=v.get("description"))
            for k, v in r["fields"].items()
        ]
        names = {c.name for c in columns}
        for key in r["parents"]:
            if key not in names:
                columns.append(
                    TableColumn(name=key, dtype="str", description="Explicit parent identity for this query.")
                )
        columns += [TableColumn(name=k, dtype=v) for k, v in _META.items()]
        meta = {
            "endpoint": "/api" + r["path"],
            "parents": r["parents"],
            "parent_types": r["parent_types"],
            "controls": r["controls"],
            "time_fields": r["time_fields"],
            "filters": r["filters"],
            "availability": self._availability.get(table_name, "unverified"),
            "contract": "9.14.1",
            "history": r["history"],
            "curated": r["curated"],
            "collection": r["collection"],
        }
        desc = f"{r['description']} GET /api{r['path']}. "
        if r["parents"]:
            desc += "Required parent: " + ", ".join(r["parents"]) + ". "
        if r["history"]:
            desc += "Retained performance; provide lookback or UTC start_time/end_time. Units/status/duration must be preserved. "
        desc += "Availability is unverified until queried. Nested arrays remain structured; scalar columns use dotted names."
        return Table(
            name=table_name,
            description=desc,
            columns=columns,
            pks=[c for c in columns if c.name == "uuid"],
            fks=[
                ForeignKey(
                    column=c, references_name=_KEYS[c.name], references_column=TableColumn(name="uuid", dtype="str")
                )
                for c in columns
                if c.name in _KEYS and c.name != "uuid"
            ],
            metadata_json={"netapp": meta},
        )

    def prompt_schema(self):
        # Table search/get_schema supplies full fields; avoid a multi-megabyte prompt.
        return "\n".join(
            f"{n}: {r['description'][:160]} | parents={','.join(r['parents']) or 'none'}"
            for n, r in _catalog()["resources"].items()
        )

    def coverage_report(self):
        return {
            "contract": "9.14.1",
            "sha256": _catalog()["sha256"],
            "validation": "awaiting_customer_validation",
            "resources": [
                {**x, "availability": self._availability.get(x["table"], "unverified")} for x in _catalog()["coverage"]
            ],
            "gaps": [
                "Private CLI diagnostics (including additional cluster QoS policies) are excluded.",
                "Historical capacity/configuration requires retained external evidence.",
                "Physical hardware, FC and exact 9.14.1P9 behavior need customer validation.",
            ],
        }

    @staticmethod
    def _resource(name):
        if not isinstance(name, str) or name not in _catalog()["resources"]:
            raise OntapQueryError("InvalidQuery", "Unknown table; use get_tables to discover reviewed resources.")
        return _catalog()["resources"][name]

    def test_connection(self):
        frame = self.execute_query({"table": "cluster", "fields": ["uuid", "name", "version"]})
        if frame.empty:
            raise OntapQueryError("InvalidResponse", "Cluster identity is absent.")
        version = frame.iloc[0].get("version.full")
        generation = frame.iloc[0].get("version.generation")
        major = frame.iloc[0].get("version.major")
        if pd.isna(generation) or pd.isna(major) or generation != 9 or major != 14:
            raise OntapQueryError(
                "UnsupportedVersion",
                "This connector requires the ONTAP 9.14 release family; verify the appliance version.",
            )
        identity = frame.iloc[0].get("uuid")
        if not isinstance(identity, str) or not identity.strip():
            raise OntapQueryError("InvalidResponse", "Cluster identity is absent.")
        self._cluster = identity
        return {
            "success": True,
            "message": f"Connected to ONTAP {version if isinstance(version, str) else '9.14.1'}. Diagnostic coverage is verified per query; full customer validation remains required.",
        }

    @staticmethod
    def _expand(requested, resource):
        if not isinstance(requested, list) or not requested or not all(isinstance(x, str) for x in requested):
            raise OntapQueryError("InvalidQuery", "fields must be a nonempty list of documented field names.")
        expanded = []
        for name in requested:
            matches = [k for k in resource["fields"] if k == name or k.startswith(name + ".")]
            if not matches:
                raise OntapQueryError("InvalidQuery", f"Unknown or excluded field: {name}.")
            expanded.extend(matches)
        return list(dict.fromkeys(expanded))

    @staticmethod
    def _value(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (int, float)) and math.isfinite(value):
            return str(value)
        if not isinstance(value, str):
            raise OntapQueryError("InvalidQuery", "Filter values must be finite scalars.")
        if any(c in value for c in ["\x00", "\n", "\r"]):
            raise OntapQueryError("InvalidQuery", "Invalid control character.")
        # ONTAP double quotes select literal matching, avoiding wildcard/operator injection.
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _filter(self, value):
        if not isinstance(value, dict):
            return self._value(value)
        if set(value) != {"op", "value"}:
            raise OntapQueryError("InvalidQuery", "Use op and value in a comparison.")
        op, val = value["op"], value["value"]
        if not isinstance(op, str):
            raise OntapQueryError("InvalidQuery", "Comparison operator must be a string.")
        if op == "in":
            if not isinstance(val, list) or not 1 <= len(val) <= 100:
                raise OntapQueryError("InvalidQuery", "in requires 1–100 values.")
            return "|".join(self._value(x) for x in val)
        if op == "like":
            if not isinstance(val, str) or any(c in val for c in '|<>!=\r\n\x00"'):
                raise OntapQueryError("InvalidQuery", "Invalid wildcard expression.")
            return val
        prefix = {"eq": "", "ne": "!", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}.get(op)
        if prefix is None:
            raise OntapQueryError("InvalidQuery", "Unknown comparison operator.")
        return prefix + self._value(val)

    @staticmethod
    def _date(value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError()
            return dt.astimezone(UTC)
        except (ValueError, AttributeError, TypeError):
            raise OntapQueryError("InvalidQuery", "Times require an ISO-8601 timestamp with timezone.") from None

    def _time_params(self, spec, resource, now):
        if not any(k in spec for k in ["lookback", "start_time", "end_time", "interval"]):
            if resource["history"]:
                raise OntapQueryError("InvalidQuery", "History queries require lookback or explicit UTC bounds.")
            return {}, None
        if "lookback" in spec:
            if "start_time" in spec or "end_time" in spec:
                raise OntapQueryError("InvalidQuery", "Use relative or absolute time, not both.")
            match = re.fullmatch(r"([1-9][0-9]*)(m|h|d|w)", str(spec["lookback"]))
            if not match:
                raise OntapQueryError("InvalidQuery", "lookback must be a positive number followed by m, h, d or w.")
            seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
            if seconds > 31536000:
                raise OntapQueryError("UnavailableHistory", "Requested lookback exceeds one year.")
            start, end = now - timedelta(seconds=seconds), now
        else:
            start = self._date(spec.get("start_time"))
            end = self._date(spec.get("end_time"))
        if start >= end or end > now + timedelta(seconds=30):
            raise OntapQueryError("InvalidQuery", "Time range must be ordered and cannot extend into the future.")
        field = "timestamp" if "timestamp" in resource["filters"] else "time" if "time" in resource["filters"] else None
        if not field:
            raise OntapQueryError("InvalidQuery", "This endpoint does not expose a supported timestamp filter.")
        params = {field: start.isoformat().replace("+00:00", "Z") + ".." + end.isoformat().replace("+00:00", "Z")}
        if resource["history"]:
            age = (now - start).total_seconds()
            tier = next((t for t, seconds in _TIERS if age <= seconds), None)
            if tier is None:
                raise OntapQueryError("UnavailableHistory", "Requested start is outside native retention.")
            requested = spec.get("interval", "auto")
            if not isinstance(requested, str):
                raise OntapQueryError("InvalidQuery", "interval must be a documented tier or auto.")
            if requested != "auto":
                seconds = dict(_TIERS).get(requested)
                if seconds is None or seconds < age:
                    raise OntapQueryError("UnavailableHistory", "Requested tier does not reach the incident start.")
                tier = requested
            params["interval"] = tier
        elif "interval" in spec:
            raise OntapQueryError("InvalidQuery", "interval is only supported for metric history.")
        return params, (field, start, end)

    def execute_query(self, query):
        try:
            spec = json.loads(query) if isinstance(query, str) else query.copy() if isinstance(query, dict) else None
        except (ValueError, TypeError):
            raise OntapQueryError("InvalidQuery", "Query must be a JSON object.") from None
        allowed = {
            "table",
            "fields",
            "filter",
            "parent",
            "lookback",
            "start_time",
            "end_time",
            "interval",
            "order_by",
            "limit",
            "severity",
        }
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise OntapQueryError("InvalidQuery", "Unknown query controls.")
        if spec.get("table") == "volume_aggregates":
            return self._volume_aggregates(spec)
        resource = self._resource(spec.get("table"))
        limit = self._integer(spec.get("limit", self.max_rows), "limit", 1, self.max_rows)
        parents = spec.get("parent", {})
        if not isinstance(parents, dict) or set(parents) != set(resource["parents"]):
            raise OntapQueryError("InvalidQuery", "Supply exactly the required parent parameters from get_schema.")
        path = "/api" + resource["path"]
        for k, v in parents.items():
            if (
                not isinstance(v, str)
                or not v
                or any(part in {".", ".."} for part in v.split("/"))
                or any(c in v for c in "\\\r\n\x00")
            ):
                raise OntapQueryError("InvalidQuery", "Parent identifiers must be nonempty path segments.")
            contract = resource["parent_types"].get(k, {})
            value = v
            field_contract = resource["fields"].get(k, {})
            if contract.get("type") == "integer" or field_contract.get("type") == "integer":
                if not re.fullmatch(r"[+-]?[0-9]{1,20}", v):
                    raise OntapQueryError("InvalidQuery", f"Parent {k} must be an integer identifier.")
                value = int(v)
                bits = 32 if contract.get("format", field_contract.get("format")) == "int32" else 64
                lower = int(contract.get("minimum", -(2 ** (bits - 1))))
                upper = int(contract.get("maximum", 2 ** (bits - 1) - 1))
                if not lower <= value <= upper:
                    raise OntapQueryError("InvalidQuery", f"Parent {k} is outside its declared range.")
            enum_value = value if contract.get("type") == "integer" else v
            if contract.get("enum") and enum_value not in contract["enum"]:
                raise OntapQueryError("InvalidQuery", f"Parent {k} is not a supported identifier.")
            path = path.replace("{" + k + "}", quote(v, safe=""))
        if "{" in path or "}" in path:
            raise OntapQueryError("InvalidQuery", "Unresolved resource path; no request was sent.")
        # Omitted fields means inexpensive identity/default fields, never fields=*.
        default = [
            k
            for k in ["uuid", "name", "state", "svm.uuid", "svm.name", "timestamp", "duration", "status"]
            if k in resource["fields"]
        ]
        if resource["history"]:
            default += [k for k in resource["fields"] if {"iops", "latency", "throughput"}.intersection(k.split("."))]
        fields = self._expand(
            spec["fields"] if "fields" in spec else (default or list(resource["fields"])[:12]), resource
        )
        fields = list(dict.fromkeys(fields + [k for k in _KEYS if k in resource["fields"]] + resource["time_fields"]))
        filters = spec.get("filter", {})
        if not isinstance(filters, dict):
            raise OntapQueryError("InvalidQuery", "filter must be an object.")
        params = {}
        for k, v in filters.items():
            if k not in resource["filters"] or k == "interval":
                raise OntapQueryError("InvalidQuery", f"Unknown or excluded filter: {k}.")
            params[k] = self._filter(v)
        if spec.get("table") in {"volumes", "volume_constituents"}:
            constituent = spec["table"] == "volume_constituents"
            if "is_constituent" in filters and filters["is_constituent"] is not constituent:
                raise OntapQueryError(
                    "InvalidQuery", "Use volumes for logical volumes and volume_constituents for constituents."
                )
            params["is_constituent"] = str(constituent).lower()
        if "severity" in spec:
            if (
                spec["table"] != "ems_events"
                or not isinstance(spec["severity"], list)
                or not spec["severity"]
                or not all(isinstance(x, str) for x in spec["severity"])
                or set(spec["severity"])
                - {"emergency", "alert", "critical", "error", "warning", "notice", "informational", "debug"}
            ):
                raise OntapQueryError("InvalidQuery", "Invalid EMS severity selection.")
            params["message.severity"] = "|".join(spec["severity"])
        if "order_by" in spec:
            if "order_by" not in resource["controls"]:
                raise OntapQueryError("InvalidQuery", "This endpoint does not support ordering.")
            order = spec["order_by"]
            if not isinstance(order, list) or not order:
                raise OntapQueryError("InvalidQuery", "order_by must be a list of field asc/desc strings.")
            for term in order:
                if not isinstance(term, str):
                    raise OntapQueryError("InvalidQuery", "Invalid sort field.")
                parts = term.split()
                if (
                    len(parts) > 2
                    or not parts
                    or parts[0] not in resource["fields"]
                    or (len(parts) == 2 and parts[1] not in {"asc", "desc"})
                ):
                    raise OntapQueryError("InvalidQuery", "Invalid sort field or direction.")
            params["order_by"] = ",".join(order)
        now = datetime.now(UTC)
        time_params, _ = self._time_params(spec, resource, now)
        if set(time_params) & set(params):
            raise OntapQueryError("InvalidQuery", "Time filters cannot override time controls.")
        params.update(time_params)
        if "fields" in resource["controls"]:
            params["fields"] = ",".join(fields)
        if resource["collection"]:
            if "max_records" in resource["controls"]:
                params["max_records"] = min(500, limit)
            if "return_timeout" in resource["controls"]:
                params["return_timeout"] = 15
        query_id = str(uuid.uuid4())
        with self._lock:
            try:
                records = self._pages(path, params, limit, time.monotonic() + 120, resource["collection"])
                self._availability[spec["table"]] = "available" if records else "empty"
            except OntapQueryError as e:
                self._availability[spec["table"]] = {
                    401: "authentication_failed",
                    403: "permission_denied",
                    404: "unsupported",
                }.get(e.status, e.code)
                raise
        rows = []
        for record in records:
            row = {k: self._extract(record, k) for k in fields}
            row.update(parents)
            row.update(
                _bow_cluster=self._cluster,
                _bow_connection=getattr(self, "_bow_connection_id", None),
                _bow_retrieved_at=now.isoformat(),
                _bow_query_id=query_id,
                _bow_endpoint=resource["path"],
                _bow_contract=_catalog()["sha256"],
            )
            rows.append(row)
        columns = list(dict.fromkeys(fields + list(parents) + list(_META)))
        # Construct each column independently to preserve nullable integers >2**53.
        frame = pd.DataFrame({k: pd.Series([row.get(k) for row in rows], dtype="object") for k in columns})
        for k in fields:
            typ = resource["fields"][k]["type"]
            try:
                if typ == "integer":
                    frame[k] = pd.array(
                        frame[k],
                        dtype="UInt64" if any(isinstance(v, int) and v > 2**63 - 1 for v in frame[k]) else "Int64",
                    )
                elif typ == "number":
                    frame[k] = pd.array(frame[k], dtype="Float64")
                elif typ == "boolean":
                    frame[k] = pd.array(frame[k], dtype="boolean")
                elif typ == "string":
                    frame[k] = pd.array(frame[k], dtype="string")
            except (ValueError, TypeError, OverflowError):
                raise OntapQueryError("InvalidResponse", f"Invalid value type for {k}.") from None
        frame["_bow_retrieved_at"] = pd.to_datetime(frame["_bow_retrieved_at"], utc=True)
        return frame

    @staticmethod
    def _extract(record, dotted):
        current = record
        for part in dotted.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return NetAppOntapClient._sanitize(current)

    @staticmethod
    def _sanitize(value):
        if isinstance(value, dict):
            return {
                k: NetAppOntapClient._sanitize(v)
                for k, v in value.items()
                if k != "_links"
                and not re.search(
                    r"(^|[._-])(password|passphrase|secret|private_key|access_key|secret_key|auth_key|token)([._-]|$)",
                    k,
                    re.I,
                )
            }
        if isinstance(value, list):
            return [NetAppOntapClient._sanitize(v) for v in value]
        return value

    def _volume_aggregates(self, spec):
        if set(spec) - {"table", "filter", "limit", "fields"}:
            raise OntapQueryError("InvalidQuery", "Invalid relationship query controls.")
        selected = spec.get("fields", ["volume.uuid", "aggregate.uuid", "aggregate.name"])
        if (
            not isinstance(selected, list)
            or not selected
            or not all(isinstance(x, str) for x in selected)
            or set(selected) - {"volume.uuid", "aggregate.uuid", "aggregate.name"}
        ):
            raise OntapQueryError("InvalidQuery", "Invalid membership fields.")
        volumes = self.execute_query(
            {
                "table": "volumes",
                "filter": spec.get("filter", {}),
                "limit": spec.get("limit", self.max_rows),
                "fields": ["uuid", "aggregates"],
            }
        )
        rows = []
        for _, v in volumes.iterrows():
            for a in v.get("aggregates") or []:
                rows.append(
                    {
                        "volume.uuid": v["uuid"],
                        "aggregate.uuid": a.get("uuid"),
                        "aggregate.name": a.get("name"),
                        **{k: v[k] for k in _META},
                    }
                )
                if len(rows) > spec.get("limit", self.max_rows):
                    raise OntapQueryError("ResultLimitExceeded", "Narrow the volume membership query.")
        frame = pd.DataFrame(
            rows, columns=list(dict.fromkeys(["volume.uuid", "aggregate.uuid"] + selected + list(_META)))
        )
        frame["_bow_retrieved_at"] = pd.to_datetime(frame["_bow_retrieved_at"], utc=True)
        return frame

    def _pages(self, path, params, limit, deadline, collection):
        current = self.url + path + "?" + urlencode(params)
        seen = set()
        records = []
        for _ in range(1000):
            if current in seen:
                raise OntapQueryError("InvalidResponse", "Repeated pagination continuation.")
            seen.add(current)
            body = self._get(current, deadline)
            if not isinstance(body, dict) or body.get("error") or body.get("errors"):
                raise OntapQueryError("PartialResponse", "ONTAP returned an incomplete or invalid response.")
            if not collection:
                return [body]
            batch = body.get("records")
            if not isinstance(batch, list) or not all(isinstance(r, dict) for r in batch):
                raise OntapQueryError("InvalidResponse", "Expected a collection of records.")
            records.extend(batch)
            if len(records) > limit:
                raise OntapQueryError("ResultLimitExceeded", "Narrow filters or increase the complete-result limit.")
            links = body.get("_links") or {}
            if not isinstance(links, dict) or not isinstance(links.get("next") or {}, dict):
                raise OntapQueryError("InvalidResponse", "Invalid pagination envelope.")
            nxt = (links.get("next") or {}).get("href")
            if nxt is not None and not isinstance(nxt, str):
                raise OntapQueryError("InvalidResponse", "Invalid pagination continuation.")
            if not nxt:
                return records
            # ONTAP continuation links stay on this origin and bound resource.
            next_url = urljoin(current, nxt)
            parsed = urlsplit(next_url)
            if (
                parsed.scheme != urlsplit(self.url).scheme
                or parsed.netloc != urlsplit(self.url).netloc
                or parsed.path != path
                or parsed.fragment
            ):
                raise OntapQueryError("InvalidResponse", "Continuation escaped the selected resource.")
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            next_params = dict(pairs)
            if len(next_params) != len(pairs) or any(
                key not in params and key not in {"start", "_tag"} and not key.startswith("start.")
                for key in next_params
            ):
                raise OntapQueryError("InvalidResponse", "Continuation introduced unsupported query controls.")
            # Preserve scope/projection even when a server omits them in its cursor.
            for key, val in params.items():
                if key in next_params and next_params[key] != str(val):
                    raise OntapQueryError("InvalidResponse", "Continuation changed query scope.")
                next_params[key] = str(val)
            current = self.url + path + "?" + urlencode(next_params)
        raise OntapQueryError("BudgetExceeded", "Pagination budget exhausted.")

    def _get(self, url, deadline):
        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OntapQueryError("BudgetExceeded", "Query deadline exhausted.")
            try:
                with self._session.get(
                    url, timeout=min(self.timeout, remaining), allow_redirects=False, stream=True
                ) as response:
                    status = response.status_code
                    if status in {429, 502, 503, 504} and attempt < 3:
                        retry = response.headers.get("Retry-After", "")
                        delay = float(retry) if retry.isdigit() else 0.25 * 2**attempt
                        if delay >= deadline - time.monotonic():
                            raise OntapQueryError("BudgetExceeded", "Retry exceeds query deadline.")
                        time.sleep(delay)
                        continue
                    if status != 200:
                        code = {
                            401: "AuthenticationFailed",
                            403: "PermissionDenied",
                            404: "UnsupportedResource",
                            429: "Throttled",
                        }.get(status, "HttpError")
                        # Do not leak appliance response bodies (may echo filters/secrets).
                        error = OntapQueryError(
                            code, f"ONTAP returned HTTP {status}. Check endpoint permissions and query scope.", status
                        )
                        # Preserve a bounded vendor code without echoing response text.
                        try:
                            raw = response.raw.read(8192)
                            vendor = json.loads(raw).get("error", {}).get("code")
                            if isinstance(vendor, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(vendor)):
                                error.ontap_code = str(vendor)
                        except (ValueError, AttributeError, TypeError):
                            pass
                        raise error
                    payload = bytearray()
                    for chunk in response.iter_content(65536):
                        payload.extend(chunk)
                        if len(payload) > 32 * 1024 * 1024:
                            raise OntapQueryError("BudgetExceeded", "Response exceeds 32 MiB.")
                        if time.monotonic() > deadline:
                            raise OntapQueryError("BudgetExceeded", "Query deadline exhausted.")
                    try:
                        return json.loads(payload)
                    except (ValueError, UnicodeError):
                        raise OntapQueryError("InvalidResponse", "ONTAP did not return valid JSON.") from None
            except requests.exceptions.SSLError:
                raise OntapQueryError(
                    "TLSFailed", "Certificate verification failed; configure the trusted CA."
                ) from None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 3:
                    raise OntapQueryError(
                        "TransportFailed", "ONTAP could not be reached within the retry budget."
                    ) from None
                time.sleep(min(0.25 * 2**attempt, max(0, deadline - time.monotonic())))
