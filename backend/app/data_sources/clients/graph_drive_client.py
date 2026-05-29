"""Microsoft Graph file-source client.

Backs both the `sharepoint` and `onedrive` data source types. Uses Microsoft
Graph delegated permissions (per-user OAuth) by default; service-principal
auth is supported for SharePoint enumeration but Graph requires delegated
auth for actual file reads on most tenants.

For v1 the client surfaces files as `Table` rows via `get_schemas()` so the
existing agent path works (catalog visibility + read on demand). Capabilities
LIST_FILES + READ_FILE are also declared so a future agent-tool layer can
call the corresponding methods directly.
"""
from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients.base import Capability, DataSourceClient


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_BASE = "https://login.microsoftonline.com"


# Extensions we try to read as DataFrames; everything else is returned as
# bytes/text. Order matters only for human-readable docs.
TABULAR_EXTS = {"csv", "tsv", "xlsx", "xls"}
TEXT_EXTS = {"txt", "md", "json", "html", "htm", "log", "yaml", "yml"}


def _ext(name: str) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


class GraphDriveClient(DataSourceClient):
    """Microsoft Graph file source. Used by SharePoint and OneDrive registry entries."""

    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}

    @property
    def description(self) -> str:
        if self.mode == "sharepoint":
            return (
                f"SharePoint site {self.site_url}"
                + (f" / library {self.drive_name}" if self.drive_name else "")
                + (f" / folder {self.folder_path}" if self.folder_path else "")
            )
        return "OneDrive" + (f" / folder {self.folder_path}" if self.folder_path else "")

    @property
    def is_document_based(self) -> bool:
        return True

    def __init__(
        self,
        # Auth
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        # OAuth-app overrides (admin separates app-only from user-OAuth app)
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        # SharePoint config
        site_url: Optional[str] = None,
        drive_name: Optional[str] = None,
        # Shared file-scoping config
        folder_path: Optional[str] = None,
        allowed_extensions: Optional[str] = None,
        recursive: bool = False,
        # Mode discriminator (set by the registry-specific subclass)
        mode: str = "sharepoint",
        **_ignored,
    ):
        super().__init__()
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.oauth_client_id = oauth_client_id
        self.oauth_client_secret = oauth_client_secret

        self.site_url = (site_url or "").strip()
        self.drive_name = (drive_name or "").strip() or None
        self.folder_path = (folder_path or "").strip().strip("/") or None
        self.allowed_extensions = self._parse_exts(allowed_extensions)
        self.recursive = bool(recursive)
        self.mode = mode

        # Snapshot whether a user OAuth token was provided up front. `_token()`
        # may later populate `self.access_token` with an app-only token in
        # service-principal mode, which would otherwise be indistinguishable
        # from a delegated user token — and `/me/*` endpoints reject app-only
        # tokens with HTTP 400.
        self._user_token_provided = bool(access_token)

        # Cached IDs (resolved lazily)
        self._site_id: Optional[str] = None
        self._drive_id: Optional[str] = None
        self._root_item_id: Optional[str] = None  # scoped root (after folder_path)

    # ------------------------------------------------------------------ auth

    def _token(self) -> str:
        """Return a valid bearer token. Prefers delegated; falls back to client-credentials."""
        if self.access_token:
            return self.access_token
        if not (self.tenant_id and self.client_id and self.client_secret):
            raise ValueError("No access_token and no service-principal credentials configured")
        resp = httpx.post(
            f"{TOKEN_BASE}/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise ValueError(f"Microsoft token endpoint error: {resp.status_code} {resp.text}")
        self.access_token = resp.json()["access_token"]
        return self.access_token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}

    # ----------------------------------------------------------------- utils

    @staticmethod
    def _parse_exts(value: Optional[str]) -> Optional[set]:
        if not value:
            return None
        items = {e.strip().lower().lstrip(".") for e in value.split(",") if e.strip()}
        return items or None

    def _allowed(self, name: str) -> bool:
        if not self.allowed_extensions:
            return True
        return _ext(name) in self.allowed_extensions

    def _get(self, path: str, **kwargs) -> dict:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        resp = httpx.get(url, headers=self._headers(), timeout=30, **kwargs)
        if resp.status_code == 401:
            # token expired mid-call; refresh once
            self.access_token = None if not (self.tenant_id and self.client_id and self.client_secret) else None
            resp = httpx.get(url, headers=self._headers(), timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise ValueError(f"Graph {url} → {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def _get_bytes(self, path: str) -> bytes:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        with httpx.Client(follow_redirects=True, timeout=60) as c:
            resp = c.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise ValueError(f"Graph {url} → {resp.status_code} {resp.text[:300]}")
        return resp.content

    # ------------------------------------------------ site / drive resolution

    def _resolve_site_id(self) -> str:
        if self._site_id:
            return self._site_id
        if not self.site_url:
            raise ValueError("site_url is required for SharePoint connections")
        # Parse e.g. https://contoso.sharepoint.com/sites/Finance
        from urllib.parse import urlparse
        u = urlparse(self.site_url)
        host = u.netloc
        path = u.path.rstrip("/")
        # Graph format: /sites/{hostname}:{server-relative-path}
        data = self._get(f"/sites/{host}:{path}")
        self._site_id = data["id"]
        return self._site_id

    def _resolve_drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id

        if self.mode == "onedrive":
            data = self._get("/me/drive")
            self._drive_id = data["id"]
            return self._drive_id

        site_id = self._resolve_site_id()
        if not self.drive_name:
            data = self._get(f"/sites/{site_id}/drive")
            self._drive_id = data["id"]
            return self._drive_id

        data = self._get(f"/sites/{site_id}/drives")
        for d in data.get("value", []):
            if d.get("name") == self.drive_name:
                self._drive_id = d["id"]
                return self._drive_id
        raise ValueError(f"Document library '{self.drive_name}' not found on site")

    def _resolve_root_item_id(self) -> str:
        if self._root_item_id:
            return self._root_item_id
        drive_id = self._resolve_drive_id()
        if not self.folder_path:
            data = self._get(f"/drives/{drive_id}/root")
        else:
            encoded = quote(self.folder_path)
            data = self._get(f"/drives/{drive_id}/root:/{encoded}")
        self._root_item_id = data["id"]
        return self._root_item_id

    # ----------------------------------------------------------- enumeration

    def _list_children(self, drive_id: str, item_id: str) -> List[dict]:
        out: List[dict] = []
        url: Optional[str] = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children?$top=200"
        while url:
            data = self._get(url)
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return out

    def _walk(self, drive_id: str, item_id: str, prefix: str = "") -> List[dict]:
        results: List[dict] = []
        for entry in self._list_children(drive_id, item_id):
            name = entry.get("name", "")
            path = f"{prefix}/{name}" if prefix else name
            is_folder = "folder" in entry
            if is_folder:
                if self.recursive:
                    results.extend(self._walk(drive_id, entry["id"], path))
                continue
            if not self._allowed(name):
                continue
            results.append({
                "id": entry["id"],
                "name": name,
                "path": path,
                "mime_type": (entry.get("file") or {}).get("mimeType"),
                "size": entry.get("size"),
                "modified_at": entry.get("lastModifiedDateTime"),
                "is_folder": False,
                "web_url": entry.get("webUrl"),
                "drive_id": drive_id,
            })
        return results

    # ---------------------------------------------------- public capabilities

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None) -> List[dict]:
        # OneDrive enumeration goes through /me/drive, which only works with a
        # delegated user token. Without one (e.g. admin-save indexing before
        # any user has signed in), return an empty catalog rather than 400.
        # The real enumeration runs per-user once a user completes OAuth.
        if self.mode == "onedrive" and not self._user_token_provided:
            return []
        drive_id = self._resolve_drive_id()
        item_id = folder_id or self._resolve_root_item_id()
        prev = self.recursive
        if recursive is not None:
            self.recursive = bool(recursive)
        try:
            return self._walk(drive_id, item_id)
        finally:
            self.recursive = prev

    def read_file(self, file_id: str, sheet: Optional[str] = None, max_bytes: Optional[int] = None, **_) -> Any:
        drive_id = self._resolve_drive_id()
        meta = self._get(f"/drives/{drive_id}/items/{file_id}")
        name = meta.get("name", "")
        ext = _ext(name)
        content = self._get_bytes(f"/drives/{drive_id}/items/{file_id}/content")
        if max_bytes and len(content) > max_bytes:
            content = content[:max_bytes]

        if ext == "csv":
            return pd.read_csv(io.BytesIO(content))
        if ext == "tsv":
            return pd.read_csv(io.BytesIO(content), sep="\t")
        if ext in ("xlsx", "xls"):
            return pd.read_excel(io.BytesIO(content), sheet_name=sheet or 0)
        if ext == "json":
            try:
                return json.loads(content.decode("utf-8", errors="replace"))
            except Exception:
                return content.decode("utf-8", errors="replace")
        if ext in TEXT_EXTS:
            return content.decode("utf-8", errors="replace")
        return content

    def search_files(self, query: str, **_) -> List[dict]:
        drive_id = self._resolve_drive_id()
        encoded = quote(query)
        data = self._get(f"/drives/{drive_id}/root/search(q='{encoded}')")
        results = []
        for entry in data.get("value", []):
            if "folder" in entry:
                continue
            if not self._allowed(entry.get("name", "")):
                continue
            results.append({
                "id": entry["id"],
                "name": entry.get("name"),
                "path": (entry.get("parentReference") or {}).get("path"),
                "mime_type": (entry.get("file") or {}).get("mimeType"),
                "size": entry.get("size"),
                "modified_at": entry.get("lastModifiedDateTime"),
                "web_url": entry.get("webUrl"),
                "drive_id": drive_id,
            })
        return results

    # ---------------------------------------- DataSourceClient compatibility

    def test_connection(self) -> dict:
        """Verify the connection is configured well enough to be usable.

        Two modes:
        - Delegated (a user access_token is present): fully resolve the drive
          and touch the configured root — proves end-to-end access.
        - Admin-only (service-principal credentials, no user token yet): just
          verify the token can be acquired. Drive/root access needs a user
          token, which arrives after a user completes OAuth — testing it now
          would fail with `/me request is only valid with delegated
          authentication flow` for OneDrive, or with insufficient privileges
          for SharePoint depending on app permissions. For SharePoint we also
          resolve the site URL since `/sites/{id}` works app-only and proves
          the configured URL is reachable.
        """
        try:
            # Acquire a token either way; this validates the credentials.
            # In service-principal mode this populates `self.access_token`
            # with an app-only token, so we use `_user_token_provided`
            # (captured at __init__) to distinguish.
            self._token()

            if self._user_token_provided:
                # We have a user token — exercise the real read path.
                self._resolve_drive_id()
                self._resolve_root_item_id()
                return {"success": True, "message": "Connected"}

            if self.mode == "sharepoint":
                # App-only token can resolve the site (proves URL + perms).
                self._resolve_site_id()
                return {
                    "success": True,
                    "message": (
                        "Service principal verified and site is reachable. "
                        "Have a user sign in to access files."
                    ),
                }

            # OneDrive admin-only: token-only check, since /me/drive needs
            # delegated auth.
            return {
                "success": True,
                "message": (
                    "Service principal credentials verified. Have a user sign "
                    "in with Microsoft to access their OneDrive."
                ),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_schemas(self) -> List[Table]:
        files = self.list_files()
        tables: List[Table] = []
        for f in files:
            tables.append(Table(
                name=f["path"] or f["name"],
                description=(
                    f"File '{f['name']}' (type: {f.get('mime_type') or _ext(f['name']) or 'unknown'})."
                ),
                columns=[],
                pks=[],
                fks=[],
                metadata_json={
                    "graph": {
                        "file_id": f["id"],
                        "drive_id": f["drive_id"],
                        "mime_type": f.get("mime_type"),
                        "size": f.get("size"),
                        "modified_at": f.get("modified_at"),
                        "web_url": f.get("web_url"),
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
        tables = self.get_schemas()
        if not tables:
            return "No files available in the configured scope."
        lines = [f"Available files ({len(tables)}):"]
        for t in tables:
            meta = (t.metadata_json or {}).get("graph", {})
            lines.append(f"- {t.name} ({meta.get('mime_type') or 'file'})")
        return "\n".join(lines)

    def execute_query(self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs):
        """For document-based use: `query` may be a file_id or a JSON spec.

        Supported shapes:
        - table_name=<path-or-name> → read that file
        - query=<file_id> → read by file ID
        - query={"file_id": "...", "sheet": "..."} (JSON string) → read with options
        """
        if isinstance(query, str) and query.strip().startswith("{"):
            try:
                spec = json.loads(query)
                fid = spec.get("file_id")
                if fid:
                    return self.read_file(fid, sheet=spec.get("sheet"), max_bytes=spec.get("max_bytes"))
            except json.JSONDecodeError:
                pass

        if table_name:
            for t in self.get_schemas():
                if t.name == table_name:
                    fid = (t.metadata_json or {}).get("graph", {}).get("file_id")
                    if fid:
                        return self.read_file(fid, sheet=kwargs.get("sheet"))
            raise ValueError(f"File not found in scope: {table_name}")

        if query:
            return self.read_file(query, sheet=kwargs.get("sheet"))

        raise ValueError("Provide table_name or query (file_id) to read a file")


class SharepointClient(GraphDriveClient):
    """SharePoint data source — alias with mode preset."""

    def __init__(self, **kwargs):
        kwargs["mode"] = "sharepoint"
        super().__init__(**kwargs)


class OnedriveClient(GraphDriveClient):
    """OneDrive data source — alias with mode preset."""

    def __init__(self, **kwargs):
        kwargs["mode"] = "onedrive"
        super().__init__(**kwargs)
