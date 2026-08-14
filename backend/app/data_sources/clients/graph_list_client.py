"""Microsoft Graph SharePoint *Lists* client.

Backs the `sharepoint_lists` data source type. Where the `sharepoint` type
(graph_drive_client) surfaces a site's document libraries as FILES, this one
surfaces the site's SharePoint lists as TABLES: every list becomes a catalog
`Table` with typed columns, and `execute_query()` takes a JSON spec that maps
1:1 to a Graph list-items call with an OData `$filter` — so the standard
tables agent path (schema context + create_data codegen) works unchanged.

Auth is identical to the file connector: delegated per-user OAuth by default
("Sign in with Microsoft"), with the admin's Entra app as the service
principal. Unlike drives, Graph list reads also work app-only when the app
holds `Sites.Read.All` application permission.

SharePoint quirk this client exists to hide: a list column's *internal* name
rarely matches what users see. A column displayed as "2017" is internally
`field_2` (or `_x0032_017`), and Graph's `$filter`/`$select`/`fields` all
speak internal names. The catalog therefore presents DISPLAY names (what the
model and user see), and this client translates display → internal on the way
out and internal → display on the way back.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients.base import Capability
from app.data_sources.clients.graph_drive_client import GRAPH_BASE, GraphDriveClient

logger = logging.getLogger(__name__)

# Graph page size for /items — 200 is the practical per-page max for list items.
ITEMS_PAGE_SIZE = 200
DEFAULT_LIMIT = 1000
MAX_ROWS = 20000

# Without this header Graph rejects $filter / $orderby on any column that is
# not indexed in SharePoint (HTTP 400) — and user-created list columns are
# almost never indexed.
PREFER_NONINDEXED = "HonorNonIndexedQueriesWarningMayFailRandomly"

# List templates that are not tabular business data. Document libraries are the
# `sharepoint` (files) connector's domain; the rest are site plumbing.
_EXCLUDED_TEMPLATES = {
    "documentLibrary",
    "webPageLibrary",
    "webTemplateExtensionsList",
    "form",
    "picture",
    "appDataCatalog",
    "sharingLinks",
    "accessRequest",
}

# System columns that are read-only but still analytically useful.
_USEFUL_READONLY = {"ID", "Modified", "Created", "Author", "Editor"}

# Read/write system columns that are noise in a data catalog.
_EXCLUDED_COLUMNS = {
    "ContentType", "Attachments", "Edit", "LinkTitle", "LinkTitleNoMenu",
    "DocIcon", "ItemChildCount", "FolderChildCount", "ComplianceAssetId",
    "AppAuthor", "AppEditor", "Order",
}

# Graph column facet → catalog dtype.
_FACET_DTYPES = [
    ("number", "float"),
    ("currency", "float"),
    ("dateTime", "datetime"),
    ("boolean", "boolean"),
    ("choice", "string"),
    ("text", "string"),
    ("personOrGroup", "string"),
    ("lookup", "string"),
    ("calculated", "string"),
    ("hyperlinkOrPicture", "string"),
]


def _column_dtype(col: dict) -> Optional[str]:
    for facet, dtype in _FACET_DTYPES:
        if facet in col:
            if facet == "calculated":
                out = (col.get("calculated") or {}).get("outputType") or ""
                return {"number": "float", "currency": "float",
                        "dateTime": "datetime", "boolean": "boolean"}.get(out, "string")
            return dtype
    return None


class SharepointListsClient(GraphDriveClient):
    """SharePoint Lists — every list on the site becomes a queryable table."""

    capabilities = {Capability.QUERY}

    # OData datetime literals are absolute; generated code should compute
    # relative dates in Python and inline the ISO value.
    relative_date_hint = (
        "OData datetime literals are absolute ISO-8601 UTC (e.g. "
        "fields/Modified ge '2026-01-01T00:00:00Z'); compute relative dates in "
        "Python (datetime.now(timezone.utc) - timedelta(days=7)) and inline the ISO string."
    )

    def __init__(
        self,
        lists: Optional[str] = None,
        include_hidden: bool = False,
        max_items: int = MAX_ROWS,
        **kwargs,
    ):
        kwargs["mode"] = "sharepoint"
        super().__init__(**kwargs)
        # Comma-separated list names to scope the connection; '*' / blank = all.
        raw = (lists or "").strip()
        if raw and raw != "*":
            self.lists_scope = [s.strip().lower() for s in raw.split(",") if s.strip()]
        else:
            self.lists_scope = None
        self.include_hidden = bool(include_hidden)
        try:
            self.max_items = max(1, min(int(max_items or MAX_ROWS), MAX_ROWS))
        except (TypeError, ValueError):
            self.max_items = MAX_ROWS

        # Caches (per client instance — clients are built per request/run).
        self._lists_cache: Optional[List[dict]] = None
        self._columns_cache: Dict[str, List[dict]] = {}

    # ------------------------------------------------------------- identity

    @property
    def is_document_based(self) -> bool:
        return False

    @property
    def description(self) -> str:
        text = (
            f"SharePoint Lists on site {self.site_url} — each list is a table; "
            "query via execute_query with a JSON spec (OData filters)."
        )
        return text + "\n\n" + self.system_prompt()

    # ------------------------------------------------------------ discovery

    def _fetch_lists(self) -> List[dict]:
        """Every list on the site (paged), with its `list` facet."""
        if self._lists_cache is not None:
            return self._lists_cache
        site_id = self._resolve_site_id()
        out: List[dict] = []
        url: Optional[str] = (
            f"{GRAPH_BASE}/sites/{site_id}/lists"
            "?$select=id,name,displayName,description,webUrl,list"
        )
        while url:
            data = self._get(url)
            out.extend(data.get("value", []) or [])
            url = data.get("@odata.nextLink")
        self._lists_cache = out
        return out

    def _included_lists(self) -> List[dict]:
        """The lists this connection spans, after template/hidden/name scoping.

        Multiple lists per site is the normal case — with no `lists` scope the
        connection catalogs every qualifying list on the site.
        """
        included = []
        for l in self._fetch_lists():
            facet = l.get("list") or {}
            if facet.get("template") in _EXCLUDED_TEMPLATES:
                continue
            if facet.get("hidden") and not self.include_hidden:
                continue
            if self.lists_scope is not None:
                names = {
                    (l.get("displayName") or "").lower(),
                    (l.get("name") or "").lower(),
                    (l.get("id") or "").lower(),
                }
                if not (names & set(self.lists_scope)):
                    continue
            included.append(l)
        # Stable order so the catalog is deterministic across runs.
        return sorted(included, key=lambda l: (l.get("displayName") or l.get("name") or "").lower())

    def _fetch_columns(self, list_id: str) -> List[dict]:
        """Catalog-worthy columns of a list: [{internal, display, dtype, read_only}]."""
        if list_id in self._columns_cache:
            return self._columns_cache[list_id]
        site_id = self._resolve_site_id()
        raw: List[dict] = []
        url: Optional[str] = f"{GRAPH_BASE}/sites/{site_id}/lists/{list_id}/columns"
        while url:
            data = self._get(url)
            raw.extend(data.get("value", []) or [])
            url = data.get("@odata.nextLink")

        cols: List[dict] = []
        seen_display: Dict[str, int] = {}
        for c in raw:
            internal = c.get("name") or ""
            if not internal or internal.startswith("_") or internal in _EXCLUDED_COLUMNS:
                continue
            if c.get("hidden"):
                continue
            dtype = _column_dtype(c)
            if c.get("readOnly") and internal not in _USEFUL_READONLY:
                continue
            if internal == "ID":
                dtype = "integer"
            if dtype is None:
                continue
            display = (c.get("displayName") or internal).strip() or internal
            # Two columns can share a display name; qualify duplicates with the
            # internal name so the mapping stays invertible.
            if display.lower() in seen_display:
                display = f"{display} ({internal})"
            seen_display[display.lower()] = 1
            cols.append({
                "internal": internal,
                "display": display,
                "dtype": dtype,
                "read_only": bool(c.get("readOnly")),
                "person": "personOrGroup" in c,
            })
        self._columns_cache[list_id] = cols
        return cols

    # ------------------------------------------------------------- catalog

    def get_schemas(self, progress_callback=None) -> List[Table]:
        try:
            lists = self._included_lists()
        except Exception as e:
            # App-only crawl without Sites.Read.All application permission is
            # the expected pre-sign-in state — an empty catalog, not a failure.
            # Per-user catalogs populate after each user completes OAuth.
            if not self._user_token_provided and any(
                code in str(e) for code in ("401", "403", "Unauthorized", "accessDenied")
            ):
                logger.info(
                    "sharepoint_lists: app-only catalog crawl denied (%s); "
                    "catalog will populate per user after sign-in.", str(e)[:160],
                )
                return []
            raise

        tables: List[Table] = []
        seen_names: Dict[str, int] = {}
        for l in lists:
            cols = self._fetch_columns(l["id"])
            name = l.get("displayName") or l.get("name") or l["id"]
            if name.lower() in seen_names:
                name = f"{name} ({l.get('name')})"
            seen_names[name.lower()] = 1
            tables.append(Table(
                name=name,
                description=(
                    f"SharePoint list '{name}'"
                    + (f" — {l.get('description')}" if l.get("description") else "")
                ),
                columns=[TableColumn(name=c["display"], dtype=c["dtype"]) for c in cols],
                pks=[TableColumn(name="ID", dtype="integer")],
                fks=[],
                metadata_json={
                    "sharepoint_list": {
                        "list_id": l["id"],
                        "list_name": l.get("name"),
                        "web_url": l.get("webUrl"),
                        "template": (l.get("list") or {}).get("template"),
                        # display → internal, the mapping queries need.
                        "internal_names": {c["display"]: c["internal"] for c in cols},
                    }
                },
            ))
        return tables

    def get_schema(self, table_name: str) -> Optional[Table]:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        return None

    def prompt_schema(self) -> str:
        from app.ai.prompt_formatters import ServiceFormatter
        tables = self.get_schemas()
        if not tables:
            return "No SharePoint lists available (sign in with Microsoft to load them)."
        return ServiceFormatter(tables).table_str

    # ------------------------------------------------------------- querying

    def _resolve_list(self, ref: str) -> dict:
        """Resolve a list by display name, internal name, or id."""
        ref_l = (ref or "").strip().lower()
        if not ref_l:
            raise ValueError("Query spec must name a 'list'.")
        candidates = self._included_lists()
        for l in candidates:
            if ref_l in (
                (l.get("displayName") or "").lower(),
                (l.get("name") or "").lower(),
                (l.get("id") or "").lower(),
            ):
                return l
        # Fall back to a startswith/contains match so "expense data" resolves.
        for l in candidates:
            if ref_l in (l.get("displayName") or "").lower():
                return l
        names = ", ".join(sorted((l.get("displayName") or l.get("name") or "?") for l in candidates))
        raise ValueError(f"SharePoint list '{ref}' not found. Available lists: {names}")

    @staticmethod
    def _parse_spec(query) -> dict:
        if isinstance(query, dict):
            return dict(query)
        text = str(query or "").strip()
        if text.startswith("{"):
            try:
                spec = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON query spec: {e}")
            if not isinstance(spec, dict):
                raise ValueError("Query spec must be a JSON object.")
            return spec
        # A bare string is treated as a list name — fetch everything.
        return {"list": text}

    def _translate_fields_expr(self, expr: str, cols: List[dict]) -> str:
        """Rewrite display names to internal names inside a $filter/$orderby.

        Handles both `fields/Name` references and bare column names. Longer
        display names are replaced first so "Total 2017" wins over "2017".
        """
        out = expr
        ordered = sorted(cols, key=lambda c: -len(c["display"]))
        for c in ordered:
            if c["display"] == c["internal"]:
                continue
            out = out.replace(f"fields/{c['display']}", f"fields/{c['internal']}")
            out = re.sub(rf"(?<![\w/]){re.escape(c['display'])}(?![\w])", c["internal"], out)
        # Bare internal/display refs without the fields/ prefix → prefix them.
        internals = {c["internal"] for c in cols}
        def _prefix(m):
            tok = m.group(0)
            return f"fields/{tok}" if tok in internals else tok
        out = re.sub(r"(?<![\w/'])[A-Za-z_][\w]*(?![\w/'(])", _prefix, out)
        return out

    def execute_query(self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs) -> pd.DataFrame:
        """Execute a SharePoint list query spec and return a DataFrame.

        `query` is a JSON string (or dict):
            {"list": "2017_Expense_Data",              (required)
             "filter": "Category eq 'Expenses'",       (optional OData $filter)
             "select": ["Title", "Category", "2017"],  (optional columns)
             "orderby": "Modified desc",               (optional)
             "limit": 1000}                             (optional)

        Column names may be display names (as cataloged); they are translated
        to SharePoint's internal names for the request and back for the result.
        """
        spec = self._parse_spec(query if query is not None else table_name)
        if table_name and "list" not in spec:
            spec["list"] = table_name
        target = self._resolve_list(spec.get("list") or "")
        cols = self._fetch_columns(target["id"])
        by_display = {c["display"].lower(): c for c in cols}
        by_internal = {c["internal"].lower(): c for c in cols}

        def _col(ref: str) -> Optional[dict]:
            r = (ref or "").strip().lower()
            return by_display.get(r) or by_internal.get(r)

        # Columns to fetch / return (display order preserved).
        selected: List[dict]
        if spec.get("select"):
            raw_sel = spec["select"]
            if isinstance(raw_sel, str):
                raw_sel = [s.strip() for s in raw_sel.split(",") if s.strip()]
            selected, unknown = [], []
            for ref in raw_sel:
                c = _col(ref)
                (selected.append(c) if c else unknown.append(ref))
            if unknown:
                known = ", ".join(c["display"] for c in cols)
                raise ValueError(
                    f"Unknown column(s) {unknown} on list '{target.get('displayName')}'. "
                    f"Available columns: {known}"
                )
        else:
            selected = list(cols)

        limit = int(spec.get("limit") or DEFAULT_LIMIT)
        limit = max(1, min(limit, self.max_items))

        site_id = self._resolve_site_id()
        internal_sel = ",".join(dict.fromkeys(c["internal"] for c in selected))
        params: Dict[str, str] = {
            "$expand": f"fields($select={internal_sel})",
        }
        if spec.get("filter"):
            params["$filter"] = self._translate_fields_expr(str(spec["filter"]), cols)
        if spec.get("orderby"):
            params["$orderby"] = self._translate_fields_expr(str(spec["orderby"]), cols)

        rows: List[dict] = []
        url: Optional[str] = f"{GRAPH_BASE}/sites/{site_id}/lists/{target['id']}/items"
        first = True
        started = time.perf_counter()
        while url and len(rows) < limit:
            page_params = dict(params) if first else None
            if first:
                page_params["$top"] = str(min(ITEMS_PAGE_SIZE, limit))
            try:
                data = self._get_with_prefer(url, page_params)
            except ValueError as e:
                msg = str(e)
                if params.get("$filter") and ("filter" in msg.lower() or "index" in msg.lower() or "400" in msg):
                    raise ValueError(
                        f"SharePoint rejected the OData filter ({msg[:200]}). "
                        "This usually means the column is not indexed in SharePoint. "
                        "Retry WITHOUT 'filter' (and without 'orderby'), then filter/sort "
                        "the returned DataFrame in pandas instead."
                    )
                raise
            for item in data.get("value", []) or []:
                rows.append(self._item_to_row(item, selected))
                if len(rows) >= limit:
                    break
            url = data.get("@odata.nextLink")
            first = False

        display_cols = ["id"] + [c["display"] for c in selected]
        df = pd.DataFrame(rows)
        # Stable, schema-shaped frame even when Graph omits null fields or the
        # list is empty — downstream chart code indexes by column name.
        df = df.reindex(columns=display_cols)
        logger.info(
            "sharepoint_lists.execute_query: list=%r filter=%r rows=%d in %.1fs",
            target.get("displayName"), spec.get("filter"), len(df),
            time.perf_counter() - started,
        )
        return df

    def _get_with_prefer(self, url: str, params: Optional[Dict[str, str]]) -> dict:
        """GET with the non-indexed-query Prefer header (kept off _get so the
        inherited file paths stay byte-identical)."""
        client = self._client()
        headers = {**self._headers(), "Prefer": PREFER_NONINDEXED}
        resp = client.get(url, headers=headers, params=params, timeout=45)
        if resp.status_code == 401 and self._can_remint_token():
            self.access_token = None
            headers = {**self._headers(), "Prefer": PREFER_NONINDEXED}
            resp = client.get(url, headers=headers, params=params, timeout=45)
        if resp.status_code >= 400:
            raise ValueError(f"Graph {url} → {resp.status_code} {resp.text[:300]}")
        return resp.json()

    @staticmethod
    def _item_to_row(item: dict, selected: List[dict]) -> dict:
        fields = item.get("fields") or {}
        row: Dict[str, Any] = {"id": item.get("id")}
        for c in selected:
            internal, display = c["internal"], c["display"]
            if internal == "ID":
                row[display] = item.get("id")
                continue
            # Person columns: fields carry only `<name>LookupId`; the readable
            # identity for Author/Editor lives on the item envelope.
            if c.get("person"):
                envelope = {"Author": "createdBy", "Editor": "lastModifiedBy"}.get(internal)
                if envelope and item.get(envelope):
                    row[display] = ((item[envelope] or {}).get("user") or {}).get("displayName")
                    continue
                row[display] = fields.get(internal, fields.get(f"{internal}LookupId"))
                continue
            if internal in fields:
                row[display] = fields.get(internal)
            elif internal == "Created":
                row[display] = item.get("createdDateTime")
            elif internal == "Modified":
                row[display] = item.get("lastModifiedDateTime")
            else:
                row[display] = fields.get(f"{internal}LookupId")
        return row

    # ------------------------------------------------------------- testing

    def test_connection(self) -> dict:
        """Bounded: token + site + one lists page. Never reads items."""
        t0 = time.perf_counter()
        timings: Dict[str, float] = {}
        details: Dict[str, Any] = {
            "mode": "sharepoint_lists",
            "auth": "delegated" if self._user_token_provided else "service_principal",
        }

        def _step(key: str, fn):
            started = time.perf_counter()
            try:
                return fn()
            finally:
                timings[key] = round((time.perf_counter() - started) * 1000, 1)

        def _result(success: bool, message: str) -> dict:
            timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return {"success": success, "message": message, "timings": timings, "details": details}

        try:
            _step("token_ms", self._token)
            details["site_id"] = _step("site_ms", self._resolve_site_id)
            try:
                lists = _step("lists_ms", self._included_lists)
            except Exception as e:
                if not self._user_token_provided:
                    # App-only without Sites.Read.All application permission —
                    # the normal admin-save state for delegated deployments.
                    return _result(
                        True,
                        "Service principal verified and site is reachable. "
                        "Have a user sign in with Microsoft to load the site's lists.",
                    )
                raise e
            details["list_count"] = len(lists)
            names = ", ".join((l.get("displayName") or l.get("name") or "?") for l in lists[:5])
            more = f" (+{len(lists) - 5} more)" if len(lists) > 5 else ""
            if not lists:
                return _result(True, "Connected — no qualifying lists found on the site.")
            return _result(
                True,
                f"Connected. Found {len(lists)} list{'s' if len(lists) != 1 else ''}: {names}{more}.",
            )
        except Exception as e:
            return _result(False, str(e))

    # ------------------------------------------------------------- prompts

    def system_prompt(self) -> str:
        return """
        ## SharePoint Lists Integration
        Query lists via `client.execute_query(query)` where `query` is a JSON string:

        ```json
        {"list": "2017_Expense_Data",
         "filter": "Category eq 'Expenses' and 2017 gt 100",
         "select": ["Title", "Category", "2017"],
         "orderby": "Modified desc",
         "limit": 1000}
        ```

        - `list` (required): the list name exactly as cataloged.
        - `filter` (optional): an OData expression over the list's columns.
          Operators: eq, ne, gt, ge, lt, le, and, or, not, startswith(Col,'x').
          Strings in single quotes; dates as ISO-8601 UTC ('2026-01-01T00:00:00Z').
          Use the cataloged (display) column names — internal-name mapping is automatic.
        - `select` (optional): columns to return; omit for all cataloged columns.
        - `orderby` (optional): e.g. "Modified desc".
        - `limit` (optional): max rows (default 1000).

        Returns a pandas DataFrame whose columns are the cataloged display
        names plus `id`. Values keep their SharePoint types (numbers are
        numeric, dates are ISO strings — parse with pd.to_datetime).
        Person columns return display names as strings.

        If a `filter` fails because the column is not indexed in SharePoint,
        re-run WITHOUT `filter`/`orderby` and filter/sort in pandas — lists are
        usually small enough to fetch whole. Aggregate in pandas as well
        (SharePoint has no server-side aggregation).

        Examples:
        ```python
        df = client.execute_query('{"list": "2017_Expense_Data", "select": ["Title", "Category", "2017"]}')
        df = client.execute_query('{"list": "Mock_Salesforce_Leads", "filter": "startswith(Company,\\'A\\')", "limit": 500}')
        ```
        """
