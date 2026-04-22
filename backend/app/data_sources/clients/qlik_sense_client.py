"""
Qlik Sense Cloud client.

Discovers Qlik apps (models) via the Qlik Cloud REST API and runs hypercube
queries against them via the Qlik Engine API (QIX), a JSON-RPC 2.0 protocol
spoken over WebSocket.

Scope of v1:
  - Qlik Cloud (tenant.REGION.qlikcloud.com) with API-key bearer auth
  - REST discovery: GET /api/v1/users/me, /api/v1/items?resourceType=app,
    /api/v1/apps/{appId}/data/metadata
  - QIX fallback + query: OpenDoc, GetTablesAndKeys, GetFieldList,
    CreateSessionObject(qHyperCubeDef), GetHyperCubeData

Out of scope for v1: on-prem Enterprise on Windows, OAuth2 Authorization Code,
M2M impersonation, JWT — all hooks are structured to slot in later without
rewriting the discovery/query core.
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient


logger = logging.getLogger(__name__)


class QlikSenseClient(DataSourceClient):
    """Qlik Sense Cloud client — REST discovery + QIX query over WebSocket."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        verify_ssl: bool = True,
        timeout_sec: int = 30,
        space_filter: Optional[str] = None,
        max_concurrency: int = 10,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self.timeout_sec = timeout_sec
        self.max_concurrency = max(1, int(max_concurrency or 10))
        # Parse the space filter into a set of tokens (IDs or names)
        self._space_filter: Optional[set] = None
        if space_filter:
            tokens = {t.strip() for t in str(space_filter).split(",") if t.strip()}
            self._space_filter = tokens or None

        self._http: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Set up the REST HTTP session. QIX connections are opened per call."""
        if self._http is not None:
            return
        if not self.base_url:
            raise RuntimeError("base_url is required")
        if not self.api_key:
            raise RuntimeError("api_key is required")
        self._http = requests.Session()

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _rest_get(self, path_or_url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET a REST endpoint; accepts a path like '/api/v1/items' or a full URL."""
        self.connect()
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        resp = self._http.get(
            url,
            params=params,
            headers=self._auth_headers(),
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Qlik Cloud REST call failed: GET {url} HTTP {resp.status_code} {resp.text[:500]}"
            )
        if not resp.content:
            return {}
        try:
            return resp.json() or {}
        except ValueError as e:
            raise RuntimeError(f"Invalid JSON from Qlik Cloud ({url}): {e}")

    def test_connection(self) -> Dict[str, Any]:
        """
        Fast connection test: /users/me (validates token) + /items?limit=1
        (validates apps.read + returns a cheap app count). O(1) in tenant size.
        """
        try:
            self.connect()
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}

        try:
            me = self._rest_get("/api/v1/users/me")
        except Exception as e:
            return {"success": False, "message": f"Authentication failed: {e}"}

        try:
            items = self._rest_get(
                "/api/v1/items",
                params={"resourceType": "app", "limit": 1},
            )
        except Exception as e:
            return {
                "success": False,
                "connectivity": True,
                "message": f"Authenticated but failed to list apps: {e}",
            }

        # We intentionally ask for limit=1 to keep this O(1). That means the
        # "data" length is at most 1 — it indicates presence, not a real count.
        has_apps = bool(items.get("data"))
        has_more = bool((items.get("links") or {}).get("next"))

        if has_apps:
            visibility = "1+" if has_more else "1"
            msg = (
                f"Connected to Qlik Cloud as '{me.get('email') or me.get('id') or 'user'}'. "
                f"Sampled {visibility} app(s) visible to this key."
            )
        else:
            msg = (
                f"Connected to Qlik Cloud as '{me.get('email') or me.get('id') or 'user'}'. "
                "Tenant has no apps visible to this key — grant the key 'apps.read' scope and workspace access."
            )
        return {
            "success": True,
            "message": msg,
            "user": me.get("email") or me.get("id"),
            "has_apps": has_apps,
            "has_more": has_more,
        }

    # ------------------------------------------------------------------
    # App enumeration (REST)
    # ------------------------------------------------------------------

    def list_apps(self) -> List[Dict[str, Any]]:
        """
        Enumerate all apps via /api/v1/items?resourceType=app, following
        pagination via links.next. Filters by space when QlikSenseConfig.space_filter
        is configured.
        """
        self.connect()
        apps: List[Dict[str, Any]] = []
        url: Optional[str] = None
        params: Optional[Dict[str, Any]] = {"resourceType": "app", "limit": 100}

        while True:
            if url is not None:
                payload = self._rest_get(url)
            else:
                payload = self._rest_get("/api/v1/items", params=params)

            for item in payload.get("data") or []:
                if self._space_filter is not None:
                    sp_id = item.get("spaceId") or ""
                    sp_name = item.get("spaceName") or ""
                    if sp_id not in self._space_filter and sp_name not in self._space_filter:
                        continue
                apps.append({
                    "id": item.get("resourceId") or item.get("id"),
                    "item_id": item.get("id"),
                    "name": item.get("name") or item.get("resourceId"),
                    "space_id": item.get("spaceId"),
                    "space_name": item.get("spaceName"),
                    "updated_at": item.get("updatedAt"),
                    "owner": item.get("ownerId"),
                })

            next_link = ((payload.get("links") or {}).get("next") or {}).get("href")
            if not next_link:
                break
            url = next_link
            params = None

        return apps

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _rest_metadata_has_content(payload: Optional[Dict[str, Any]]) -> bool:
        if not payload:
            return False
        return bool(payload.get("tables")) and bool(payload.get("fields"))

    @staticmethod
    def _build_tables_from_rest_metadata(
        app: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> List[Table]:
        """Convert REST /apps/{id}/data/metadata into Table objects."""
        app_id = app.get("id")
        app_name = app.get("name") or app_id or ""
        space_name = app.get("space_name") or ""
        app_key = f"{space_name}/{app_name}" if space_name else app_name

        # Build an index: table name -> list of (field_name, tags)
        fields_by_table: Dict[str, List[Dict[str, Any]]] = {}
        for field in metadata.get("fields") or []:
            src_tables = field.get("srcTables") or []
            for t in src_tables:
                fields_by_table.setdefault(t, []).append({
                    "name": field.get("name") or "",
                    "tags": field.get("tags") or [],
                    "is_key": "$key" in (field.get("tags") or []),
                })

        # Cross-table key fields become approximate relationships — every table
        # that shares a key field is related via that field. Build pks from $key.
        key_to_tables: Dict[str, List[str]] = {}
        for field in metadata.get("fields") or []:
            if "$key" in (field.get("tags") or []):
                for t in field.get("srcTables") or []:
                    key_to_tables.setdefault(field.get("name") or "", []).append(t)

        tables: List[Table] = []
        for tbl in metadata.get("tables") or []:
            tbl_name = tbl.get("name") or ""
            if not tbl_name:
                continue
            full_name = f"{app_key}/{tbl_name}" if app_key else tbl_name

            columns: List[TableColumn] = []
            pks: List[TableColumn] = []
            for f in fields_by_table.get(tbl_name, []):
                col = TableColumn(
                    name=f["name"],
                    dtype=_tags_to_dtype(f["tags"]),
                    description=None,
                    metadata={"qlik_tags": f["tags"]},
                )
                columns.append(col)
                if f["is_key"]:
                    pks.append(col)

            fks: List[ForeignKey] = []
            for f in fields_by_table.get(tbl_name, []):
                if not f["is_key"]:
                    continue
                other_tables = [t for t in key_to_tables.get(f["name"], []) if t != tbl_name]
                for other in other_tables:
                    fks.append(ForeignKey(
                        column=TableColumn(name=f["name"], dtype=_tags_to_dtype(f["tags"])),
                        references_name=(f"{app_key}/{other}" if app_key else other),
                        references_column=TableColumn(name=f["name"], dtype=_tags_to_dtype(f["tags"])),
                    ))

            tables.append(Table(
                name=full_name,
                description=None,
                columns=columns,
                pks=pks,
                fks=fks,
                is_active=True,
                metadata_json={
                    "qlik_sense": {
                        "appId": app_id,
                        "appName": app_name,
                        "spaceId": app.get("space_id"),
                        "spaceName": space_name,
                        "tableName": tbl_name,
                        "rowCount": tbl.get("rows"),
                        "source": "rest_metadata",
                    }
                },
            ))

        return tables

    @staticmethod
    def _build_tables_from_qix(
        app: Dict[str, Any],
        qtr: List[Dict[str, Any]],
        qk: List[Dict[str, Any]],
    ) -> List[Table]:
        """Convert a QIX GetTablesAndKeys result into Table objects."""
        app_id = app.get("id")
        app_name = app.get("name") or app_id or ""
        space_name = app.get("space_name") or ""
        app_key = f"{space_name}/{app_name}" if space_name else app_name

        # Index keys (association links) by table name
        keys_by_table: Dict[str, List[Dict[str, Any]]] = {}
        for key in qk or []:
            key_fields = key.get("qKeyFields") or []
            tables_for_key = key.get("qTables") or []
            for t in tables_for_key:
                keys_by_table.setdefault(t, []).append({
                    "fields": key_fields,
                    "other_tables": [o for o in tables_for_key if o != t],
                })

        tables: List[Table] = []
        for tbl in qtr or []:
            tbl_name = tbl.get("qName") or ""
            if not tbl_name:
                continue
            full_name = f"{app_key}/{tbl_name}" if app_key else tbl_name

            key_fields_set: set = set()
            for entry in keys_by_table.get(tbl_name, []):
                for kf in entry["fields"]:
                    key_fields_set.add(kf)

            columns: List[TableColumn] = []
            pks: List[TableColumn] = []
            for field in tbl.get("qFields") or []:
                fname = field.get("qName") or ""
                if not fname:
                    continue
                tags = field.get("qTags") or []
                col = TableColumn(
                    name=fname,
                    dtype=_tags_to_dtype(tags),
                    description=None,
                    metadata={"qlik_tags": tags},
                )
                columns.append(col)
                if fname in key_fields_set:
                    pks.append(col)

            fks: List[ForeignKey] = []
            for entry in keys_by_table.get(tbl_name, []):
                for kf in entry["fields"]:
                    for other in entry["other_tables"]:
                        fks.append(ForeignKey(
                            column=TableColumn(name=kf, dtype="key"),
                            references_name=(f"{app_key}/{other}" if app_key else other),
                            references_column=TableColumn(name=kf, dtype="key"),
                        ))

            tables.append(Table(
                name=full_name,
                description=None,
                columns=columns,
                pks=pks,
                fks=fks,
                is_active=True,
                metadata_json={
                    "qlik_sense": {
                        "appId": app_id,
                        "appName": app_name,
                        "spaceId": app.get("space_id"),
                        "spaceName": space_name,
                        "tableName": tbl_name,
                        "rowCount": tbl.get("qNoOfRows"),
                        "source": "qix",
                    }
                },
            ))
        return tables

    def _crawl_app(self, app: Dict[str, Any]) -> List[Table]:
        """
        Per-app crawl: REST fast path, fall back to QIX if REST metadata is empty.
        Errors are converted to a stub Table row so the overall crawl never aborts.
        """
        app_id = app.get("id")
        app_name = app.get("name") or app_id or ""
        space_name = app.get("space_name") or ""
        app_key = f"{space_name}/{app_name}" if space_name else app_name

        rest_error: Optional[Exception] = None
        rest_meta: Optional[Dict[str, Any]] = None
        try:
            rest_meta = self._rest_get(f"/api/v1/apps/{app_id}/data/metadata")
        except Exception as e:
            rest_error = e
            logger.debug("REST metadata for app %s failed: %s", app_id, e)

        if self._rest_metadata_has_content(rest_meta):
            try:
                return self._build_tables_from_rest_metadata(app, rest_meta)
            except Exception as e:
                rest_error = e

        qix_error: Optional[Exception] = None
        qtr: List[Dict[str, Any]] = []
        qk: List[Dict[str, Any]] = []
        try:
            qtr, qk = self._qix_get_tables_and_keys(app_id)
        except Exception as e:
            qix_error = e

        if qtr:
            try:
                return self._build_tables_from_qix(app, qtr, qk)
            except Exception as e:
                qix_error = e

        if rest_error is not None or qix_error is not None:
            err = rest_error or qix_error
            logger.warning("Qlik app crawl failed for %s (%s): %s", app_name, app_id, err)
            stub_name = app_key if app_key else (app_id or "unknown-app")
            return [Table(
                name=stub_name,
                description=f"Qlik app failed to crawl: {err}",
                columns=[],
                pks=[],
                fks=[],
                is_active=False,
                metadata_json={
                    "qlik_sense": {
                        "appId": app_id,
                        "appName": app_name,
                        "spaceName": space_name,
                        "status": "error",
                        "error": str(err),
                    }
                },
            )]

        return []

    def get_schemas(self) -> List[Table]:
        """
        Full-tenant crawl: list_apps() then parallel per-app REST metadata,
        with QIX fallback for apps that don't expose REST metadata.
        """
        apps = self.list_apps()
        if not apps:
            return []

        tables: List[Table] = []
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            futures = {pool.submit(self._crawl_app, app): app for app in apps}
            for fut in as_completed(futures):
                try:
                    tables.extend(fut.result() or [])
                except Exception as e:
                    app = futures[fut]
                    logger.warning("Unhandled crawl exception for %s: %s", app.get("id"), e)
        return tables

    def get_schema(self, table_name: str) -> Table:
        """Find a single Table by its 'Space/App/Table' name or metadata keys."""
        all_tables = self.get_schemas()

        for tbl in all_tables:
            if tbl.name == table_name:
                return tbl

        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get("qlik_sense") or {}
            if meta.get("tableName") == table_name:
                return tbl

        for tbl in all_tables:
            meta = (tbl.metadata_json or {}).get("qlik_sense") or {}
            if meta.get("appId") == table_name or meta.get("appName") == table_name:
                return tbl

        raise RuntimeError(f"Table not found for '{table_name}'")

    # ------------------------------------------------------------------
    # QIX (Engine API, JSON-RPC 2.0 over WebSocket)
    # ------------------------------------------------------------------

    def _ws_url_for_app(self, app_id: str) -> str:
        """Build the WebSocket URL for opening a specific app."""
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
        netloc = parsed.netloc or parsed.path.lstrip("/")
        return urlunparse((scheme, netloc, f"/app/{app_id}", "", "", ""))

    def _qix_get_tables_and_keys(self, app_id: str):
        """
        Open the app and call GetTablesAndKeys in a single WebSocket session.

        Returns (qtr, qk) lists from the result. On failure returns ([], []).
        """
        try:
            return asyncio.run(self._qix_get_tables_and_keys_async(app_id))
        except Exception as e:
            logger.debug("QIX GetTablesAndKeys failed for %s: %s", app_id, e)
            return [], []

    async def _qix_get_tables_and_keys_async(self, app_id: str):
        import ssl
        import websockets

        url = self._ws_url_for_app(app_id)
        ssl_ctx: Any
        if url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not self.verify_ssl:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        else:
            ssl_ctx = None
        headers = [("Authorization", f"Bearer {self.api_key}")]

        async with websockets.connect(
            url,
            extra_headers=headers,
            ssl=ssl_ctx,
            open_timeout=self.timeout_sec,
            close_timeout=5,
        ) as ws:

            async def rpc(handle: int, method: str, params) -> Dict[str, Any]:
                payload = {
                    "jsonrpc": "2.0",
                    "id": rpc.counter,
                    "handle": handle,
                    "method": method,
                    "params": params,
                }
                rpc.counter += 1
                await ws.send(json.dumps(payload))
                raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout_sec)
                msg = json.loads(raw)
                if "error" in msg:
                    err = msg["error"] or {}
                    raise RuntimeError(f"Qlik QIX error on {method}: {err.get('message') or err}")
                return msg.get("result") or {}

            rpc.counter = 1
            opendoc = await rpc(-1, "OpenDoc", [app_id])
            doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
            if doc_handle is None:
                return [], []
            tk = await rpc(doc_handle, "GetTablesAndKeys", [
                {"qcx": 1000, "qcy": 1000}, {"qcx": 0, "qcy": 0}, 0, True, False,
            ])
            return tk.get("qtr") or [], tk.get("qk") or []

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute_query(
        self,
        app: Optional[str] = None,
        dimensions: Optional[List[str]] = None,
        measures: Optional[List[Dict[str, str]]] = None,
        filters: Optional[Dict[str, List[Any]]] = None,
        max_rows: int = 10000,
        table_name: Optional[str] = None,
        **_kwargs: Any,
    ) -> pd.DataFrame:
        """
        Run a hypercube query against a Qlik Sense app and return a DataFrame.

        Args:
            app: "Space/AppName" or raw app ID. Alias: ``table_name``.
            dimensions: Qlik field names to group by (e.g. ["Region", "Year"]).
            measures: list of ``{"expr": "...", "alias": "..."}`` dicts;
                ``expr`` uses Qlik expression syntax (Sum, Count, Avg, ...).
            filters: ``{field: [allowed_values]}`` applied as Qlik selections
                before the cube is built.
            max_rows: maximum rows to return (paginated in 10k-cell chunks).
        """
        target = app or table_name
        if not target:
            raise ValueError("app (or table_name) is required")
        dimensions = dimensions or []
        measures = measures or []
        if not dimensions and not measures:
            raise ValueError("At least one dimension or measure is required")

        app_id = self._resolve_app_id(target)

        hypercube_def = _build_hypercube_def(dimensions, measures, max_rows)
        selections = list((filters or {}).items())

        # Selections are applied inside _execute_hypercube() before CreateSessionObject
        # so the hypercube reflects the narrowed associative state.
        return asyncio.run(self._execute_hypercube(app_id, selections, hypercube_def, max_rows))

    async def _execute_hypercube(
        self,
        app_id: str,
        selections: List,
        hypercube_def: Dict[str, Any],
        max_rows: int,
    ) -> pd.DataFrame:
        import ssl
        import websockets

        url = self._ws_url_for_app(app_id)
        ssl_ctx: Any
        if url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if not self.verify_ssl:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        else:
            ssl_ctx = None
        headers = [("Authorization", f"Bearer {self.api_key}")]

        async with websockets.connect(
            url,
            extra_headers=headers,
            ssl=ssl_ctx,
            open_timeout=self.timeout_sec,
            close_timeout=5,
        ) as ws:

            async def rpc(handle: int, method: str, params) -> Dict[str, Any]:
                payload = {
                    "jsonrpc": "2.0",
                    "id": rpc.counter,
                    "handle": handle,
                    "method": method,
                    "params": params,
                }
                rpc.counter += 1
                await ws.send(json.dumps(payload))
                raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout_sec)
                msg = json.loads(raw)
                if "error" in msg:
                    err = msg["error"] or {}
                    raise RuntimeError(f"Qlik QIX error on {method}: {err.get('message') or err}")
                return msg.get("result") or {}

            rpc.counter = 1
            opendoc = await rpc(-1, "OpenDoc", [app_id])
            doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
            if doc_handle is None:
                raise RuntimeError("Qlik OpenDoc did not return a document handle")

            # Apply selections as SelectInField(field, [values], toggle=False)
            for field, values in selections:
                await rpc(doc_handle, "SelectInField", [
                    field,
                    {"qMatchMode": 0, "qSoftLock": False, "qValues": [
                        {"qText": str(v)} for v in (values or [])
                    ]},
                    False,
                ])

            session = await rpc(doc_handle, "CreateSessionObject", [{
                "qInfo": {"qType": "bow-hypercube"},
                "qHyperCubeDef": hypercube_def,
            }])
            obj_handle = (session.get("qReturn") or {}).get("qHandle")
            if obj_handle is None:
                raise RuntimeError("Qlik CreateSessionObject did not return a handle")

            width = len(hypercube_def.get("qDimensions") or []) + len(
                hypercube_def.get("qMeasures") or []
            )
            rows: List[List[Any]] = []
            top = 0
            page_height = max(1, min(max_rows, 10000 // max(1, width)))
            while len(rows) < max_rows:
                height = min(page_height, max_rows - len(rows))
                page = await rpc(obj_handle, "GetHyperCubeData", [
                    "/qHyperCubeDef",
                    [{"qTop": top, "qLeft": 0, "qWidth": width, "qHeight": height}],
                ])
                data_pages = page.get("qDataPages") or []
                if not data_pages:
                    break
                matrix = data_pages[0].get("qMatrix") or []
                if not matrix:
                    break
                for r in matrix:
                    rows.append(r)
                if len(matrix) < height:
                    break
                top += len(matrix)

        return _hypercube_matrix_to_df(hypercube_def, rows)

    def _resolve_app_id(self, target: str) -> str:
        """
        Accept a raw app ID or a 'Space/AppName' / 'AppName' string.

        Always attempts a lookup against list_apps() first so that plain names
        like "Ops" or "Forecast" (no "/" and no spaces) resolve correctly. Falls
        back to returning `target` verbatim when list_apps() is unavailable
        (e.g., offline) or when nothing matches — callers can still pass an
        opaque ID that doesn't appear in the items listing.
        """
        if not target:
            raise ValueError("app is required")
        try:
            apps = self.list_apps()
        except Exception as e:
            logger.debug("list_apps() failed during resolve_app_id(%r): %s", target, e)
            return target
        for app in apps:
            full = (
                f"{app.get('space_name') or ''}/{app.get('name')}"
                if app.get("space_name")
                else (app.get("name") or "")
            )
            if full == target or app.get("name") == target or app.get("id") == target:
                return app.get("id") or target
        return target

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    def prompt_schema(self) -> str:
        schemas = self.get_schemas()
        return ServiceFormatter(schemas).table_str

    @property
    def description(self) -> str:
        return (
            "Qlik Sense Client: discover Qlik apps (REST) and run hypercube queries "
            "against them via the Engine API (QIX) over WebSocket. Works against Qlik Cloud."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Qlik Sense Hypercube Query Guide

Run hypercube queries against published Qlik Sense apps. Each app is a self-contained
associative model; cubes combine dimensions (Qlik fields) and measures (Qlik expressions).

### Schema Structure

Each Qlik table is exposed as a schema table named `Space/AppName/TableName`
(or `AppName/TableName` when no space). The `metadata.qlik_sense` block on each
table carries `appId`, `appName`, `spaceId`, `spaceName`, `tableName` — you must
pass the full `Space/App/Table` name (or the `appId`) to `execute_query()` via
the `app` argument.

### How to Execute Queries

Signature: `execute_query(app, dimensions, measures, filters=None, max_rows=10000)`

```python
# Total sales by region
df = client.execute_query(
    app="Sales/Pipeline 2025",
    dimensions=["Region"],
    measures=[{"expr": "Sum([Sales])", "alias": "Total Sales"}],
)

# Filtered top-N
df = client.execute_query(
    app="Sales/Pipeline 2025",
    dimensions=["Product"],
    measures=[{"expr": "Sum([Revenue])", "alias": "Rev"}],
    filters={"Region": ["EMEA"], "Year": [2025]},
    max_rows=10,
)

# Row count / profiling
df = client.execute_query(
    app="Sales/Pipeline 2025",
    measures=[{"expr": "Count([OrderID])", "alias": "Rows"}],
)
```

### Key Query Rules

- **Field names are case-sensitive** and must match exactly what the schema lists.
- **Measures are Qlik expressions**, not SQL. Use `Sum(...)`, `Count(distinct ...)`,
  `Avg(...)`. Wrap names containing spaces in square brackets: `Sum([Net Sales])`.
- **Filters apply as Qlik selections** — they narrow the associative state and
  propagate across every related table automatically (this is different from a
  SQL `WHERE` and different from a DAX filter).
- Qlik is associative, not strictly relational: key fields shared across tables
  link them automatically. Use a single cube that names fields from multiple
  tables rather than writing joins.
- Result is a flat DataFrame of (dimensions..., measures...). Rows are capped at
  `max_rows`; paging is handled internally.
"""

    # ------------------------------------------------------------------
    # Compatibility
    # ------------------------------------------------------------------


# Compatibility alias for dynamic resolver expecting 'QlikSenseClient' via different case
QliksenseClient = QlikSenseClient


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

_TAG_DTYPE_ORDER = [
    ("$key", "key"),
    ("$date", "date"),
    ("$timestamp", "timestamp"),
    ("$numeric", "numeric"),
    ("$integer", "integer"),
    ("$text", "text"),
    ("$ascii", "text"),
]


def _tags_to_dtype(tags: Optional[List[str]]) -> str:
    tags = tags or []
    for tag, dtype in _TAG_DTYPE_ORDER:
        if tag in tags:
            return dtype
    return "unknown"


def _build_hypercube_def(
    dimensions: List[str],
    measures: List[Dict[str, str]],
    max_rows: int,
) -> Dict[str, Any]:
    qdims = [{
        "qDef": {"qFieldDefs": [d]},
        "qNullSuppression": False,
    } for d in dimensions]
    qmeas = [{
        "qDef": {
            "qDef": (m.get("expr") if isinstance(m, dict) else str(m)) or "",
            "qLabel": (m.get("alias") if isinstance(m, dict) else "") or "",
        }
    } for m in measures]
    width = max(1, len(qdims) + len(qmeas))
    return {
        "qDimensions": qdims,
        "qMeasures": qmeas,
        "qInitialDataFetch": [{
            "qTop": 0,
            "qLeft": 0,
            "qHeight": min(max_rows, max(1, 10000 // width)),
            "qWidth": width,
        }],
        "qSuppressZero": False,
        "qSuppressMissing": False,
    }


def _hypercube_matrix_to_df(
    hypercube_def: Dict[str, Any],
    rows: List[List[Dict[str, Any]]],
) -> pd.DataFrame:
    dim_defs = hypercube_def.get("qDimensions") or []
    meas_defs = hypercube_def.get("qMeasures") or []

    col_names: List[str] = []
    col_is_measure: List[bool] = []
    for d in dim_defs:
        field_defs = (d.get("qDef") or {}).get("qFieldDefs") or []
        col_names.append(field_defs[0] if field_defs else "dim")
        col_is_measure.append(False)
    for m in meas_defs:
        q_def = (m.get("qDef") or {})
        label = q_def.get("qLabel") or q_def.get("qDef") or "measure"
        col_names.append(label)
        col_is_measure.append(True)

    # De-dupe column names (append .1, .2 ... for collisions)
    seen: Dict[str, int] = {}
    unique_names: List[str] = []
    for name in col_names:
        if name in seen:
            seen[name] += 1
            unique_names.append(f"{name}.{seen[name]}")
        else:
            seen[name] = 0
            unique_names.append(name)

    parsed_rows: List[List[Any]] = []
    for row in rows:
        parsed: List[Any] = []
        for idx, cell in enumerate(row):
            if idx >= len(col_is_measure):
                parsed.append(cell.get("qText") if isinstance(cell, dict) else cell)
                continue
            if isinstance(cell, dict):
                qnum = cell.get("qNum")
                qtext = cell.get("qText")
                if col_is_measure[idx]:
                    if isinstance(qnum, (int, float)):
                        parsed.append(qnum)
                    else:
                        parsed.append(qtext)
                else:
                    parsed.append(qtext if qtext not in (None, "") else qnum)
            else:
                parsed.append(cell)
        parsed_rows.append(parsed)

    if not parsed_rows:
        return pd.DataFrame(columns=unique_names)
    df = pd.DataFrame(parsed_rows, columns=unique_names)
    return df
