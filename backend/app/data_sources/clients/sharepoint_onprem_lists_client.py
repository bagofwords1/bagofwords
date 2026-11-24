"""SharePoint Server *lists* as tables, over the on-premises REST API (2013+).

Backs the `sharepoint_onprem_lists` data source type. It is the on-premises
counterpart of `sharepoint_lists` (Microsoft Graph): every list on the site is a
catalog table with typed columns, and `execute_query()` takes the same JSON spec
(see `_sharepoint_list_spec`). Authentication is Windows only (NTLM, or Kerberos
with a mounted keytab) through `WindowsAuthSessions` — no Graph, no Entra app.

Why rows are filtered here and not on the server
------------------------------------------------
SharePoint Server enforces a List View Threshold (5,000 items by default): any
query that has to *scan* more items than that is refused with
SPQueryThrottledException, even when it would return three rows. A `$filter` or
`$orderby` on a column without an index scans the whole list, and `$top` is
applied only after the filter. Microsoft Graph offers a header to override this;
SharePoint Server does not. Indexing is no reliable fix either (an index cannot
be added to a 2013 list past the threshold, indexed lookups still fail).

So this client never sends `$filter` or `$orderby`. It reads the list in pages
ordered by ID — the primary key, always indexed, safe at any list size — via
`$top` and `$skiptoken=Paged=TRUE&p_ID=<last id>`, and evaluates the spec's
filter and sort locally (`_odata_filter`). A query without a filter stops as
soon as it has `limit` rows; a filtered or sorted query scans at most
`max_items` items and says so on the result when the list holds more.

Other SharePoint Server details handled here
--------------------------------------------
* Column internal names rarely match what users see; the catalog shows display
  names and translates both ways. REST payloads key values by the field's
  `EntityPropertyName` (`OData__x0032_017` for a column internally `_x0032_017`).
* Person and lookup columns return only numeric IDs unless expanded. Names are
  fetched with `$expand` — `Title` only, never `EMail` (one user without an email
  fails the whole query). SharePoint also limits how many lookup joins one query
  may make (12 by default); past `MAX_EXPANSIONS`, or when the server refuses,
  those columns return IDs and the result says so.
* Managed-metadata (taxonomy) values are read from the column's hidden
  companion text field: the REST `Label` of a taxonomy value is often the
  term's internal number, not its name.
* Dates are returned in UTC (ISO-8601 with `Z`). Calculated date columns are
  computed in the site's time zone and stay text.
* Folders inside a list are skipped; rich-text notes are reduced to text.
* Every request stays on the configured site's `/_api/`; links from the server
  are never followed, redirects are refused, responses are size-bounded, and
  provider error bodies are never surfaced.
"""
from __future__ import annotations

import html
import json
import logging
import re
import textwrap
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients._odata_filter import (
    Column, ODataFilterError, compile_filter, parse_orderby, sort_rows,
)
from app.data_sources.clients._sharepoint_list_spec import (
    column_lookup, parse_list_spec, select_columns, spec_limit,
)
from app.data_sources.clients._windows_auth_session import WindowsAuthSessions
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_progress
from app.data_sources.clients.sharepoint_onprem_client import SharePointHTTPError, _safe_path

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 1000
#: Items read per request. SharePoint accepts up to 5,000 (the threshold);
#: smaller pages keep each response well under the size bound.
PAGE_SIZE = 1000
MIN_PAGE_SIZE = 50
#: Default and ceiling for how many items one query may read.
DEFAULT_MAX_ITEMS = 20000
MAX_ITEMS_CEILING = 100000
#: Person/lookup columns expanded to names in one request. SharePoint Server's
#: default lookup threshold is 12 joins per query; stay clear of it.
MAX_EXPANSIONS = 10
RESPONSE_BYTE_LIMIT = 16 * 1024 * 1024
#: Retries on HTTP 429/503 (SharePoint request throttling), honouring Retry-After.
THROTTLE_RETRIES = 3
THROTTLE_MAX_WAIT_SECONDS = 10.0

# BaseType 0 is the generic-list family (custom lists, tasks, contacts, issues,
# calendars, links, announcements…). Document libraries are BaseType 1 and
# belong to the `sharepoint_onprem` files connector.
GENERIC_LIST_BASE_TYPE = 0
# Visible list templates that are site plumbing, not business data.
_EXCLUDED_TEMPLATES = {
    140,   # Workflow history
    160,   # Access requests
}

# Read-only system columns worth keeping in a data catalog.
_USEFUL_READONLY = {"ID", "Modified", "Created", "Author", "Editor"}
# Columns that are noise in a data catalog even when visible.
_EXCLUDED_COLUMNS = {
    "ContentType", "Attachments", "Edit", "LinkTitle", "LinkTitleNoMenu", "LinkTitle2",
    "DocIcon", "ItemChildCount", "FolderChildCount", "ComplianceAssetId",
    "AppAuthor", "AppEditor", "Order", "SelectTitle", "PermMask", "GUID",
    "InstanceID", "WorkflowVersion", "WorkflowInstanceID", "MetaInfo",
    "owshiddenversion", "UniqueId", "ProgId", "ScopeId", "FSObjType",
    "FileRef", "FileDirRef", "FileLeafRef", "Last_x0020_Modified",
    "Created_x0020_Date", "SortBehavior", "SyncClientId", "HTML_x0020_File_x0020_Type",
    "ServerUrl", "EncodedAbsUrl", "BaseName", "File_x0020_Type", "Restricted",
    "OriginatorId", "NoExecute", "ContentVersion", "AccessPolicy", "ParentUniqueId",
    "ParentVersionString", "ParentLeafName",
}

_PLAIN_TYPES = {
    "Text": "string", "Note": "string", "Choice": "string", "MultiChoice": "string",
    "Number": "float", "Currency": "float", "Integer": "integer", "Counter": "integer",
    "DateTime": "datetime", "Boolean": "boolean", "URL": "string",
    "GridChoice": "string", "OutcomeChoice": "string",
}
_LOOKUP_TYPES = {"Lookup": False, "LookupMulti": True, "User": False, "UserMulti": True}
_TAXONOMY_TYPES = {"TaxonomyFieldType": False, "TaxonomyFieldTypeMulti": True}
# SP.FieldType numbers a calculated column's OutputType may carry.
_CALCULATED_OUTPUT = {1: "float", 9: "float", 10: "float", 8: "boolean"}
_CALCULATED_PREFIX = re.compile(r"^(?:string|float|datetime|boolean|error|int|number|currency);#", re.I)
_GUID = re.compile(r"^\{?[0-9a-fA-F-]{36}\}?$")


class ListThresholdError(ValueError):
    """SharePoint refused the query for exceeding a list or lookup threshold."""

    def __init__(self, lookup: bool):
        self.lookup = lookup
        super().__init__(
            "SharePoint refused the query: it exceeds the lookup column threshold."
            if lookup else
            "SharePoint refused the query: it exceeds the List View Threshold."
        )


class _ResponseTooLarge(ValueError):
    pass


class _FieldRejected(ValueError):
    """HTTP 400 for the request's $select/$expand (a column the server will not project)."""


@dataclass
class _ListColumn:
    internal: str
    entity: str
    display: str
    dtype: str
    type_name: str
    read_only: bool = False
    multi: bool = False
    lookup_field: Optional[str] = None   # Lookup/User: the target column to show
    text_entity: Optional[str] = None    # Taxonomy: companion note field's entity name
    rich_text: bool = False
    calculated: bool = False

    @property
    def is_lookup(self) -> bool:
        return self.type_name in _LOOKUP_TYPES

    def as_spec(self) -> dict:
        return {"internal": self.internal, "display": self.display, "entity": self.entity, "col": self}


@dataclass
class _ScanResult:
    rows: List[Dict[str, Any]]
    scanned: int
    exhausted: bool
    id_only: List[str] = field(default_factory=list)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")


def _html_to_text(value: str) -> str:
    parser = _HTMLText()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts).replace("​", "")
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _entity_name(internal: str) -> str:
    """REST property name for a field when the server does not report one."""
    return "OData_" + internal if internal.startswith("_") else internal


def _results(value: Any) -> Optional[list]:
    """A multi-value payload as a list: verbose `{"results": [...]}` or a bare array."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        return value["results"]
    return None


def _guid_key(value: Any) -> str:
    return str(value or "").strip("{}").lower()


class SharepointOnpremListsClient(DataSourceClient):
    """SharePoint Server lists — every list on the site becomes a queryable table."""

    capabilities = {Capability.QUERY}

    relative_date_hint = (
        "Filter dates with ISO-8601 UTC literals (e.g. Modified ge '2026-01-01T00:00:00Z'); "
        "compute relative dates in Python (datetime.now(timezone.utc) - timedelta(days=7)) "
        "and inline the ISO string."
    )

    def __init__(
        self,
        site_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        kerberos: bool = False,
        principal: Optional[str] = None,
        lists: Optional[str] = "*",
        include_hidden: bool = False,
        max_items: int = DEFAULT_MAX_ITEMS,
        allow_http: bool = False,
        **kwargs,
    ):
        self.site_url = (site_url or "").strip().rstrip("/")
        parsed = urlsplit(self.site_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Site URL must be an HTTP(S) site URL without credentials, query, or fragment.")
        if parsed.scheme == "http" and not allow_http:
            raise ValueError("HTTPS is required. Enable Allow HTTP only for an isolated test lab.")
        if "/_api" in parsed.path.lower() or "/_layouts" in parsed.path.lower():
            raise ValueError("Site URL must be the site address (e.g. https://sharepoint.example.com/sites/finance), not an API or page URL.")
        _safe_path(unquote(parsed.path))
        self._origin = (parsed.scheme.lower(), parsed.netloc.lower())
        self._origin_url = f"{parsed.scheme}://{parsed.netloc}"
        self._site_path = unquote(parsed.path).rstrip("/")

        raw = (lists or "").strip()
        self.lists_scope = (
            [s.strip().casefold() for s in raw.split(",") if s.strip()]
            if raw and raw != "*" else None
        )
        self.include_hidden = bool(include_hidden)
        try:
            max_items = int(DEFAULT_MAX_ITEMS if max_items in (None, "") else max_items)
        except (TypeError, ValueError):
            raise ValueError("Max items per query must be a whole number.")
        if not 1 <= max_items <= MAX_ITEMS_CEILING:
            raise ValueError(f"Max items per query must be between 1 and {MAX_ITEMS_CEILING:,}.")
        self.max_items = max_items

        self.kerberos, self.principal = bool(kerberos), principal
        self._sessions = WindowsAuthSessions(
            username, password, kerberos=bool(kerberos), principal=principal,
            headers={"Accept": "application/json;odata=verbose"},
        )
        self._sleep = time.sleep
        self.page_size = PAGE_SIZE
        # Caches live on this instance only (clients are built per request/run).
        self._lists_cache: Optional[List[dict]] = None
        self._columns_cache: Dict[str, List[_ListColumn]] = {}

    # ------------------------------------------------------------- identity

    @property
    def is_document_based(self) -> bool:
        return False

    @property
    def description(self) -> str:
        return (
            f"SharePoint Server lists on site {self.site_url} — each list is a table; "
            "query via execute_query with a JSON spec."
            + "\n\n" + self.system_prompt()
        )

    # ------------------------------------------------------------ transport

    def _api_url(self, path: str) -> str:
        url = self.site_url + "/_api/" + path.lstrip("/")
        p = urlsplit(url)
        if ((p.scheme.lower(), p.netloc.lower()) != self._origin
                or not unquote(p.path).startswith(self._site_path + "/_api/")
                or p.username or p.password or p.fragment):
            raise ValueError("Refusing a SharePoint API path outside the configured site.")
        return url

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET one `/_api/` resource as JSON (the verbose `d` envelope unwrapped)."""
        url = self._api_url(path)
        throttled = 0
        for attempt in range(2 + THROTTLE_RETRIES):
            try:
                response = self._sessions.get().get(
                    url, params=params, timeout=(10, 60), allow_redirects=False, stream=True,
                )
            except requests.exceptions.SSLError as exc:
                raise ValueError("SharePoint TLS verification failed. Install the issuing CA in the backend trust store.") from exc
            except requests.RequestException as exc:
                self._sessions.reset()
                if attempt:
                    raise ValueError("Cannot reach SharePoint. Check DNS, network access, and server availability.") from exc
                continue
            with response:
                status = response.status_code
                if status in (429, 503) and throttled < THROTTLE_RETRIES:
                    throttled += 1
                    self._sleep(self._retry_after(response, throttled))
                    continue
                if 300 <= status < 400:
                    raise ValueError("SharePoint redirected the request. Use the site's configured public URL (alternate access mapping).")
                body = self._read_bounded(response)
            if status == 200:
                try:
                    data = json.loads(body)
                except ValueError as exc:
                    raise ValueError("SharePoint returned a response that is not JSON. Check that the Site URL points at a SharePoint Server site.") from exc
                return data.get("d", data) if isinstance(data, dict) else data
            self._raise_for_error(status, body)
        raise SharePointHTTPError(429)

    @staticmethod
    def _retry_after(response, attempt: int) -> float:
        try:
            wait = float(response.headers.get("Retry-After", ""))
        except ValueError:
            wait = 2.0 ** attempt
        return max(0.0, min(wait, THROTTLE_MAX_WAIT_SECONDS))

    @staticmethod
    def _read_bounded(response) -> bytes:
        if int(response.headers.get("Content-Length") or 0) > RESPONSE_BYTE_LIMIT:
            raise _ResponseTooLarge()
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > RESPONSE_BYTE_LIMIT:
                raise _ResponseTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _raise_for_error(status: int, body: bytes):
        """Map a SharePoint error response; the provider's message is never surfaced."""
        code, message = "", ""
        try:
            error = json.loads(body).get("error") or json.loads(body).get("odata.error") or {}
            code = str(error.get("code") or "")
            message = error.get("message")
            message = str(message.get("value") if isinstance(message, dict) else message or "")
        except (ValueError, AttributeError):
            pass
        if "SPQueryThrottledException" in code or "threshold" in message.lower():
            raise ListThresholdError(lookup="lookup" in message.lower())
        if status == 400:
            raise _FieldRejected("SharePoint rejected the requested columns.")
        raise SharePointHTTPError(status)

    # ------------------------------------------------------------ discovery

    def _fetch_lists(self) -> List[dict]:
        if self._lists_cache is not None:
            return self._lists_cache
        params = {
            "$select": "Id,Title,Description,ItemCount,Hidden,BaseTemplate,BaseType,RootFolder/ServerRelativeUrl",
            "$expand": "RootFolder",
            "$filter": f"BaseType eq {GENERIC_LIST_BASE_TYPE}",
            "$top": 5000,
        }
        data = self._get("web/lists", params)
        rows = _results(data.get("results", data.get("value"))) or []
        out = []
        for row in rows:
            root = ((row.get("RootFolder") or {}).get("ServerRelativeUrl") or "").rstrip("/")
            # A list whose root is not inside this site is not part of this connection.
            if root and not unquote(root).casefold().startswith((self._site_path + "/").casefold()):
                continue
            out.append(row)
        self._lists_cache = out
        return out

    def _included_lists(self) -> List[dict]:
        included = []
        for item in self._fetch_lists():
            if item.get("BaseType", GENERIC_LIST_BASE_TYPE) != GENERIC_LIST_BASE_TYPE:
                continue
            if item.get("BaseTemplate") in _EXCLUDED_TEMPLATES:
                continue
            if item.get("Hidden") and not self.include_hidden:
                continue
            if self.lists_scope is not None:
                names = {str(item.get("Title") or "").casefold(), _guid_key(item.get("Id"))}
                if not (names & {_guid_key(s) if _GUID.match(s) else s for s in self.lists_scope}):
                    continue
            included.append(item)
        return sorted(included, key=lambda l: str(l.get("Title") or "").casefold())

    def _list_path(self, item: dict) -> str:
        list_id = _guid_key(item.get("Id"))
        if not re.fullmatch(r"[0-9a-f-]{36}", list_id):
            raise ValueError("SharePoint returned a list without a valid id.")
        return f"web/lists(guid'{list_id}')"

    def _columns(self, item: dict) -> List[_ListColumn]:
        key = _guid_key(item.get("Id"))
        if key in self._columns_cache:
            return self._columns_cache[key]
        data = self._get(self._list_path(item) + "/fields")
        fields = _results(data.get("results", data.get("value"))) or []
        by_id = {_guid_key(f.get("Id")): f for f in fields}

        cols: List[_ListColumn] = []
        seen: Dict[str, int] = {}
        for f in fields:
            internal = str(f.get("InternalName") or "")
            type_name = str(f.get("TypeAsString") or "")
            if not internal or f.get("Hidden") or internal in _EXCLUDED_COLUMNS:
                continue
            # `_ModerationStatus`, `_UIVersionString`…: system columns. A user
            # column whose name starts with a digit or symbol is encoded `_x003N_…`.
            if internal.startswith("_") and not re.match(r"^_x[0-9a-fA-F]{4}_", internal):
                continue
            # Calculated columns are read-only by nature but are business data.
            if f.get("ReadOnlyField") and internal not in _USEFUL_READONLY and type_name != "Calculated":
                continue
            entity = str(f.get("EntityPropertyName") or _entity_name(internal))
            col = _ListColumn(internal=internal, entity=entity, display="", dtype="string", type_name=type_name,
                              read_only=bool(f.get("ReadOnlyField")))
            if type_name in _PLAIN_TYPES:
                col.dtype = _PLAIN_TYPES[type_name]
                col.multi = type_name == "MultiChoice"
                col.rich_text = type_name == "Note" and bool(f.get("RichText"))
            elif type_name in _LOOKUP_TYPES:
                col.multi = _LOOKUP_TYPES[type_name] or bool(f.get("AllowMultipleValues"))
                target = str(f.get("LookupField") or "Title")
                col.lookup_field = "Title" if type_name.startswith("User") or not re.fullmatch(r"\w+", target) else target
            elif type_name in _TAXONOMY_TYPES:
                companion = by_id.get(_guid_key(f.get("TextField")))
                if not companion:
                    continue  # no readable labels: leave the column out rather than show term ids
                col.multi = _TAXONOMY_TYPES[type_name] or bool(f.get("AllowMultipleValues"))
                col.text_entity = str(companion.get("EntityPropertyName") or _entity_name(str(companion.get("InternalName"))))
            elif type_name == "Calculated":
                col.calculated = True
                col.dtype = _CALCULATED_OUTPUT.get(f.get("OutputType"), "string")
            else:
                continue  # Computed, Attachments, Guid, File, ContentTypeId, …
            if internal == "ID":
                col.dtype = "integer"
            display = str(f.get("Title") or internal).strip() or internal
            # Two columns may share a display name; qualify repeats with the
            # internal name so the mapping stays invertible.
            if display.casefold() in seen:
                display = f"{display} ({internal})"
            seen[display.casefold()] = 1
            col.display = display
            cols.append(col)
        if not any(c.internal == "ID" for c in cols):
            cols.insert(0, _ListColumn(internal="ID", entity="ID", display="ID", dtype="integer",
                                       type_name="Counter", read_only=True))
        self._columns_cache[key] = cols
        return cols

    # ------------------------------------------------------------- catalog

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> List[Table]:
        tables: List[Table] = []
        seen: Dict[str, int] = {}
        for item in discovery_items(self._included_lists(), "lists", label=lambda l: l.get("Title")):
            cols = self._columns(item)
            name = str(item.get("Title") or item.get("Id"))
            if name.casefold() in seen:
                name = f"{name} ({_guid_key(item.get('Id'))[:8]})"
            seen[name.casefold()] = 1
            count = item.get("ItemCount")
            description = f"SharePoint Server list '{name}'"
            if item.get("Description"):
                description += f" — {item['Description']}"
            if isinstance(count, int):
                description += f" ({count:,} items)"
            tables.append(Table(
                name=name,
                description=description,
                columns=[TableColumn(name=c.display, dtype=c.dtype) for c in cols],
                pks=[TableColumn(name="ID", dtype="integer")],
                fks=[],
                metadata_json={"sharepoint_onprem_list": {
                    "list_id": _guid_key(item.get("Id")),
                    "list_title": item.get("Title"),
                    "url": self._origin_url + str((item.get("RootFolder") or {}).get("ServerRelativeUrl") or ""),
                    "item_count": count,
                    "base_template": item.get("BaseTemplate"),
                    "internal_names": {c.display: c.internal for c in cols},
                }},
            ))
        return tables

    def get_schema(self, table_name: str) -> Optional[Table]:
        return next((t for t in self.get_schemas() if t.name == table_name), None)

    def prompt_schema(self) -> str:
        from app.ai.prompt_formatters import ServiceFormatter
        tables = self.get_schemas()
        if not tables:
            return "No SharePoint lists available on this site."
        return ServiceFormatter(tables).table_str

    # ------------------------------------------------------------- reading

    def _resolve_list(self, ref: str) -> dict:
        wanted = (ref or "").strip()
        if not wanted:
            raise ValueError("Query spec must name a 'list'.")
        low = wanted.casefold()
        candidates = self._included_lists()
        for item in candidates:
            if low in (str(item.get("Title") or "").casefold(), _guid_key(item.get("Id"))):
                return item
        # A catalog name qualified for a duplicate title: "Tasks (1a2b3c4d)".
        m = re.fullmatch(r"(.*) \(([0-9a-f]{8})\)", low)
        if m:
            for item in candidates:
                if str(item.get("Title") or "").casefold() == m.group(1) and _guid_key(item.get("Id")).startswith(m.group(2)):
                    return item
        partial = [item for item in candidates if low in str(item.get("Title") or "").casefold()]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(sorted(str(item.get("Title") or "?") for item in candidates))
        raise ValueError(f"SharePoint list '{ref}' not found. Available lists: {names}")

    def _request_parts(self, cols: List[_ListColumn], mode: str,
                       required: frozenset) -> Tuple[List[str], List[str], set]:
        """$select / $expand for these columns, and which lookups are expanded.

        `mode` is "all" (expand up to MAX_EXPANSIONS person/lookup columns),
        "required" (only those the filter or sort reads) or "none". Columns in
        `required` are always expanded first: a filter or sort compares names,
        so those must never be the ones that fall back to IDs.
        """
        select = ["ID", "FileSystemObjectType"]
        expand: List[str] = []
        expanded: set = set()
        lookups = sorted((c for c in cols if c.is_lookup), key=lambda c: c.internal not in required)
        for c in lookups:
            wanted = mode == "all" or (mode == "required" and c.internal in required)
            if wanted and len(expanded) < MAX_EXPANSIONS:
                expanded.add(c.internal)
        for c in cols:
            if c.internal == "ID":
                continue
            if c.is_lookup:
                if c.internal in expanded:
                    select.append(f"{c.entity}/{c.lookup_field}")
                    expand.append(c.entity)
                else:
                    select.append(f"{c.entity}Id")
            elif c.text_entity:
                select.append(c.text_entity)
            else:
                select.append(c.entity)
        return list(dict.fromkeys(select)), list(dict.fromkeys(expand)), expanded

    def _value(self, raw: dict, col: _ListColumn, expanded: set) -> Any:
        if col.internal == "ID":
            return raw.get("ID", raw.get("Id"))
        if col.is_lookup:
            if col.internal in expanded:
                value = raw.get(col.entity)
                if isinstance(value, list):  # nometadata: a multi-value is a bare array
                    return [v.get(col.lookup_field) for v in value if isinstance(v, dict)]
                if isinstance(value, dict) and "__deferred" not in value:
                    many = _results(value)
                    if many is not None:
                        return [v.get(col.lookup_field) for v in many if isinstance(v, dict)]
                    return value.get(col.lookup_field)
                if value is None:
                    return [] if col.multi else None
            ids = raw.get(col.entity + "Id")
            many = _results(ids)
            if many is not None:
                return [int(v) for v in many if v is not None]
            return ids
        if col.text_entity:
            text = raw.get(col.text_entity)
            if not text:
                return [] if col.multi else None
            labels = [part.split("|", 1)[0].strip() for part in str(text).split(";") if part.strip()]
            return labels if col.multi else (labels[0] if labels else None)
        value = raw.get(col.entity)
        if col.multi:
            many = _results(value)
            return list(many) if many is not None else ([] if value is None else [value])
        if value is None:
            return None
        if col.type_name == "URL":
            return value.get("Url") if isinstance(value, dict) else value
        if col.calculated:
            text = _CALCULATED_PREFIX.sub("", str(value), count=1)
            if col.dtype == "float":
                try:
                    return float(text)
                except ValueError:
                    return None
            if col.dtype == "boolean":
                return text.strip().lower() in ("1", "true", "yes")
            return text
        if col.dtype == "float":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        if col.dtype == "integer":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        if col.dtype == "boolean":
            return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes")
        if col.rich_text or (col.type_name == "Note" and isinstance(value, str) and value.lstrip().startswith("<div")):
            return _html_to_text(html.unescape(value) if "&lt;" in value[:40] else value)
        return value

    def _scan(self, item: dict, cols: List[_ListColumn], *, keep, stop_at: Optional[int],
              required: frozenset = frozenset()) -> _ScanResult:
        """Read the list in ID order, keeping the rows `keep` accepts.

        Every list item counts against `max_items` — folders included, since
        they cost the server the same — and no page asks for more than the
        allowance left, so a query never reads past it. Whether more items
        remain comes from the response itself (a next link, unread items on the
        page, or a full page from a server that sends no links); nothing is
        fetched just to find out.

        Pages are requested from the last ID seen, never by following a link
        the server returned. A page too large for the response bound is retried
        at half the size. A refused lookup expansion is retried with only the
        filter/sort columns expanded, then with none; a filter or sort column
        that cannot be expanded is an error, never a silent ID comparison.
        """
        path = self._list_path(item) + "/items"
        page_cap = self.page_size
        mode = "all"
        saw_links = False
        last_id = 0
        rows: List[Dict[str, Any]] = []
        scanned = 0
        id_only: List[str] = []
        while True:
            allowance = self.max_items - scanned
            if allowance <= 0:
                return _ScanResult(rows=rows, scanned=scanned, exhausted=False, id_only=id_only)
            page = min(page_cap, allowance)
            select, expand, expanded = self._request_parts(cols, mode, required)
            if required - expanded:
                names = ", ".join(c.display for c in cols if c.internal in required - expanded)
                raise ValueError(
                    f"SharePoint would not return names for {names}, which the filter or sort uses "
                    "(the server limits person/lookup columns per query). Filter or sort on fewer "
                    "person/lookup columns, or filter on their IDs."
                )
            params: Dict[str, Any] = {"$select": ",".join(select), "$top": page}
            if expand:
                params["$expand"] = ",".join(expand)
            if last_id:
                params["$skiptoken"] = f"Paged=TRUE&p_ID={last_id}"
            try:
                data = self._get(path, params)
            except _ResponseTooLarge:
                if page_cap <= MIN_PAGE_SIZE:
                    raise ValueError("SharePoint list items are too large to read; select fewer columns.")
                page_cap = max(MIN_PAGE_SIZE, page_cap // 2)
                continue
            except (ListThresholdError, _FieldRejected) as exc:
                if expand and (isinstance(exc, _FieldRejected) or exc.lookup):
                    if mode == "required":
                        raise ValueError(
                            "SharePoint refused to return names for the person/lookup columns the "
                            "filter or sort uses. Filter or sort on fewer of them, or on their IDs."
                        ) from exc
                    # Keep the filter/sort columns as names if dropping the rest is enough.
                    mode = "required" if required and len(expanded) > len(required) else "none"
                    continue
                if isinstance(exc, ListThresholdError) and exc.lookup:
                    raise ValueError(
                        "SharePoint refused the query: it names more person/lookup columns than the "
                        "server's lookup column threshold allows. Select fewer of those columns."
                    ) from exc
                if isinstance(exc, ListThresholdError):
                    raise ValueError(
                        "SharePoint refused to page through this list (List View Threshold). "
                        "Ask a SharePoint administrator to check the list's throttling settings."
                    ) from exc
                raise ValueError("SharePoint rejected the requested columns for this list.") from exc
            for c in cols:
                if c.is_lookup and c.internal not in expanded and c.display not in id_only:
                    id_only.append(c.display)

            results = _results(data.get("results", data.get("value"))) or []
            linked = bool(data.get("__next") or data.get("odata.nextLink") or data.get("@odata.nextLink"))
            saw_links = saw_links or linked
            # A server that links pages says when there is no next one; without
            # links, only a short page proves the end.
            more_pages = linked or (not saw_links and len(results) >= page)
            fresh = []
            for raw in results:
                try:
                    item_id = int(raw.get("ID", raw.get("Id")))
                except (TypeError, ValueError):
                    continue
                if item_id > (fresh[-1][0] if fresh else last_id):
                    fresh.append((item_id, raw))
            if not fresh:
                return _ScanResult(rows=rows, scanned=scanned, exhausted=True, id_only=id_only)
            for index, (item_id, raw) in enumerate(fresh):
                if scanned >= self.max_items:
                    return _ScanResult(rows=rows, scanned=scanned, exhausted=False, id_only=id_only)
                last_id = item_id
                scanned += 1
                if raw.get("FileSystemObjectType") == 1:
                    continue  # a folder, not a row
                row = {c.internal: self._value(raw, c, expanded) for c in cols}
                if not keep(row):
                    continue
                rows.append(row)
                if stop_at is not None and len(rows) >= stop_at:
                    rest = any(r.get("FileSystemObjectType") != 1 for _, r in fresh[index + 1:])
                    return _ScanResult(rows=rows, scanned=scanned, exhausted=not (rest or more_pages),
                                       id_only=id_only)
            if not more_pages:
                return _ScanResult(rows=rows, scanned=scanned, exhausted=True, id_only=id_only)

    def execute_query(self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs) -> pd.DataFrame:
        """Run a list query spec and return a DataFrame.

        `query` is a JSON string (or dict):
            {"list": "Expenses",                       (required)
             "filter": "Category eq 'Travel'",         (optional OData filter)
             "select": ["Title", "Category", "Amount"],(optional columns)
             "orderby": "Modified desc",               (optional)
             "limit": 1000}                             (optional)
        """
        spec = parse_list_spec(query if query is not None else table_name)
        if table_name and "list" not in spec:
            spec["list"] = table_name
        target = self._resolve_list(str(spec.get("list") or ""))
        title = str(target.get("Title") or "")
        cols = self._columns(target)
        specs = [c.as_spec() for c in cols]
        selected = [s["col"] for s in select_columns(spec.get("select"), specs, title)]
        limit = spec_limit(spec, DEFAULT_LIMIT, self.max_items)

        find = column_lookup(specs)
        names = [c.display for c in cols] + [c.internal for c in cols] + [c.entity for c in cols]

        def resolve(ref: str) -> Optional[Column]:
            found = find(ref)
            if found is None:
                return None
            col = found["col"]
            return Column(key=col.internal, dtype=col.dtype, name=col.display)

        try:
            flt = compile_filter(str(spec["filter"]), names, resolve) if spec.get("filter") else None
            order = parse_orderby(str(spec["orderby"]), names, resolve) if spec.get("orderby") else []
        except ODataFilterError as exc:
            known = ", ".join(c.display for c in cols)
            raise ValueError(f"{exc} Columns on list '{title}': {known}") from exc

        compared = set(flt.keys) if flt else set()
        compared |= {c.key for c, _ in order}
        needed = {c.internal for c in selected} | compared
        fetch = [c for c in cols if c.internal in needed or c.internal == "ID"]
        # Person/lookup columns the filter or sort reads must arrive as names.
        required = frozenset(c.internal for c in fetch if c.is_lookup and c.internal in compared)
        if len(required) > MAX_EXPANSIONS:
            raise ValueError(
                f"The filter and sort use {len(required)} person/lookup columns; SharePoint can "
                f"return names for at most {MAX_EXPANSIONS} per query. Use fewer of them."
            )

        started = time.perf_counter()
        result = self._scan(
            target, fetch,
            keep=flt.matches if flt else (lambda _row: True),
            stop_at=None if (flt or order) else limit,
            required=required,
        )
        rows = sort_rows(result.rows, order) if order else result.rows
        cut_by_limit = len(rows) > limit
        rows = rows[:limit]

        def out(value):
            if isinstance(value, list):
                return "; ".join(str(v) for v in value if v is not None) or None
            return value

        frame = pd.DataFrame(
            [{"id": r.get("ID"), **{c.display: out(r.get(c.internal)) for c in selected}} for r in rows],
            columns=["id"] + [c.display for c in selected],
        )

        notices: List[str] = []
        count = target.get("ItemCount") if isinstance(target.get("ItemCount"), int) else None
        if not result.exhausted and (flt or order):
            notices.append(
                f"Read the first {result.scanned:,} items of list '{title}'"
                + (f" (of about {count:,})" if count else "")
                + "; the filter and sort only cover those items. SharePoint Server cannot filter "
                "unindexed columns on large lists, so rows are filtered after reading. "
                "Raise 'Max items per query' on the connection to cover more."
            )
        elif cut_by_limit or (not result.exhausted and not (flt or order)):
            notices.append(
                f"Returned the first {limit:,} rows of list '{title}'"
                + (f" (about {count:,} items)" if count else "")
                + " because of the query's limit; raise 'limit' to get more."
            )
        if result.id_only:
            notices.append(
                "These person/lookup columns hold SharePoint item IDs instead of names, because the "
                "server limits how many lookup columns one query may join: "
                + ", ".join(result.id_only) + ". Select fewer columns to get names."
            )
        frame.attrs["truncated"] = (not result.exhausted) or cut_by_limit
        if notices:
            frame.attrs["bow_notice"] = " ".join(notices)
        logger.info(
            "sharepoint_onprem_lists.execute_query: list=%r filter=%r scanned=%d rows=%d in %.1fs",
            title, spec.get("filter"), result.scanned, len(frame), time.perf_counter() - started,
        )
        return frame

    # ------------------------------------------------------------- testing

    def test_connection(self) -> dict:
        """Bounded: site, identity, and the list catalog. Never reads items."""
        try:
            web = self._get("web", {"$select": "Title,Url"})
            user = self._get("web/currentuser", {"$select": "LoginName"})
            lists = self._included_lists()
        except IndexingCancelled:
            raise
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        details = {"site": web.get("Title"), "identity": user.get("LoginName"),
                   "auth_method": "kerberos" if self.kerberos else "ntlm", "list_count": len(lists)}
        if not lists:
            return {"success": True, "message": "Connected to SharePoint Server — no lists in scope on this site.",
                    "details": details}
        names = ", ".join(str(l.get("Title") or "?") for l in lists[:5])
        more = f" (+{len(lists) - 5} more)" if len(lists) > 5 else ""
        return {"success": True,
                "message": f"Connected to SharePoint Server. Found {len(lists)} list{'s' if len(lists) != 1 else ''}: {names}{more}.",
                "details": details}

    # ------------------------------------------------------------- prompts

    def system_prompt(self) -> str:
        return textwrap.dedent(f"""
        ## SharePoint Server Lists
        Query a list with `client.execute_query(query)` where `query` is a JSON string:

        ```json
        {{"list": "Expenses",
         "filter": "Category eq 'Travel' and Amount gt 100",
         "select": ["Title", "Category", "Amount", "Modified"],
         "orderby": "Modified desc",
         "limit": 1000}}
        ```

        - `list` (required): the list name exactly as cataloged.
        - `filter` (optional): an OData expression over the cataloged column names.
          Operators: eq, ne, gt, ge, lt, le, and, or, not, parentheses;
          functions startswith(Col,'x'), endswith(Col,'x'), contains(Col,'x'),
          tolower(Col), year(Col), month(Col), day(Col).
          Text in single quotes (double a quote to escape it), compared case-insensitively.
          Dates as ISO-8601 UTC: Modified ge '2026-01-01T00:00:00Z'. Empty values: Col eq null.
          A multi-value column matches `eq` when any of its values does.
        - `select` (optional): columns to return; omit for all cataloged columns.
        - `orderby` (optional): e.g. "Amount desc, Title".
        - `limit` (optional): max rows (default {DEFAULT_LIMIT:,}, up to {self.max_items:,}).

        The filter and sort run after the rows are read (SharePoint Server refuses
        server-side filters on large lists), so any column works in a filter. A query
        reads at most {self.max_items:,} items; when a list is larger the result carries a
        note saying how much was covered.

        Returns a pandas DataFrame with an `id` column plus the selected columns,
        named as cataloged. Numbers are numeric; dates are ISO-8601 UTC strings
        (parse with pd.to_datetime(..., utc=True)); person and lookup columns hold
        display names; multi-value columns are joined with "; ".
        Aggregate in pandas (SharePoint has no server-side aggregation).

        Examples:
        ```python
        df = client.execute_query('{{"list": "Expenses", "select": ["Title", "Category", "Amount"]}}')
        df = client.execute_query('{{"list": "Issues", "filter": "Status ne \\'Closed\\' and startswith(Title,\\'VPN\\')", "orderby": "Created desc", "limit": 200}}')
        ```
        """).strip()
