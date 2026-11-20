"""
Qlik Sense Cloud client.

Discovers Qlik apps (models) via the Qlik Cloud REST API and runs hypercube
queries against them via the Qlik Engine API (QIX), a JSON-RPC 2.0 protocol
spoken over WebSocket.

Authentication:
  - ``api_key`` — long-lived bearer token generated in the tenant UI
    (Settings > API keys). Fastest to set up; single rotatable secret.
  - ``oauth_m2m`` — OAuth 2.0 Client Credentials. The tenant issues
    short-lived access tokens in exchange for ``client_id`` + ``client_secret``.
    Tokens are cached in-process and refreshed before expiry. Preferred for
    production deployments with secret-rotation policies.

Discovery:
  - Qlik Cloud (tenant.REGION.qlikcloud.com)
  - REST authentication/app listing: GET /api/v1/users/me, /api/v1/items?resourceType=app,
    /api/v1/apps/{appId}/data/metadata
  - Shared QIX catalog: tables, master items, sheets, variables, lineage
  - QIX query: OpenDoc, GetTablesAndKeys, GetFieldList,
    CreateSessionObject(qHyperCubeDef), GetHyperCubeData

Out of scope for v1: on-prem Enterprise on Windows, OAuth2 Authorization Code
(interactive user login), JWT.
"""

from __future__ import annotations
from app.data_sources.clients.progress import discovery_progress, discovery_items, IndexingCancelled

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import pandas as pd
import requests
from app.core.egress import pinned_request, pinned_session

from app.ai.prompt_formatters import ServiceFormatter, Table
from app.data_sources.clients._qix_common import (
    _TAG_DTYPE_ORDER as _TAG_DTYPE_ORDER_SHARED,
    QixSession,
    apply_field_selections,
    build_hypercube_def,
    build_ssl_context,
    build_tables_from_qix,
    build_tables_from_rest_metadata,
    fetch_hypercube_pages,
    hypercube_matrix_to_df,
    tags_to_dtype,
)
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients._qix_catalog import QlikEngineCatalog, _compact


logger = logging.getLogger(__name__)


class QlikSenseClient(QlikEngineCatalog, DataSourceClient):
    """Qlik Sense Cloud client — REST discovery + QIX query over WebSocket."""
    _metadata_namespace = "qlik_sense"

    # OAuth tokens are refreshed this many seconds before their advertised expiry.
    _TOKEN_REFRESH_SKEW_SEC = 60

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: Optional[str] = "user_default",
        verify_ssl: bool = True,
        timeout_sec: int = 30,
        space_filter: Optional[str] = None,
        max_concurrency: int = 4,
        app_filter: Optional[str] = None,
        max_apps: Optional[int] = None,
        include_master_items: bool = True,
        include_lineage: bool = True,
        include_sheet_objects: bool = True,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or None
        self.client_id = client_id or None
        self.client_secret = client_secret or None
        self.scope = scope or "user_default"
        self.verify_ssl = verify_ssl
        self.timeout_sec = timeout_sec
        self.max_concurrency = max(1, min(4, int(max_concurrency or 4)))
        self.host = urlparse(self.base_url).netloc
        self.include_master_items = include_master_items
        self.include_lineage = include_lineage
        self.include_sheet_objects = include_sheet_objects
        self.max_apps = max(1, int(max_apps)) if max_apps else None
        self._app_filter = {v.strip().casefold() for v in (app_filter or "").split(",") if v.strip()} or None
        # Parse the space filter into a set of tokens (IDs or names)
        self._space_filter: Optional[set] = None
        if space_filter:
            tokens = {t.strip() for t in str(space_filter).split(",") if t.strip()}
            self._space_filter = tokens or None

        self._http: Optional[requests.Session] = None
        # `Space/App` keys of the apps the last get_schemas could not read.
        self._unread_app_keys: List[str] = []

        # OAuth M2M token cache
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    @property
    def _auth_mode(self) -> str:
        """Which credential was supplied? 'api_key', 'oauth_m2m', or raises."""
        if self.api_key:
            return "api_key"
        if self.client_id and self.client_secret:
            return "oauth_m2m"
        raise RuntimeError(
            "Qlik Sense requires either api_key or (client_id + client_secret)"
        )

    def connect(self) -> None:
        """Set up the REST HTTP session. QIX connections are opened per call."""
        if self._http is not None:
            return
        if not self.base_url:
            raise RuntimeError("base_url is required")
        # Validate we have *some* credential path (raises on misconfiguration).
        _ = self._auth_mode
        self._http = pinned_session()

    def _fetch_oauth_token(self) -> None:
        """Exchange client_id/client_secret for a bearer token via /oauth/token."""
        url = f"{self.base_url}/oauth/token"
        body = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
        }
        # Use a one-shot request here (not self._http) so token exchange doesn't
        # try to call _bearer_token() recursively.
        resp = pinned_request(
            "POST",
            url,
            data=body,
            headers={"Accept": "application/json"},
            timeout=self.timeout_sec,
            verify=self.verify_ssl,
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"Qlik Cloud OAuth token exchange failed: POST {url} "
                f"HTTP {resp.status_code} {resp.text[:500]}"
            )
        try:
            data = resp.json() or {}
        except ValueError as e:
            raise RuntimeError(f"Qlik Cloud OAuth returned non-JSON: {e}")
        token = data.get("access_token")
        if not token:
            raise RuntimeError("Qlik Cloud OAuth response missing access_token")
        expires_in = int(data.get("expires_in") or 3600)
        self._access_token = token
        self._token_expires_at = time.time() + expires_in

    def _bearer_token(self) -> str:
        """Return the current bearer token, refreshing OAuth tokens as needed."""
        mode = self._auth_mode
        if mode == "api_key":
            return self.api_key or ""
        # oauth_m2m — refresh if close to expiry
        now = time.time()
        if not self._access_token or now >= (self._token_expires_at - self._TOKEN_REFRESH_SKEW_SEC):
            self._fetch_oauth_token()
        return self._access_token or ""

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer_token()}",
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
        apps = self._scoped_apps()
        return apps[:self.max_apps] if self.max_apps else apps

    def _scoped_apps(self) -> List[Dict[str, Any]]:
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
                app_id = item.get("resourceId") or item.get("id")
                if self._app_filter and not {str(app_id or "").casefold(), str(item.get("name") or "").casefold()} & self._app_filter:
                    continue
                apps.append({
                    "id": item.get("resourceId") or item.get("id"),
                    "item_id": item.get("id"),
                    "name": item.get("name") or item.get("resourceId"),
                    "space_id": item.get("spaceId"),
                    "space_name": item.get("spaceName"),
                    "updated_at": item.get("updatedAt"),
                    "owner": item.get("ownerId"),
                    "description": item.get("description"),
                    # A shared-space catalog must not include token-owner private
                    # sheets, even if Items omits the app's publish metadata.
                    "published": bool(item.get("spaceId") or (item.get("resourceAttributes") or {}).get("publishTime")),
                    "last_reload": (item.get("resourceAttributes") or {}).get("lastReloadTime"),
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

    def _acting_user(self, app=None):
        # Cloud always uses the bearer token's identity; no impersonation header.
        return None

    def _app_meta(self, app, payload=None):
        payload = payload or {}
        return _compact({
            "appId": app.get("id"), "appName": app.get("name"),
            "spaceId": app.get("space_id"), "spaceName": app.get("space_name"),
            "appDescription": app.get("description"), "owner": app.get("owner"),
            "modifiedDate": app.get("updated_at"), "lastReload": app.get("last_reload"),
            "webUrl": f"{self.base_url}/sense/app/{app.get('id')}",
            "hasData": payload.get("has_data"), "hasScript": payload.get("has_script"),
            "alternateStates": payload.get("state_names"), "sectionAccess": payload.get("section_access"),
            "scriptChars": payload.get("script_chars"), "lineageSources": payload.get("lineage_sources"),
            "sheets": payload.get("sheet_names"),
        })

    def _engine_tables(self, app):
        payload = asyncio.run(self._read_app_async(app, None))
        tables = self._build_data_tables(app, payload.get("qtr") or [], payload.get("qk") or [], payload)
        if self.include_master_items:
            master = self._build_master_table(app, payload, {t.name for t in tables})
            if master is not None:
                tables.append(master)
        if self.include_sheet_objects:
            tables.extend(self._build_sheet_tables(app, payload, {t.name for t in tables}))
        if not tables:
            key = self._app_key(app)
            self._unread_app_keys.append(key)
            tables = [Table(name=key, columns=[], pks=[], fks=[], is_active=False,
                            metadata_json={"qlik_sense": {**self._app_meta(app, payload), "status": "empty"}})]
        return tables

    @staticmethod
    def _rest_metadata_has_content(payload: Optional[Dict[str, Any]]) -> bool:
        if not payload:
            return False
        return bool(payload.get("tables")) and bool(payload.get("fields"))

    # Catalog mapping is shared with the on-prem client — see `_qix_common`.
    _build_tables_from_rest_metadata = staticmethod(build_tables_from_rest_metadata)
    _build_tables_from_qix = staticmethod(build_tables_from_qix)

    def _crawl_app(self, app: Dict[str, Any]) -> List[Table]:
        """
        Per-app Engine discovery, with a validated REST model fallback.
        Errors are converted to a stub Table row so the overall crawl never aborts.
        """
        app_id = app.get("id")
        app_name = app.get("name") or app_id or ""
        space_name = app.get("space_name") or ""
        app_key = f"{space_name}/{app_name}" if space_name else app_name

        engine_error = None
        if self.include_master_items or self.include_lineage or self.include_sheet_objects:
            try:
                return self._engine_tables(app)
            except Exception as exc:
                engine_error = exc

        rest_error: Optional[Exception] = None
        rest_meta: Optional[Dict[str, Any]] = None
        try:
            rest_meta = self._rest_get(f"/api/v1/apps/{app_id}/data/metadata")
        except Exception as e:
            rest_error = e
            logger.debug("REST metadata for app %s failed: %s", app_id, e)

        if self._rest_metadata_has_content(rest_meta):
            try:
                tables = self._build_tables_from_rest_metadata(app, rest_meta)
                expected = {t.get("name"): t for t in rest_meta["tables"] if not t.get("is_system")}
                if any(
                    len(t.columns) != expected[t.metadata_json["qlik_sense"]["tableName"]].get("no_of_fields", len(t.columns))
                    or (not t.columns and expected[t.metadata_json["qlik_sense"]["tableName"]].get("no_of_fields", 1) != 0)
                    for t in tables
                ):
                    raise ValueError("REST metadata did not map fields to business tables")
                if not tables and expected:
                    raise ValueError("REST metadata did not map business tables")
                if engine_error is not None:
                    # Keep previously indexed semantic entries when only the
                    # REST model snapshot is readable this time.
                    self._unread_app_keys.append(app_key)
                    for table in tables:
                        table.metadata_json["qlik_sense"]["discoveryWarning"] = "Engine discovery unavailable; REST model only"
                return tables
            except Exception as e:
                rest_error = e

        qix_error: Optional[Exception] = None
        qtr: List[Dict[str, Any]] = []
        qk: List[Dict[str, Any]] = []
        try:
            if engine_error is not None:
                raise engine_error
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
            if app_key:
                self._unread_app_keys.append(app_key)
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

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> List[Table]:
        """
        Scoped crawl with shared Engine discovery and a validated REST fallback.
        """
        self._unread_app_keys = []
        apps = self.list_apps()
        if not apps:
            return []

        tables: List[Table] = []
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            try:
                futures = {pool.submit(self._crawl_app, app): app for app in apps}
                for fut in discovery_items(as_completed(futures), 'applications', label=lambda fut: futures[fut].get('name') or futures[fut].get('id'), total=len(futures)):
                    try:
                        tables.extend(fut.result() or [])
                    except IndexingCancelled:
                        raise
                    except Exception as e:
                        app = futures[fut]
                        logger.warning("Unhandled crawl exception for %s: %s", app.get("id"), e)
                        name = app.get("name") or app.get("id") or ""
                        space = app.get("space_name") or ""
                        self._unread_app_keys.append(f"{space}/{name}" if space else name)
            except IndexingCancelled:
                for pending in futures:
                    pending.cancel()
                raise
        if self._unread_app_keys and (not tables or all((t.metadata_json or {}).get("qlik_sense", {}).get("status") == "error" for t in tables)):
            raise RuntimeError("Qlik discovery failed for every visible app; check access and Engine connectivity")
        return tables

    def unindexed_table_prefixes(self) -> List[str]:
        return [k for k in self._unread_app_keys if k]

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

        Returns (qtr, qk) lists from the result. Failures propagate so unreadable apps cannot masquerade as empty models.
        """
        return asyncio.run(self._qix_get_tables_and_keys_async(app_id))

    def _qix_session(self, app_id: str, user=None) -> QixSession:
        url = self._ws_url_for_app(app_id)
        ssl_ctx = build_ssl_context(verify_ssl=self.verify_ssl) if url.startswith("wss://") else None
        return QixSession(
            url,
            headers=[("Authorization", f"Bearer {self._bearer_token()}")],
            ssl_context=ssl_ctx,
            timeout_sec=self.timeout_sec,
        )

    async def _qix_get_tables_and_keys_async(self, app_id: str):
        async with self._qix_session(app_id) as qix:
            opendoc = await qix.rpc(-1, "OpenDoc", [app_id])
            doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
            if doc_handle is None:
                raise RuntimeError("OpenDoc returned no document handle")
            tk = await qix.rpc(doc_handle, "GetTablesAndKeys", [
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
        object_id: Optional[str] = None,
        search: Optional[str] = None,
        fields: Optional[List[str]] = None,
        variables: Optional[Dict[str, Any]] = None,
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
                before the cube is built. Values must be non-empty lists of
                strings or finite numbers.
            max_rows: maximum rows to return (paginated in 10k-cell chunks).
        """
        if object_id is not None or search is not None or variables is not None:
            return self._execute_engine_query(
                app=app, dimensions=dimensions, measures=measures, filters=filters,
                max_rows=max_rows, table_name=table_name, object_id=object_id,
                search=search, fields=fields, variables=variables,
            )
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
        async with self._qix_session(app_id) as qix:
            opendoc = await qix.rpc(-1, "OpenDoc", [app_id])
            doc_handle = (opendoc.get("qReturn") or {}).get("qHandle")
            if doc_handle is None:
                raise RuntimeError("Qlik OpenDoc did not return a document handle")

            await apply_field_selections(qix, doc_handle, selections)

            session = await qix.rpc(doc_handle, "CreateSessionObject", [{
                "qInfo": {"qType": "bow-hypercube"},
                "qHyperCubeDef": hypercube_def,
            }])
            obj_handle = (session.get("qReturn") or {}).get("qHandle")
            if obj_handle is None:
                raise RuntimeError("Qlik CreateSessionObject did not return a handle")

            width = len(hypercube_def.get("qDimensions") or []) + len(
                hypercube_def.get("qMeasures") or []
            )
            rows = await fetch_hypercube_pages(qix, obj_handle, width, max_rows)

        return hypercube_matrix_to_df(hypercube_def, rows)

    def _resolve_app(self, target):
        if not target:
            raise ValueError("app is required")
        apps = self._scoped_apps()
        matches = [a for a in apps if target in (a.get("id"), a.get("name"), self._app_key(a))]
        if not matches:
            matches = [a for a in apps if target.startswith(self._app_key(a) + "/")]
            if matches:
                longest = max(len(self._app_key(a)) for a in matches)
                matches = [a for a in matches if len(self._app_key(a)) == longest]
        if not matches and not self._app_filter and not self._space_filter:
            return target, None
        if len(matches) != 1:
            raise ValueError("Qlik app is ambiguous or outside the configured space/app scope")
        return matches[0]["id"], matches[0]

    def _resolve_app_id(self, target: str) -> str:
        return self._resolve_app(target)[0]

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

    system_prompt = QlikEngineCatalog.system_prompt

    # ------------------------------------------------------------------
    # Compatibility
    # ------------------------------------------------------------------


# Compatibility alias for dynamic resolver expecting 'QlikSenseClient' via different case
QliksenseClient = QlikSenseClient


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------
#
# These now live in `_qix_common` (shared with the on-prem client). The
# underscore-prefixed names are kept as aliases: they are the public surface
# this module has always exposed to its tests and to any caller that imported
# them directly.

_TAG_DTYPE_ORDER = _TAG_DTYPE_ORDER_SHARED
_tags_to_dtype = tags_to_dtype
_build_hypercube_def = build_hypercube_def
_hypercube_matrix_to_df = hypercube_matrix_to_df
