"""Broadcom DX NetOps Spectrum (formerly CA Spectrum) data source client.

Talks to the OneClick Web Services API (`https://<oneclick>/spectrum/restful/…`),
the product's only network-reachable read API (research:
docs/research/broadcom-spectrum-connector.md). The API has no query language:
requests are XML "GET tunnelling" bodies keyed by hex attribute ids and hex
model handles, so — like `aria_operations_client` — queries are JSON specs
(see `system_prompt`) that map onto a catalog of virtual tables:

  landscapes · devices · models · interfaces · model_attributes · alarms ·
  alarm_counts · alarm_filters · events · associations · connectivity ·
  attribute_enums

Wire format: every request sends `Accept: application/xml` and POST bodies as
`application/xml`. XML is the API's native, documented format (TechDocs shows
only XML samples; JSON is a mechanical rendering with no published shape), so
parsing XML is the robust choice. Responses are parsed with defusedxml.

Auth (TechDocs 24.3 + KB 416236): HTTP Basic with a *local* Spectrum user
(works on every version, also with SAML/SSO enabled), or a bearer JWT minted
by an admin in OneClick → Administration → Token Configuration (24.3.10+).
Both are static — there is no token endpoint, so nothing to refresh.

Paging: `throttlesize` per request; a page carries `throttle`, `total-*` and
either `error="EndOfResults"` or a `<link rel="next" href="…"/>` that we follow
verbatim (models: `?id=<result set>&start=N`; others `?start=N`). Broadcom
recommends ≥600 s client timeouts and 250–500 batches for inventory pulls, so
both are config knobs.

Read-only by design: the client never issues PUT/DELETE/POST-create, and the
only `action` it runs is FIND_DEV_MODELS_BY_IP (a search).
"""
import json
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import pandas as pd
import requests
from defusedxml import ElementTree as SafeET

from app.ai.prompt_formatters import ForeignKey, ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, ProgressCallback, discovery_progress

RS = "http://www.ca.com/spectrum/restful/schema/request"
FILTER = "http://www.ca.com/spectrum/restful/schema/filter"
# Prefixes as the docs use them (`rs:`); the filter namespace gets a prefix
# instead of the docs' default-namespace spelling — identical to a
# namespace-aware (JAXB) parser.
ET.register_namespace("rs", RS)
ET.register_namespace("flt", FILTER)

_QUERY_DEADLINE = ContextVar("spectrum_query_deadline", default=None)
QUERY_BUDGET_SECONDS = 600         # Broadcom: inventory pulls can take minutes
MAX_ROWS = 50_000
DEFAULT_LIMIT = 500
DEFAULT_THROTTLE = 500             # Broadcom KB: 250–500 keeps each batch inside the timeout
MAX_THROTTLE = 5000
MAX_PAGES = 400
DEFAULT_EVENT_WINDOW_MINS = 60
MAX_EVENT_WINDOW_DAYS = 31

# ── attribute dictionary (TechDocs "Alarm and Model Attributes" + KB articles) ──
# column name → attribute id. Model attributes:
A_MODEL_TYPE = "0x10000"
A_MTYPE_HANDLE = "0x10001"
A_CONDITION = "0x1000a"
A_NAME = "0x1006e"
A_CLASS = "0x11ee8"
A_LANDSCAPE_NAME = "0x11d42"
A_LANDSCAPE = "0x129ac"
A_MH = "0x129fa"
A_IP = "0x12d7f"
A_DEVICE_TYPE = "0x23000e"
A_SYS_NAME = "0x133f4"
A_CONTACT = "0x23000c"
A_LOCATION = "0x23000d"
A_CRITICALITY = "0x1290c"
A_IS_MANAGED = "0x1295d"
A_GC_LIST = "0x12adb"
A_CONTACT_STATUS = "0x10004"
A_NOTES = "0x11564"
# Alarm attributes:
A_ALARM_ID = "0x11f9c"
A_SEVERITY = "0x11f56"
A_CREATED = "0x11f4e"
A_LAST_OCCURRENCE = "0x1321a"
A_ACK = "0x11f4d"
A_ALARM_STATUS = "0x11f4f"
A_CAUSE = "0x11f50"
A_TITLE = "0x12b4c"
A_ALARMED_MH = "0x11f53"
A_TROUBLESHOOTER = "0x11f57"
A_TICKET = "0x12022"
A_OCCURRENCES = "0x11fc5"
A_ALARM_SOURCE = "0x11fc4"
A_PRIMARY = "0x11f54"
A_ORIG_EVENT = "0x1296e"
A_SYMPTOM_LIST = "0x12a04"
A_CAUSE_LIST = "0x12a05"
A_SYMPTOM_COUNT = "0x12a06"
A_CAUSE_COUNT = "0x12a07"
A_WEB_URL = "0x12a63"
A_USER_CLEARABLE = "0x11f9b"
# Event attributes:
A_EVENT_TEXT = "0x4820007"

ACTION_FIND_DEV_MODELS_BY_IP = "0x10093"

MODEL_COLUMNS: List[Tuple[str, str, str]] = [     # (column, attr id, dtype)
    ("mh", A_MH, "str"), ("name", A_NAME, "str"), ("model_type", A_MODEL_TYPE, "str"),
    ("model_type_handle", A_MTYPE_HANDLE, "str"), ("model_class", A_CLASS, "int"),
    ("condition", A_CONDITION, "int"), ("ip", A_IP, "str"), ("device_type", A_DEVICE_TYPE, "str"),
    ("sys_name", A_SYS_NAME, "str"), ("location", A_LOCATION, "str"), ("contact", A_CONTACT, "str"),
    ("criticality", A_CRITICALITY, "int"), ("is_managed", A_IS_MANAGED, "bool"),
    ("contact_status", A_CONTACT_STATUS, "int"), ("global_collections", A_GC_LIST, "str"),
    ("landscape", A_LANDSCAPE, "str"), ("landscape_name", A_LANDSCAPE_NAME, "str"),
]
INTERFACE_COLUMNS: List[Tuple[str, str, str]] = [
    ("mh", A_MH, "str"), ("name", A_NAME, "str"), ("model_type", A_MODEL_TYPE, "str"),
    ("model_class", A_CLASS, "int"), ("condition", A_CONDITION, "int"), ("ip", A_IP, "str"),
    ("landscape", A_LANDSCAPE, "str"),
]
ALARM_COLUMNS: List[Tuple[str, str, str]] = [
    ("alarm_id", A_ALARM_ID, "str"), ("severity", A_SEVERITY, "int"), ("title", A_TITLE, "str"),
    ("created_epoch", A_CREATED, "int"), ("last_occurrence_epoch", A_LAST_OCCURRENCE, "int"),
    ("occurrences", A_OCCURRENCES, "int"), ("acknowledged", A_ACK, "bool"), ("alarm_status", A_ALARM_STATUS, "str"),
    ("cause_code", A_CAUSE, "str"), ("is_primary", A_PRIMARY, "bool"), ("alarm_source", A_ALARM_SOURCE, "int"),
    ("troubleshooter", A_TROUBLESHOOTER, "str"), ("ticket_id", A_TICKET, "str"),
    ("originating_event", A_ORIG_EVENT, "str"), ("symptom_count", A_SYMPTOM_COUNT, "int"),
    ("symptom_alarm_ids", A_SYMPTOM_LIST, "str"), ("cause_count", A_CAUSE_COUNT, "int"),
    ("cause_alarm_ids", A_CAUSE_LIST, "str"), ("web_url", A_WEB_URL, "str"),
    ("model_mh", A_ALARMED_MH, "str"), ("model_name", A_NAME, "str"), ("model_type", A_MODEL_TYPE, "str"),
    ("model_class", A_CLASS, "int"), ("ip", A_IP, "str"), ("landscape", A_LANDSCAPE, "str"),
    ("landscape_name", A_LANDSCAPE_NAME, "str"),
]
EVENT_COLUMNS: List[Tuple[str, str, str]] = [
    ("text", A_EVENT_TEXT, "str"), ("severity", A_SEVERITY, "int"), ("model_name", A_NAME, "str"),
    ("model_type", A_MODEL_TYPE, "str"), ("ip", A_IP, "str"), ("landscape", A_LANDSCAPE, "str"),
]
DEFAULT_MODEL_ATTRS = [a for _, a, _ in MODEL_COLUMNS if a != A_MH]
ATTR_NAME: Dict[str, str] = {
    **{a: c for c, a, _ in MODEL_COLUMNS},
    **{a: c for c, a, _ in ALARM_COLUMNS},
    A_EVENT_TEXT: "event_text", A_NOTES: "notes", A_USER_CLEARABLE: "user_clearable",
    "0x10032": "sys_uptime", "0x10009": "security_string",
}

# Fallbacks when /attribute/<id>/enums is unavailable (2/3 = Major/Critical per KB 194661;
# the rest confirmed live via attribute_enums).
SEVERITY_FALLBACK = {0: "Normal", 1: "Minor", 2: "Major", 3: "Critical", 4: "Suppressed", 5: "Maintenance"}
SEVERITY_BY_NAME = {v.lower(): k for k, v in SEVERITY_FALLBACK.items()}

_CATALOG: Dict[str, dict] = {
    "landscapes": {
        "pk": "id",
        "columns": [("id", "str"), ("name", "str"), ("is_primary", "bool"), ("spectrum_version", "str")],
        "fks": [],
        "desc": ("The SpectroSERVER landscapes behind this OneClick (one per SS in a distributed estate). "
                 "`id` is the hex landscape handle every model handle embeds. Start here."),
    },
    "devices": {
        "pk": "mh",
        "columns": [(c, t) for c, _, t in MODEL_COLUMNS] + [("model_class_label", "str"), ("condition_label", "str")],
        "fks": [("landscape", "landscapes", "id")],
        "desc": ("Every managed device model (routers, switches, firewalls, hosts …) = SpectroSERVER action "
                 "FIND_ALL_DEVICE_MODELS. Filter by `name` (exact, list ok), `name_contains`, `ip`, `model_type`, "
                 "`model_class` (3 Router, 2 Switch …), `global_collection`, `landscape`, `condition`. "
                 "`condition` 0 Normal … 6 Critical; `contact_status` 0 = Lost (device down)."),
    },
    "models": {
        "pk": "mh",
        "columns": [(c, t) for c, _, t in MODEL_COLUMNS] + [("model_class_label", "str"), ("condition_label", "str")],
        "fks": [("landscape", "landscapes", "id")],
        "desc": ("Any model (devices, interfaces, containers, services, global collections). Same filters as "
                 "`devices` plus `mh` (list of handles) and `model_type_handle`; devices are only a subset."),
    },
    "interfaces": {
        "pk": "mh",
        "columns": [("device_mh", "str"), ("device_name", "str")] + [(c, t) for c, _, t in INTERFACE_COLUMNS]
                   + [("condition_label", "str")],
        "fks": [("device_mh", "devices", "mh"), ("landscape", "landscapes", "id")],
        "desc": ("Port / interface models of a device, named `<device>_<ifName>` (e.g. rtr-core-01_Gi0/1). "
                 "Requires `device` (name or mh; list ok). `condition` 6 = Critical (e.g. link down)."),
    },
    "model_attributes": {
        "pk": None,
        "columns": [("mh", "str"), ("model_name", "str"), ("attr_id", "str"), ("attr_name", "str"), ("value", "str")],
        "fks": [("mh", "models", "mh")],
        "desc": ("Long-format attribute read of one or more models (`mh` or `name`): any hex attribute id via "
                 "`attrs` (default: the device columns). Use it for attributes that are not a catalog column "
                 "(sysUpTime 0x10032, notes 0x11564, custom asset attributes …)."),
    },
    "alarms": {
        "pk": "alarm_id",
        "columns": [(c, t) for c, _, t in ALARM_COLUMNS] + [("severity_label", "str"), ("created_at", "str"),
                                                           ("last_occurrence_at", "str")],
        "fks": [("model_mh", "models", "mh"), ("landscape", "landscapes", "id")],
        "desc": ("CURRENT alarms (Spectrum keeps no alarm history in the API — see `events`). Filter by "
                 "`severity` (3 Critical, 2 Major, 1 Minor; names ok; list ok) or `min_severity`, `name` / `ip` / "
                 "`model_mh` of the alarmed model, `model_type`, `landscape`, `acknowledged`, `primary_only` "
                 "(drop symptom alarms), `title_contains`, `since_hours`, `start_time`/`end_time` (epoch ms), "
                 "`alarm_id`, or a OneClick `alarm_filter` name. `cause_alarm_ids` / `symptom_alarm_ids` are the "
                 "root-cause ⇄ impact links; `is_primary=false` alarms are impact of a cause."),
    },
    "alarm_counts": {
        "pk": None,
        "columns": [("alarm_filter", "str"), ("critical", "int"), ("major", "int"), ("minor", "int")],
        "fks": [],
        "desc": "Alarm counts by severity, optionally through a named OneClick `alarm_filter`.",
    },
    "alarm_filters": {
        "pk": "name",
        "columns": [("name", "str")],
        "fks": [],
        "desc": "Alarm filters defined in OneClick (usable as `alarm_filter` in alarms / alarm_counts).",
    },
    "events": {
        "pk": "event_id",
        "columns": [("event_id", "str"), ("time_epoch_ms", "int"), ("time_at", "str"), ("event_type", "str"),
                    ("model_mh", "str")] + [(c, t) for c, _, t in EVENT_COLUMNS] + [("severity_label", "str")],
        "fks": [("model_mh", "models", "mh"), ("landscape", "landscapes", "id")],
        "desc": ("Event history (traps, polls, state changes) for DEVICE models — the timeline behind alarms. "
                 "Requires `device` (name/mh, list ok, one landscape); window via `duration_in_mins` (default 60) "
                 "or `start_time`/`end_time` epoch ms; `include_interfaces: true` adds port events; "
                 "`exclude_types` drops noisy event type ids."),
    },
    "associations": {
        "pk": None,
        "columns": [("relation", "str"), ("left_mh", "str"), ("left_name", "str"), ("right_mh", "str"), ("right_name", "str")],
        "fks": [("left_mh", "models", "mh"), ("right_mh", "models", "mh")],
        "desc": ("Edges of one relation around a model: `relation` (hex handle, e.g. 0x10002 Collects) + `mh` or "
                 "`name`, `side` left|right. Containment / collection topology."),
    },
    "connectivity": {
        "pk": None,
        "columns": [("device_mh", "str"), ("device_name", "str"), ("local_port_mh", "str"), ("local_port", "str"),
                    ("local_port_type", "str"), ("local_port_condition", "int"), ("neighbor_mh", "str"),
                    ("neighbor", "str"), ("neighbor_ip", "str"), ("neighbor_type", "str"), ("neighbor_class", "str"),
                    ("neighbor_condition", "int"), ("neighbor_port_mh", "str"), ("neighbor_port", "str")],
        "fks": [("device_mh", "devices", "mh"), ("neighbor_mh", "devices", "mh")],
        "desc": ("Layer-2/3 neighbours of a device (`ip` or `name`): local port → neighbour device/link/port. "
                 "THE topology for blast-radius questions."),
    },
    "attribute_enums": {
        "pk": None,
        "columns": [("attr_id", "str"), ("enum_id", "int"), ("label", "str")],
        "fks": [],
        "desc": "Enumeration labels of an attribute (`attr_id`, e.g. 0x11f56 severity, 0x11ee8 model class, 0x1000a condition).",
    },
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _first(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el.iter():
        if c is not el and _local(c.tag) == name:
            return c
    return None


def _text(el: Optional[ET.Element], name: str) -> Optional[str]:
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return None


def _iso(epoch_seconds: Optional[int]) -> Optional[str]:
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _cast(value: Optional[str], dtype: str):
    if value is None or value == "":
        return None
    if dtype == "int":
        try:
            return int(value)
        except ValueError:
            try:
                return int(value, 16)
            except ValueError:
                return None
    if dtype == "bool":
        return value.strip().lower() in ("true", "1", "yes")
    return value


class SpectrumClient(DataSourceClient):
    """Read-only DX NetOps Spectrum OneClick Web Services client."""

    def __init__(
        self,
        url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        landscapes: str = "",
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        request_timeout_s: int = 600,
        throttlesize: int = DEFAULT_THROTTLE,
    ):
        base = (url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        for suffix in ("/spectrum/restful", "/spectrum/rest", "/spectrum"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.base_url = base
        self.username = (username or "").strip() or None
        self.password = password or None
        self.token = (token or "").strip() or None
        self.landscape_filter = [x.strip() for x in (landscapes or "").split(",") if x.strip()]
        self.verify_ssl = verify_ssl
        self.ca_bundle = (ca_bundle or "").strip() or None
        self.request_timeout_s = max(5, int(request_timeout_s or 600))
        self.throttlesize = max(1, min(int(throttlesize or DEFAULT_THROTTLE), MAX_THROTTLE))
        self._session: Optional[requests.Session] = None
        self._landscapes_cache: Optional[List[dict]] = None
        self._enum_cache: Dict[str, Dict[int, str]] = {}

    # ── plumbing ──────────────────────────────────────────────────────────────

    @property
    def description(self):
        return ("Broadcom DX NetOps Spectrum (CA Spectrum) client — network fault management: device and "
                "interface inventory, current alarms with root-cause/symptom links, event history, topology "
                "(connectivity, associations) via the OneClick Web Services API.\n\n" + self.system_prompt())

    def _http(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    @property
    def _verify(self):
        if not self.verify_ssl:
            return False
        return self.ca_bundle or True

    def _auth_headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.username and self.password is not None:
            import base64
            raw = f"{self.username}:{self.password}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        raise RuntimeError("DX NetOps Spectrum needs a local user + password (Basic) or a bearer token.")

    def _timeout(self) -> float:
        deadline = _QUERY_DEADLINE.get()
        if deadline is None:
            return float(self.request_timeout_s)
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise RuntimeError("DX NetOps Spectrum query budget exhausted; narrow the landscape, filter or window.")
        return min(float(self.request_timeout_s), remaining)

    def _request(self, method: str, path_or_url: str, params: Optional[dict] = None,
                 xml_body: Optional[bytes] = None) -> ET.Element:
        """One authenticated call, parsed as XML. `path_or_url` is a noun path or an absolute `next` href."""
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = f"{self.base_url}/spectrum/restful/{path_or_url.lstrip('/')}"
        headers = {**self._auth_headers(), "Accept": "application/xml"}
        if xml_body is not None:
            headers["Content-Type"] = "application/xml; charset=UTF-8"
        try:
            resp = self._http().request(method, url, params=params, data=xml_body, headers=headers,
                                        verify=self._verify, timeout=self._timeout(), allow_redirects=False)
        except requests.exceptions.SSLError as e:
            raise RuntimeError(
                f"DX NetOps Spectrum TLS error: {e}. Provide the CA bundle path or disable 'Verify SSL' for "
                "self-signed OneClick certificates."
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"DX NetOps Spectrum connection failed to {url}: {e}")
        if resp.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"DX NetOps Spectrum redirected {method} {url} to {resp.headers.get('Location')} — with SSO enabled "
                "the API user must be a LOCAL Spectrum account (basic auth) or use a bearer token."
            )
        if resp.status_code == 401:
            raise RuntimeError(
                "DX NetOps Spectrum authentication failed (401). Use a user from Spectrum's local user database "
                "(SSO-only accounts cannot authenticate to the REST API) or a token from Token Configuration."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                f"DX NetOps Spectrum authorization failed (403) for {method} {path_or_url}: the user is not "
                "permitted to use this REST verb/resource (OneClick → REST API access settings)."
            )
        if resp.status_code == 404:
            raise RuntimeError(f"DX NetOps Spectrum resource not found (404): {path_or_url}. {resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"DX NetOps Spectrum HTTP error ({resp.status_code}) for {method} {path_or_url}: "
                               f"{resp.text[:300]}")
        body = resp.content or b""
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if b"<" not in body[:200] or ("html" in ctype):
            raise RuntimeError(
                f"DX NetOps Spectrum answered non-XML for {path_or_url} ({ctype or 'no content type'}): "
                f"{resp.text[:200]!r}. Check the OneClick URL (the /spectrum/restful path is appended automatically)."
            )
        try:
            root = SafeET.fromstring(body)
        except Exception as e:
            raise RuntimeError(f"DX NetOps Spectrum returned unparsable XML for {path_or_url}: {e}")
        err = root.get("error")
        if err and err != "EndOfResults":
            raise RuntimeError(f"DX NetOps Spectrum error for {method} {path_or_url}: {err}")
        return root

    def _get(self, path: str, params: Optional[dict] = None) -> ET.Element:
        return self._request("GET", path, params=params)

    def _post(self, path: str, xml_body: bytes, params: Optional[dict] = None) -> ET.Element:
        return self._request("POST", path, params=params, xml_body=xml_body)

    def _paged(self, first: ET.Element, item: str, limit: int,
               repost: Optional[bytes] = None) -> Iterable[ET.Element]:
        """Yield `<item>` elements across `next` links until EndOfResults / limit.

        A `next` href is followed with GET (models: the stored result set `id`),
        except for GET-tunnelled searches whose next page needs the same body
        again (`repost`): the href is re-POSTed with it."""
        root = first
        n = 0
        for _ in range(MAX_PAGES):
            for el in root.iter():
                if _local(el.tag) == item:
                    yield el
                    n += 1
                    if n >= limit:
                        return
            nxt = None
            for el in root.iter():
                if _local(el.tag) == "link" and el.get("rel") == "next" and el.get("href"):
                    nxt = el.get("href")
                    break
            if nxt is None or root.get("error") == "EndOfResults":
                return
            href = urljoin(self.base_url + "/", nxt)
            root = self._request("POST", href, xml_body=repost) if repost is not None else self._request("GET", href)
        raise RuntimeError("DX NetOps Spectrum pagination safety limit reached; narrow the query.")

    # ── XML request builders (Request.xsd / Filter.xsd subset) ────────────────

    @staticmethod
    def _xml(el: ET.Element) -> bytes:
        return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(el, encoding="utf-8")

    @staticmethod
    def _filter_leaf(parent: ET.Element, op: str, attr_id: str, value: str) -> None:
        node = ET.SubElement(parent, f"{{{FILTER}}}{op}")
        a = ET.SubElement(node, f"{{{FILTER}}}attribute", id=attr_id)
        ET.SubElement(a, f"{{{FILTER}}}value").text = str(value)

    def _model_criteria(self, spec: dict) -> Optional[ET.Element]:
        """Build `<search-criteria><filtered-models><and>…` from model-scope fields."""
        clauses: List[Tuple[str, str, List[str]]] = []     # (op, attr, values) — values OR'ed
        if spec.get("mh"):
            clauses.append(("equals", A_MH, self._as_list(spec["mh"])))
        if spec.get("name"):
            clauses.append(("equals", A_NAME, self._as_list(spec["name"])))
        if spec.get("name_contains"):
            clauses.append(("has-substring", A_NAME, self._as_list(spec["name_contains"])))
        if spec.get("ip"):
            clauses.append(("equals", A_IP, self._as_list(spec["ip"])))
        if spec.get("model_type"):
            clauses.append(("equals", A_MODEL_TYPE, self._as_list(spec["model_type"])))
        if spec.get("model_type_handle"):
            clauses.append(("equals", A_MTYPE_HANDLE, self._as_list(spec["model_type_handle"])))
        if spec.get("model_class") is not None:
            clauses.append(("equals", A_CLASS, [str(v) for v in self._as_list(spec["model_class"])]))
        if spec.get("condition") is not None:
            clauses.append(("equals", A_CONDITION, [str(v) for v in self._as_list(spec["condition"])]))
        if spec.get("global_collection"):
            clauses.append(("has-substring", A_GC_LIST, self._as_list(spec["global_collection"])))
        if spec.get("location_contains"):
            clauses.append(("has-substring", A_LOCATION, self._as_list(spec["location_contains"])))
        if not clauses:
            return None
        crit = ET.Element(f"{{{RS}}}search-criteria")
        fm = ET.SubElement(crit, f"{{{FILTER}}}filtered-models")
        and_ = ET.SubElement(fm, f"{{{FILTER}}}and")
        for op, attr, values in clauses:
            if len(values) == 1:
                self._filter_leaf(and_, op, attr, values[0])
            else:
                or_ = ET.SubElement(and_, f"{{{FILTER}}}or")
                for v in values:
                    self._filter_leaf(or_, op, attr, v)
        return crit

    def _model_request(self, spec: dict, attrs: List[str], throttle: int,
                       criteria: Optional[ET.Element] = None, handles: Optional[List[str]] = None) -> bytes:
        req = ET.Element(f"{{{RS}}}model-request", throttlesize=str(throttle))
        for ls in self._landscape_handles(spec):
            ET.SubElement(req, f"{{{RS}}}landscape", id=ls)
        target = ET.SubElement(req, f"{{{RS}}}target-models")
        if handles:
            for h in handles:
                ET.SubElement(target, f"{{{RS}}}model", mh=h)
        else:
            search = ET.SubElement(target, f"{{{RS}}}models-search")
            crit = criteria if criteria is not None else self._model_criteria(spec)
            if crit is None:
                # No filter: all device models (the documented FIND_ALL_DEVICE_MODELS action).
                crit = ET.Element(f"{{{RS}}}search-criteria")
                am = ET.SubElement(crit, f"{{{FILTER}}}action-models")
                fm = ET.SubElement(am, f"{{{FILTER}}}filtered-models")
                eq = ET.SubElement(fm, f"{{{FILTER}}}equals")
                ET.SubElement(eq, f"{{{FILTER}}}model-type").text = "SearchManager"
                ET.SubElement(am, f"{{{FILTER}}}action").text = "FIND_ALL_DEVICE_MODELS"
            search.append(crit)
        for a in attrs:
            ET.SubElement(req, f"{{{RS}}}requested-attribute", id=a)
        return self._xml(req)

    # ── landscapes / enums ────────────────────────────────────────────────────

    def _landscapes(self) -> List[dict]:
        if self._landscapes_cache is None:
            root = self._get("landscapes")
            out = []
            for el in root.iter():
                if _local(el.tag) == "landscape":
                    out.append({"id": (_text(el, "id") or "").lower(), "name": _text(el, "name") or "",
                                "is_primary": (_text(el, "isPrimary") or "").lower() == "true",
                                "spectrum_version": _text(el, "spectrumVersion") or ""})
            self._landscapes_cache = out
        return self._landscapes_cache

    def _landscape_handles(self, spec: dict) -> List[str]:
        """Resolve `landscape` (handle or name, list ok) ∪ the connection's landscape filter."""
        wanted = self._as_list(spec.get("landscape")) if spec.get("landscape") else list(self.landscape_filter)
        if not wanted:
            return []
        known = self._landscapes()
        out: List[str] = []
        for w in wanted:
            w_l = str(w).strip().lower()
            hit = next((l for l in known if l["id"] == w_l or l["name"].lower() == w_l), None)
            if hit is None and not re.fullmatch(r"0x[0-9a-f]+", w_l):
                raise ValueError(f"Unknown landscape '{w}'. Known: " + ", ".join(f"{l['id']} ({l['name']})" for l in known))
            out.append(hit["id"] if hit else w_l)
        return out

    def _enum(self, attr_id: str) -> Dict[int, str]:
        if attr_id in self._enum_cache:
            return self._enum_cache[attr_id]
        labels: Dict[int, str] = {}
        try:
            root = self._get(f"attribute/{attr_id}/enums")
            for el in root.iter():
                if _local(el.tag) == "enum" and el.get("id") is not None:
                    try:
                        labels[int(el.get("id"))] = el.get("value") or ""
                    except ValueError:
                        continue
        except (IndexingCancelled, KeyboardInterrupt):
            raise
        except Exception:
            labels = {}
        if not labels and attr_id == A_SEVERITY:
            labels = dict(SEVERITY_FALLBACK)
        self._enum_cache[attr_id] = labels
        return labels

    # ── connection test / schema ──────────────────────────────────────────────

    def test_connection(self):
        try:
            self._landscapes_cache = None
            lss = self._landscapes()
            if not lss:
                return {"success": False, "message": "Connected, but OneClick reported no landscapes."}
            version = next((l["spectrum_version"] for l in lss if l["spectrum_version"]), "")
            # One tunnelled POST proves the user may use GET + POST (the two verbs the client needs).
            root = self._post("models", self._model_request({}, [A_NAME], 1))
            total = root.get("total-models") or "?"
            names = ", ".join(f"{l['name']} ({l['id']})" for l in lss[:6])
            return {"success": True,
                    "message": f"Connected to DX NetOps Spectrum {version}: {len(lss)} landscape(s) [{names}], "
                               f"{total} device models."}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @discovery_progress
    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        tables = [self._build_table(name) for name in _CATALOG]
        by_name = {t.name: t for t in tables}
        try:
            lss = self._landscapes()
            if lss:
                by_name["landscapes"].description += " Landscapes: " + "; ".join(
                    f"{l['name']} {l['id']}" + (" (primary)" if l["is_primary"] else "") for l in lss[:10]) + "."
            if progress_callback:
                progress_callback("landscapes", "devices", 0, 2)
            root = self._post("models", self._model_request({}, [A_NAME], 1))
            total = root.get("total-models")
            if total:
                by_name["devices"].description += f" {total} device models managed."
            if progress_callback:
                progress_callback("alarms", "counts", 1, 2)
            counts = self._alarm_counts(None)
            by_name["alarms"].description += (f" Currently {counts['critical']} critical, {counts['major']} major, "
                                              f"{counts['minor']} minor alarms.")
        except IndexingCancelled:
            raise
        except Exception:
            pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        if table_name not in _CATALOG:
            raise ValueError(f"Unknown DX NetOps Spectrum table '{table_name}'. Available: {', '.join(_CATALOG)}.")
        return self._build_table(table_name)

    def _build_table(self, name: str) -> Table:
        spec = _CATALOG[name]
        columns = [TableColumn(name=c, dtype=d) for c, d in spec["columns"]]
        by_name = {c.name: c for c in columns}
        fks = [ForeignKey(column=by_name[col], references_name=ref, references_column=TableColumn(name=rc, dtype="str"))
               for col, ref, rc in spec["fks"]]
        pks = [by_name[spec["pk"]]] if spec["pk"] else []
        return Table(name=name, description=spec["desc"], columns=columns, pks=pks, fks=fks)

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    # ── queries ───────────────────────────────────────────────────────────────

    def execute_query(self, query=None, **kwargs) -> pd.DataFrame:
        """Run a JSON query spec (string or dict) and return a DataFrame."""
        if query is None and kwargs:
            query = kwargs
        token = _QUERY_DEADLINE.set(time.monotonic() + QUERY_BUDGET_SECONDS)
        try:
            spec = self._parse_spec(query)
            limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)
            handler = getattr(self, f"_q_{spec['table']}")
            df = handler(spec, limit)
            return df.head(limit)
        finally:
            _QUERY_DEADLINE.reset(token)

    def _q_landscapes(self, spec: dict, limit: int) -> pd.DataFrame:
        self._landscapes_cache = None
        return pd.DataFrame(self._landscapes(), columns=[c for c, _ in _CATALOG["landscapes"]["columns"]])

    def _model_rows(self, spec: dict, limit: int, columns: List[Tuple[str, str, str]],
                    criteria: Optional[ET.Element] = None, handles: Optional[List[str]] = None) -> List[dict]:
        extra = [str(a).lower() for a in self._as_list(spec.get("attrs"))] if spec.get("attrs") else []
        attrs = [a for _, a, _ in columns if a != A_MH] + [a for a in extra if a not in ATTR_NAME]
        throttle = max(1, min(self.throttlesize, limit))
        crit = criteria if (criteria is not None or handles) else self._model_criteria(spec)
        body = self._model_request(spec, attrs, throttle, criteria=crit, handles=handles)
        root = self._post("models", body)
        rows = []
        for el in self._paged(root, "model", limit):
            row = self._model_row(el, columns, extra)
            if row is not None:
                rows.append(row)
        return rows

    @staticmethod
    def _attr_map(el: ET.Element) -> Dict[str, Optional[str]]:
        out: Dict[str, Optional[str]] = {}
        for a in el:
            if _local(a.tag) == "attribute" and a.get("id"):
                out[a.get("id").lower()] = None if a.get("error") else (a.text or "")
        return out

    def _model_row(self, el: ET.Element, columns: List[Tuple[str, str, str]], extra: List[str]) -> Optional[dict]:
        if el.get("error") and el.get("error") != "EndOfResults":
            return None
        attrs = self._attr_map(el)
        row: dict = {}
        for col, aid, dtype in columns:
            row[col] = el.get("mh") if aid == A_MH else _cast(attrs.get(aid), dtype)
        for aid in extra:
            row[ATTR_NAME.get(aid, f"attr_{aid}")] = attrs.get(aid.lower())
        return row

    def _decorate_models(self, rows: List[dict]) -> List[dict]:
        cls = self._enum(A_CLASS)
        cond = self._enum(A_CONDITION)
        for r in rows:
            r["model_class_label"] = cls.get(r.get("model_class")) if r.get("model_class") is not None else None
            r["condition_label"] = cond.get(r.get("condition")) if r.get("condition") is not None else None
        return rows

    def _device_handles(self, spec: dict) -> set:
        """All device model handles (GET devices = FIND_ALL_DEVICE_MODELS), handles only — one
        cheap pull used to keep filtered `devices` queries to real devices."""
        params: dict = {"throttlesize": MAX_THROTTLE}
        lss = self._landscape_handles(spec)
        if lss:
            params["landscape"] = lss
        root = self._get("devices", params=params)
        return {el.get("mh") for el in self._paged(root, "model", MAX_ROWS) if el.get("mh")}

    def _q_devices(self, spec: dict, limit: int) -> pd.DataFrame:
        if self._model_criteria(spec) is None:
            rows = self._model_rows(spec, limit, MODEL_COLUMNS)
        else:
            # A filtered models-search spans every model kind (ports, containers …);
            # intersect with the API's own definition of "device".
            devices = self._device_handles(spec)
            rows = [r for r in self._model_rows(spec, MAX_ROWS, MODEL_COLUMNS) if r["mh"] in devices][:limit]
        return self._frame(self._decorate_models(rows), "devices")

    def _q_models(self, spec: dict, limit: int) -> pd.DataFrame:
        scope_keys = set(spec) & self._MODEL_SCOPE
        handles = self._as_list(spec.get("mh")) if scope_keys == {"mh"} else None
        crit = None if handles else self._model_criteria(spec)
        if crit is None and not handles:
            raise ValueError("models needs at least one filter (mh, name, name_contains, ip, model_type, "
                             "model_type_handle, model_class, global_collection, condition) — use `devices` for all devices.")
        rows = self._model_rows(spec, limit, MODEL_COLUMNS, criteria=crit, handles=handles)
        return self._frame(self._decorate_models(rows), "models")

    def _resolve_devices(self, spec: dict, key: str = "device") -> List[dict]:
        refs = self._as_list(spec.get(key))
        if not refs:
            raise ValueError(f"`{key}` is required (device name or model handle; list ok).")
        handles = [r for r in refs if re.fullmatch(r"0x[0-9a-fA-F]+", str(r))]
        names = [r for r in refs if r not in handles]
        found: List[dict] = []
        if handles:
            found += self._model_rows({"mh": handles, "landscape": spec.get("landscape")}, len(handles), MODEL_COLUMNS,
                                      handles=handles)
        if names:
            found += self._model_rows({"name": names, "landscape": spec.get("landscape")}, len(names) * 4, MODEL_COLUMNS)
        if not found:
            raise ValueError(f"No model matched {key}={refs}. Names are exact — try devices with name_contains first.")
        return found

    def _q_interfaces(self, spec: dict, limit: int) -> pd.DataFrame:
        devices = self._resolve_devices(spec)
        rows: List[dict] = []
        for d in devices:
            sub = {"name_contains": f"{d['name']}_", "landscape": d.get("landscape") or spec.get("landscape")}
            for r in self._model_rows(sub, limit, INTERFACE_COLUMNS):
                if r["mh"] == d["mh"]:
                    continue
                rows.append({"device_mh": d["mh"], "device_name": d["name"], **r})
            if len(rows) >= limit:
                break
        cond = self._enum(A_CONDITION)
        for r in rows:
            r["condition_label"] = cond.get(r.get("condition")) if r.get("condition") is not None else None
        return self._frame(rows, "interfaces")

    def _q_model_attributes(self, spec: dict, limit: int) -> pd.DataFrame:
        targets = self._resolve_devices({**spec, "device": spec.get("mh") or spec.get("name")}) \
            if (spec.get("mh") or spec.get("name")) else None
        if not targets:
            raise ValueError("model_attributes needs `mh` or `name`.")
        attrs = [str(a).lower() for a in self._as_list(spec.get("attrs"))] or DEFAULT_MODEL_ATTRS
        rows: List[dict] = []
        for t in targets:
            root = self._get(f"model/{t['mh']}", params={"attr": attrs})
            for el in root.iter():
                if _local(el.tag) == "model":
                    for a in el:
                        if _local(a.tag) == "attribute" and a.get("id"):
                            aid = a.get("id").lower()
                            rows.append({"mh": t["mh"], "model_name": t["name"], "attr_id": aid,
                                         "attr_name": ATTR_NAME.get(aid, ""),
                                         "value": None if a.get("error") else (a.text or "")})
        return self._frame(rows, "model_attributes")

    # alarms ------------------------------------------------------------------

    def _severity_values(self, spec: dict) -> Optional[List[int]]:
        values: List[int] = []
        if spec.get("severity") is not None:
            for v in self._as_list(spec["severity"]):
                if isinstance(v, bool):
                    raise ValueError("severity must be an int (0-5) or a name (critical/major/minor).")
                if isinstance(v, int):
                    values.append(v)
                else:
                    key = str(v).strip().lower()
                    enum = {name.lower(): k for k, name in self._enum(A_SEVERITY).items()} or SEVERITY_BY_NAME
                    if key in enum:
                        values.append(enum[key])
                    elif key.isdigit():
                        values.append(int(key))
                    else:
                        raise ValueError(f"Unknown severity '{v}'. Use 3 Critical, 2 Major, 1 Minor (or those names).")
        if spec.get("min_severity") is not None:
            ms = spec["min_severity"]
            if not isinstance(ms, int) or isinstance(ms, bool):
                key = str(ms).strip().lower()
                ms = SEVERITY_BY_NAME.get(key)
                if ms is None:
                    raise ValueError("min_severity must be 1 Minor, 2 Major or 3 Critical.")
            values.extend(v for v in range(ms, 4) if v not in values)
        return values or None

    def _alarm_request(self, spec: dict, attrs: List[str], throttle: int) -> bytes:
        req = ET.Element(f"{{{RS}}}alarm-request", throttlesize=str(throttle))
        for ls in self._landscape_handles(spec):
            ET.SubElement(req, f"{{{RS}}}landscape", id=ls)
        clauses: List[Tuple[str, str, List[str]]] = []
        sev = self._severity_values(spec)
        if sev:
            clauses.append(("equals", A_SEVERITY, [str(v) for v in sev]))
        if spec.get("acknowledged") is not None:
            clauses.append(("equals", A_ACK, ["true" if spec["acknowledged"] else "false"]))
        if spec.get("title_contains"):
            clauses.append(("has-substring", A_TITLE, self._as_list(spec["title_contains"])))
        if spec.get("cause_code"):
            clauses.append(("equals", A_CAUSE, self._as_list(spec["cause_code"])))
        if spec.get("troubleshooter"):
            clauses.append(("equals", A_TROUBLESHOOTER, self._as_list(spec["troubleshooter"])))
        if spec.get("ticket_id"):
            clauses.append(("equals", A_TICKET, self._as_list(spec["ticket_id"])))
        if spec.get("start_time") is not None:
            clauses.append(("greater-than-or-equals", A_CREATED, [str(int(spec["start_time"]) // 1000)]))
        if spec.get("end_time") is not None:
            clauses.append(("less-than-or-equals", A_CREATED, [str(int(spec["end_time"]) // 1000)]))
        if clauses:
            af = ET.SubElement(req, f"{{{RS}}}attribute-filter")
            crit = ET.SubElement(af, f"{{{FILTER}}}search-criteria")
            fm = ET.SubElement(crit, f"{{{FILTER}}}filtered-models")
            and_ = ET.SubElement(fm, f"{{{FILTER}}}and")
            for op, attr, values in clauses:
                if len(values) == 1:
                    self._filter_leaf(and_, op, attr, values[0])
                else:
                    or_ = ET.SubElement(and_, f"{{{FILTER}}}or")
                    for v in values:
                        self._filter_leaf(or_, op, attr, v)
        model_scope = {k: spec[k] for k in ("name", "name_contains", "ip", "model_type", "model_class", "global_collection")
                       if spec.get(k) is not None}
        if spec.get("model_mh"):
            model_scope["mh"] = spec["model_mh"]
        if model_scope:
            target = ET.SubElement(req, f"{{{RS}}}target-models")
            if "mh" in model_scope and len(model_scope) == 1:
                for h in self._as_list(model_scope["mh"]):
                    ET.SubElement(target, f"{{{RS}}}model", mh=h)
            else:
                search = ET.SubElement(target, f"{{{RS}}}models-search")
                search.append(self._model_criteria(model_scope))
        for aid in self._as_list(spec.get("alarm_id")):
            ET.SubElement(req, f"{{{RS}}}alarm", id=str(aid))
        if spec.get("alarm_filter"):
            ET.SubElement(req, f"{{{RS}}}alarm-filter", name=str(spec["alarm_filter"]))
        for a in attrs:
            ET.SubElement(req, f"{{{RS}}}requested-attribute", id=a)
        return self._xml(req)

    def _q_alarms(self, spec: dict, limit: int) -> pd.DataFrame:
        extra = [str(a).lower() for a in self._as_list(spec.get("attrs"))] if spec.get("attrs") else []
        attrs = [a for _, a, _ in ALARM_COLUMNS] + [a for a in extra if a not in ATTR_NAME]
        throttle = max(1, min(self.throttlesize, limit))
        params: dict = {}
        if spec.get("primary_only"):
            params["symptoms"] = "no"
        if spec.get("since_hours"):
            params["lasthour"] = int(spec["since_hours"])
        root = self._post("alarms", self._alarm_request(spec, attrs, throttle), params=params or None)
        rows: List[dict] = []
        for el in self._paged(root, "alarm", limit):
            if el.get("error"):
                continue
            am = self._attr_map(el)
            row = {"alarm_id": el.get("id")}
            for col, aid, dtype in ALARM_COLUMNS:
                if col == "alarm_id":
                    row[col] = el.get("id") or am.get(aid)
                else:
                    row[col] = _cast(am.get(aid), dtype)
            for aid in extra:
                row[ATTR_NAME.get(aid, f"attr_{aid}")] = am.get(aid)
            rows.append(row)
        sev = self._enum(A_SEVERITY)
        for r in rows:
            r["severity_label"] = sev.get(r.get("severity")) if r.get("severity") is not None else None
            r["created_at"] = _iso(r.get("created_epoch"))
            r["last_occurrence_at"] = _iso(r.get("last_occurrence_epoch"))
        # Client-side post filters the API cannot express.
        if spec.get("primary_only"):
            rows = [r for r in rows if r.get("is_primary") is not False]
        if spec.get("landscape"):
            wanted = set(self._landscape_handles(spec))
            rows = [r for r in rows if not r.get("landscape") or r["landscape"].lower() in wanted]
        rows.sort(key=lambda r: (r.get("created_epoch") or 0))
        return self._frame(rows, "alarms")

    def _alarm_counts(self, alarm_filter: Optional[str]) -> dict:
        req = ET.Element(f"{{{RS}}}alarms-count-request")
        if alarm_filter:
            ET.SubElement(req, f"{{{RS}}}alarm-filter", name=alarm_filter)
        root = self._post("alarms/count", self._xml(req))
        out = {"alarm_filter": alarm_filter or ""}
        for k in ("critical", "major", "minor"):
            out[k] = _cast(_text(root, k), "int")
        return out

    def _q_alarm_counts(self, spec: dict, limit: int) -> pd.DataFrame:
        filters = self._as_list(spec.get("alarm_filter")) or [None]
        return self._frame([self._alarm_counts(f) for f in filters], "alarm_counts")

    def _q_alarm_filters(self, spec: dict, limit: int) -> pd.DataFrame:
        root = self._get("alarms/filters")
        rows = [{"name": (el.text or "").strip()} for el in root.iter() if _local(el.tag) == "alarmfilter"]
        return self._frame(rows, "alarm_filters")

    # events ------------------------------------------------------------------

    def _window(self, spec: dict) -> Tuple[int, int]:
        now_ms = int(time.time() * 1000)
        start, end = spec.get("start_time"), spec.get("end_time")
        if start is None and end is None:
            mins = int(spec.get("duration_in_mins") or DEFAULT_EVENT_WINDOW_MINS)
            return now_ms - mins * 60_000, now_ms
        if end is None:
            end = min(now_ms, int(start) + int(spec.get("duration_in_mins") or DEFAULT_EVENT_WINDOW_MINS) * 60_000) \
                if spec.get("duration_in_mins") else now_ms
        if start is None:
            start = int(end) - int(spec.get("duration_in_mins") or DEFAULT_EVENT_WINDOW_MINS) * 60_000
        start, end = int(start), int(end)
        if end < start:
            raise ValueError("end_time must be ≥ start_time (epoch milliseconds).")
        if end - start > MAX_EVENT_WINDOW_DAYS * 86_400_000:
            raise ValueError(f"events window capped at {MAX_EVENT_WINDOW_DAYS} days per query.")
        return start, end

    def _q_events(self, spec: dict, limit: int) -> pd.DataFrame:
        devices = self._resolve_devices(spec)
        start, end = self._window(spec)
        extra = [str(a).lower() for a in self._as_list(spec.get("attrs"))] if spec.get("attrs") else []
        attrs = [a for _, a, _ in EVENT_COLUMNS] + [a for a in extra if a not in ATTR_NAME]
        by_ls: Dict[str, List[dict]] = {}
        for d in devices:
            ls = (d.get("landscape") or "").lower() or (self._landscape_handles(spec) or [""])[0]
            if not ls:
                ls = d["mh"][:-5] + "00000"   # a handle embeds its landscape prefix
            by_ls.setdefault(ls, []).append(d)
        if len(by_ls) > 1:
            raise ValueError("events supports one landscape per query (the API limit); split the device list by landscape.")
        ls, devs = next(iter(by_ls.items()))
        throttle = max(1, min(self.throttlesize, limit))
        req = ET.Element(f"{{{RS}}}get-event-request", throttlesize=str(throttle))
        f = ET.SubElement(req, f"{{{RS}}}get-events-filter")
        f.set("start-time", str(start))
        f.set("end-time", str(end))
        f.set("subcomponents-events", "true" if spec.get("include_interfaces") else "false")
        if spec.get("exclude_types"):
            f.set("exclude-events", ",".join(str(x) for x in self._as_list(spec["exclude_types"])))
        for a in attrs:
            ET.SubElement(req, f"{{{RS}}}requested-attribute", id=a)
        target = ET.SubElement(req, f"{{{RS}}}target-models")
        for d in devs:
            ET.SubElement(target, f"{{{RS}}}model", mh=d["mh"])
        ET.SubElement(req, f"{{{RS}}}landscape", id=ls)
        body = self._xml(req)
        root = self._post("events/getEvents", body)
        rows: List[dict] = []
        sev = self._enum(A_SEVERITY)
        for el in self._paged(root, "event", limit, repost=body):
            am = self._attr_map(el)
            ts = _cast(el.get("time"), "int")
            row = {"event_id": el.get("id"), "time_epoch_ms": ts,
                   "time_at": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat() if ts else None,
                   "event_type": el.get("type"), "model_mh": el.get("mh")}
            for col, aid, dtype in EVENT_COLUMNS:
                row[col] = _cast(am.get(aid), dtype)
            for aid in extra:
                row[ATTR_NAME.get(aid, f"attr_{aid}")] = am.get(aid)
            row["severity_label"] = sev.get(row["severity"]) if row.get("severity") is not None else None
            rows.append(row)
        rows.sort(key=lambda r: r.get("time_epoch_ms") or 0)
        return self._frame(rows, "events")

    # topology ----------------------------------------------------------------

    def _q_associations(self, spec: dict, limit: int) -> pd.DataFrame:
        rel = str(spec.get("relation") or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]+", rel):
            raise ValueError("associations needs `relation` as a hex relation handle (e.g. 0x10002 Collects).")
        targets = self._resolve_devices({**spec, "device": spec.get("mh") or spec.get("name")}) \
            if (spec.get("mh") or spec.get("name")) else None
        if not targets:
            raise ValueError("associations needs `mh` or `name`.")
        side = spec.get("side") or "left"
        if side not in ("left", "right"):
            raise ValueError("side must be 'left' or 'right'.")
        rows: List[dict] = []
        for t in targets:
            root = self._get(f"associations/relation/{rel}/model/{t['mh']}", params={"side": side})
            for el in root.iter():
                if _local(el.tag) == "association":
                    rows.append({"relation": el.get("relation") or rel, "left_mh": _text(el, "leftmh"),
                                 "right_mh": _text(el, "rightmh")})
            if len(rows) >= limit:
                break
        rows = rows[:limit]
        handles = sorted({h for r in rows for h in (r["left_mh"], r["right_mh"]) if h})
        names: Dict[str, str] = {}
        for chunk in self._chunks(handles, 200):
            for r in self._model_rows({"mh": chunk}, len(chunk), [("mh", A_MH, "str"), ("name", A_NAME, "str")],
                                      handles=chunk):
                names[r["mh"]] = r["name"]
        for r in rows:
            r["left_name"] = names.get(r["left_mh"])
            r["right_name"] = names.get(r["right_mh"])
        return self._frame(rows, "associations")

    def _q_connectivity(self, spec: dict, limit: int) -> pd.DataFrame:
        ips = [str(x) for x in self._as_list(spec.get("ip"))]
        if not ips and (spec.get("name") or spec.get("device") or spec.get("mh")):
            for d in self._resolve_devices({**spec, "device": spec.get("device") or spec.get("name") or spec.get("mh")}):
                if d.get("ip"):
                    ips.append(d["ip"])
        if not ips:
            raise ValueError("connectivity needs `ip` or `name` of a device.")
        rows: List[dict] = []
        for ip in ips:
            root = self._get(f"connectivity/{ip}")
            dev = self._model_rows({"ip": ip}, 1, [("mh", A_MH, "str"), ("name", A_NAME, "str")])
            dev_mh, dev_name = (dev[0]["mh"], dev[0]["name"]) if dev else (None, None)
            for conn in root.iter():
                if _local(conn.tag) != "connection-response":
                    continue
                left = _first(conn, "connection-element-left")
                right = _first(conn, "connection-element-right")
                rport = _first(conn, "connection-element-right-port")
                rows.append({
                    "device_mh": dev_mh, "device_name": dev_name,
                    "local_port_mh": _text(left, "mh"), "local_port": _text(left, "name"),
                    "local_port_type": _text(left, "type"), "local_port_condition": _cast(_text(left, "condition"), "int"),
                    "neighbor_mh": _text(right, "mh"), "neighbor": _text(right, "name"), "neighbor_ip": _text(right, "ipaddress"),
                    "neighbor_type": _text(right, "type"), "neighbor_class": _text(right, "class"),
                    "neighbor_condition": _cast(_text(right, "condition"), "int"),
                    "neighbor_port_mh": _text(rport, "mh"), "neighbor_port": _text(rport, "name"),
                })
        return self._frame(rows, "connectivity")

    def _q_attribute_enums(self, spec: dict, limit: int) -> pd.DataFrame:
        aid = str(spec.get("attr_id") or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]+", aid):
            raise ValueError("attribute_enums needs `attr_id` as a hex attribute id (e.g. 0x11f56).")
        self._enum_cache.pop(aid, None)
        rows = [{"attr_id": aid, "enum_id": k, "label": v} for k, v in sorted(self._enum(aid).items())]
        return self._frame(rows, "attribute_enums")

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _frame(rows: List[dict], table: str) -> pd.DataFrame:
        cols = [c for c, _ in _CATALOG[table]["columns"]]
        extra = [k for r in rows for k in r if k not in cols]
        seen: List[str] = []
        for k in extra:
            if k not in seen:
                seen.append(k)
        return pd.DataFrame(rows, columns=cols + seen)

    @staticmethod
    def _as_list(v) -> List[Any]:
        if v is None:
            return []
        if isinstance(v, (list, tuple, set)):
            return [x for x in v if x is not None and x != ""]
        return [v]

    @staticmethod
    def _chunks(items: List[str], n: int) -> Iterable[List[str]]:
        for i in range(0, len(items), n):
            yield items[i:i + n]

    _COMMON = {"table", "limit", "landscape", "attrs"}
    _MODEL_SCOPE = {"mh", "name", "name_contains", "ip", "model_type", "model_type_handle", "model_class",
                    "condition", "global_collection", "location_contains"}
    _ALLOWED = {
        "landscapes": set(),
        "devices": _MODEL_SCOPE - {"mh", "model_type_handle"},
        "models": _MODEL_SCOPE,
        "interfaces": {"device"},
        "model_attributes": {"mh", "name"},
        "alarms": {"severity", "min_severity", "acknowledged", "primary_only", "title_contains", "since_hours",
                   "start_time", "end_time", "alarm_id", "alarm_filter", "cause_code", "troubleshooter", "ticket_id",
                   "name", "name_contains", "ip", "model_mh", "model_type", "model_class", "global_collection"},
        "alarm_counts": {"alarm_filter"},
        "alarm_filters": set(),
        "events": {"device", "start_time", "end_time", "duration_in_mins", "include_interfaces", "exclude_types"},
        "associations": {"relation", "mh", "name", "side"},
        "connectivity": {"ip", "name", "device", "mh"},
        "attribute_enums": {"attr_id"},
    }

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = dict(query)
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError(
                    'DX NetOps Spectrum query must be a JSON object like {"table": "alarms", "min_severity": 2} '
                    f"— got: {str(query)[:200]}"
                )
        if not isinstance(spec, dict) or not spec.get("table"):
            raise ValueError('DX NetOps Spectrum query spec must include a "table" key.')
        table = str(spec["table"])
        if table not in _CATALOG:
            raise ValueError(f"Unknown DX NetOps Spectrum table '{table}'. Available: {', '.join(_CATALOG)}.")
        unknown = set(spec) - self._ALLOWED[table] - self._COMMON
        if unknown:
            raise ValueError(f"Unsupported fields for {table}: {', '.join(sorted(unknown))}. "
                             f"Allowed: {', '.join(sorted(self._ALLOWED[table] | self._COMMON))}.")
        for field in ("start_time", "end_time"):
            if field in spec and (isinstance(spec[field], bool) or not isinstance(spec[field], int) or spec[field] < 0):
                raise ValueError(f"{field} must be a non-negative epoch-MILLISECOND integer.")
        for field in ("limit", "duration_in_mins", "since_hours"):
            if field in spec and (isinstance(spec[field], bool) or not isinstance(spec[field], int) or spec[field] <= 0):
                raise ValueError(f"{field} must be a positive integer.")
        for field in ("acknowledged", "primary_only", "include_interfaces"):
            if field in spec and not isinstance(spec[field], bool):
                raise ValueError(f"{field} must be a JSON boolean.")
        if "attrs" in spec:
            bad = [a for a in self._as_list(spec["attrs"]) if not re.fullmatch(r"0x[0-9a-fA-F]+", str(a))]
            if bad:
                raise ValueError(f"attrs must be hex attribute ids (e.g. 0x10032): {bad}")
        spec["table"] = table
        return spec

    # ── prompt ────────────────────────────────────────────────────────────────

    def system_prompt(self):
        return """
        ## Broadcom DX NetOps Spectrum Integration
        Query Spectrum via `execute_query(query)` where `query` is a JSON string:

        ```json
        {"table": "alarms", "min_severity": 2, "primary_only": true, "limit": 200}
        ```

        - `table` (required): landscapes, devices, models, interfaces, model_attributes, alarms,
          alarm_counts, alarm_filters, events, associations, connectivity, attribute_enums.
        - Model scope (devices / models / alarms): `name` (EXACT, list ok), `name_contains`, `ip`,
          `model_type` (Rtr_Cisco, SwCiscoIOS, Gen_IF_Port …), `model_class` (3 Router, 2 Switch, 8 Switch-Router,
          9 Firewall, 7 Server, 20 Interface), `global_collection`, `condition`, `landscape` (handle or name).
          Handles are hex strings (`0x100010`); keep them as strings.
        - `attrs`: extra hex attribute ids to return as columns (any table that reads models/alarms/events).
        - `limit` (default 500): a limited result is a sample, not proof of absence.
        - Times: alarms expose `created_epoch` (seconds) + `created_at` (ISO UTC); events take
          `start_time`/`end_time` in EPOCH MILLISECONDS or `duration_in_mins` (default 60).

        ALARMS (what is wrong now):
        - `alarms` = current alarms. `severity` 3 Critical / 2 Major / 1 Minor (names ok), `min_severity`,
          `acknowledged`, `primary_only: true` drops symptom (impact) alarms, `title_contains`, `since_hours`,
          `alarm_filter` (a OneClick filter name from `alarm_filters`), or a model scope (`name`, `ip`, `model_mh`).
        - Root cause vs impact: `is_primary=false` + `cause_alarm_ids` point at the causing alarm; a primary
          alarm's `symptom_alarm_ids` list what it suppressed. Follow those ids with `alarm_id: [...]`.
        - `alarm_counts` for a quick severity breakdown (optionally per `alarm_filter`).

        INVENTORY / TOPOLOGY:
        - `devices` (all device models) → `mh`, `ip`, `model_type`, `condition` (6 Critical … 0 Normal),
          `contact_status` 0 = device not responding. `models` for interfaces/containers with a filter.
        - `interfaces` with `device` → the ports of a device (`<device>_<ifName>`), with `condition`.
        - `connectivity` with `ip` or `name` → neighbours per local port (blast radius / what sits behind a link).
        - `associations` with `relation` (hex handle) + `mh`/`name` → containment/collection edges.
        - `model_attributes` with `mh`/`name` + `attrs` → any attribute (e.g. 0x10032 sysUpTime, 0x11564 notes).

        TIMELINE (what happened):
        - `events` with `device` (name/mh, list ok, one landscape) over a window — traps, polls, link changes —
          add `include_interfaces: true` for port events and `exclude_types` for noise (e.g. "0x10d26" poll ok).
          Order by `time_epoch_ms`; the earliest deviation on the primary alarm's model is usually the cause.

        Examples:
        ```python
        crit = client.execute_query('{"table": "alarms", "min_severity": 2, "primary_only": true}')
        impact = client.execute_query('{"table": "alarms", "alarm_id": ["0x1000004", "0x1000005"]}')
        dev = client.execute_query('{"table": "devices", "name_contains": "rtr-core"}')
        nb = client.execute_query('{"table": "connectivity", "name": "rtr-core-01"}')
        ifs = client.execute_query('{"table": "interfaces", "device": "rtr-core-01"}')
        ev = client.execute_query('{"table": "events", "device": ["rtr-core-01", "fw-edge-01"], "include_interfaces": true, "start_time": 1761792000000, "end_time": 1761795600000, "exclude_types": ["0x10d26"]}')
        ```
        Aggregate by fetching rows and grouping in pandas. Join to other connections on `name`, `ip` or `sys_name`.
        """


SpectrumClient = SpectrumClient
