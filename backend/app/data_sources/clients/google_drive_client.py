"""Google Drive (+ Sheets) file-source client.

Per-user OAuth (delegated). The admin configures an OAuth client (client_id /
client_secret) at connection setup; each user grants access via the
authorization-code flow, and the resulting access token is what this client
uses to read files. No service-account / domain-wide delegation in v1.
"""
from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import pandas as pd

from app.ai.prompt_formatters import Table
from app.data_sources.clients._document_text import (
    DOC_EXTS,
    doc_text_is_usable,
    extract_document_text_from_bytes,
)
from app.data_sources.clients.base import Capability, DataSourceClient


DRIVE_BASE = "https://www.googleapis.com/drive/v3"
SHEETS_BASE = "https://sheets.googleapis.com/v4"

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

TEXT_EXTS = {"txt", "md", "json", "html", "htm", "log", "yaml", "yml", "csv", "tsv"}


def _ext(name: str) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


class GoogleDriveClient(DataSourceClient):
    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}

    @property
    def description(self) -> str:
        scope = self.folder_id or self.shared_drive_id or "My Drive"
        return f"Google Drive ({scope})"

    @property
    def is_document_based(self) -> bool:
        return True

    def __init__(
        self,
        # Per-user token (from OAuth flow — refreshed by the server before the
        # client is constructed, see `maybe_refresh_oauth_credentials`).
        access_token: Optional[str] = None,
        # Config
        folder_id: Optional[str] = None,
        shared_drive_id: Optional[str] = None,
        allowed_extensions: Optional[str] = None,
        # None ⇒ default ON. Personal Drive root almost always contains
        # folders rather than loose files; a non-recursive walk returns
        # empty for most users. Admins who scope to a specific folder can
        # explicitly pass False to opt out.
        recursive: Optional[bool] = None,
        workspace_domain: Optional[str] = None,
        **_ignored,
    ):
        super().__init__()
        self.access_token = access_token
        self.folder_id = (folder_id or "").strip() or None
        self.shared_drive_id = (shared_drive_id or "").strip() or None
        self.allowed_extensions = self._parse_exts(allowed_extensions)
        self.recursive = True if recursive is None else bool(recursive)
        self.workspace_domain = workspace_domain

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _parse_exts(value: Optional[str]) -> Optional[set]:
        if not value:
            return None
        items = {e.strip().lower().lstrip(".") for e in value.split(",") if e.strip()}
        return items or None

    def _allowed(self, name: str, mime_type: str) -> bool:
        if not self.allowed_extensions:
            return True
        # Map Google-native types to pseudo-extensions
        ext = _ext(name)
        if mime_type == GOOGLE_SHEET_MIME:
            ext = ext or "gsheet"
        elif mime_type == GOOGLE_DOC_MIME:
            ext = ext or "gdoc"
        return ext in self.allowed_extensions

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise ValueError(
                "Google Drive client has no access token. Users must connect via OAuth "
                "before the connection can be used."
            )
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def _get(self, url: str, params: Optional[dict] = None, **kwargs) -> dict:
        resp = httpx.get(url, headers=self._headers(), params=params, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise ValueError(f"Google {url} → {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def _get_bytes(self, url: str, params: Optional[dict] = None) -> bytes:
        with httpx.Client(follow_redirects=True, timeout=60) as c:
            resp = c.get(url, headers=self._headers(), params=params)
        if resp.status_code >= 400:
            raise ValueError(f"Google {url} → {resp.status_code} {resp.text[:300]}")
        return resp.content

    # ---------------------------------------------------------- enumeration

    def _drive_params(self) -> dict:
        params = {
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if self.shared_drive_id:
            params["corpora"] = "drive"
            params["driveId"] = self.shared_drive_id
        return params

    def _list_in_folder(self, folder_id: str) -> List[dict]:
        out: List[dict] = []
        page_token: Optional[str] = None
        q = f"'{folder_id}' in parents and trashed=false"
        while True:
            params = {
                **self._drive_params(),
                "q": q,
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size,webViewLink,parents)",
                "pageSize": "1000",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get(f"{DRIVE_BASE}/files", params=params)
            out.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out

    def _walk(self, folder_id: str, prefix: str = "") -> List[dict]:
        results: List[dict] = []
        for entry in self._list_in_folder(folder_id):
            mime = entry.get("mimeType", "")
            name = entry.get("name", "")
            path = f"{prefix}/{name}" if prefix else name
            if mime == GOOGLE_FOLDER_MIME:
                if self.recursive:
                    results.extend(self._walk(entry["id"], path))
                continue
            if not self._allowed(name, mime):
                continue
            results.append({
                "id": entry["id"],
                "name": name,
                "path": path,
                "mime_type": mime,
                "size": entry.get("size"),
                "modified_at": entry.get("modifiedTime"),
                "is_folder": False,
                "web_url": entry.get("webViewLink"),
            })
        return results

    def _root_folder_id(self) -> str:
        if self.folder_id:
            return self.folder_id
        if self.shared_drive_id:
            return self.shared_drive_id
        return "root"

    # ----------------------------------------------------------- public API

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None) -> List[dict]:
        # No user token yet (e.g. admin-save indexing before any user has
        # signed in) → no Drive access is possible. Return an empty catalog
        # rather than raising; per-user enumeration happens after OAuth.
        if not self.access_token:
            return []
        target = folder_id or self._root_folder_id()
        prev = self.recursive
        if recursive is not None:
            self.recursive = bool(recursive)
        try:
            return self._walk(target)
        finally:
            self.recursive = prev

    def _looks_like_filename(self, value: str) -> bool:
        if not value:
            return False
        return ("." in value) or ("/" in value) or (" " in value)

    def _resolve_file_id(self, file_id_or_name: str) -> str:
        """Accept either a Drive file id or a filename; return the id.

        The LLM often passes the readable name. Drive doesn't have a direct
        path lookup like Graph does, so we fall back to a name search and
        take the first non-trashed match.
        """
        if not self._looks_like_filename(file_id_or_name):
            return file_id_or_name
        try:
            safe = file_id_or_name.replace("'", "\\'")
            data = self._get(
                f"{DRIVE_BASE}/files",
                params={
                    **self._drive_params(),
                    "q": f"name = '{safe}' and trashed=false",
                    "fields": "files(id,name,mimeType)",
                    "pageSize": "10",
                },
            )
            for entry in (data.get("files") or []):
                if entry.get("mimeType") == GOOGLE_FOLDER_MIME:
                    continue
                return entry["id"]
        except Exception:
            pass
        return file_id_or_name

    def read_file(self, file_id: str, sheet: Optional[str] = None, max_bytes: Optional[int] = None, **_) -> Any:
        file_id = self._resolve_file_id(file_id)
        meta = self._get(
            f"{DRIVE_BASE}/files/{file_id}",
            params={"fields": "id,name,mimeType,size", "supportsAllDrives": "true"},
        )
        mime = meta.get("mimeType", "")
        name = meta.get("name", "")

        # Google-native: export
        if mime == GOOGLE_SHEET_MIME:
            # Use Sheets API for live values when a specific sheet is requested,
            # otherwise export the whole spreadsheet as xlsx.
            if sheet:
                values = self._get(
                    f"{SHEETS_BASE}/spreadsheets/{file_id}/values/{quote(sheet)}",
                    params={"valueRenderOption": "UNFORMATTED_VALUE"},
                ).get("values", [])
                if not values:
                    return pd.DataFrame()
                header = values[0]
                rows = [r + [None] * (len(header) - len(r)) for r in values[1:]]
                return pd.DataFrame(rows, columns=header)
            content = self._get_bytes(
                f"{DRIVE_BASE}/files/{file_id}/export",
                params={"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            )
            from app.data_sources.clients.graph_drive_client import _trim_to_data
            return _trim_to_data(pd.read_excel(io.BytesIO(content), header=None))

        if mime == GOOGLE_DOC_MIME:
            content = self._get_bytes(
                f"{DRIVE_BASE}/files/{file_id}/export",
                params={"mimeType": "text/plain"},
            )
            return content.decode("utf-8", errors="replace")

        # Binary download
        content = self._get_bytes(
            f"{DRIVE_BASE}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        if max_bytes and len(content) > max_bytes:
            content = content[:max_bytes]

        ext = _ext(name)
        # Rich documents (pdf/docx/pptx): return extracted plain text — same
        # contract as the network_dir / s3 / graph clients. Near-empty
        # extraction (scanned / image-based PDF) falls back to raw bytes so
        # the tool can render it for a vision model.
        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(content, name)
            return text if doc_text_is_usable(text) else content

        from app.data_sources.clients.graph_drive_client import _trim_to_data
        if ext == "csv":
            return _trim_to_data(pd.read_csv(io.BytesIO(content), header=None))
        if ext == "tsv":
            return _trim_to_data(pd.read_csv(io.BytesIO(content), sep="\t", header=None))
        if ext in ("xlsx", "xls"):
            return _trim_to_data(pd.read_excel(io.BytesIO(content), sheet_name=sheet or 0, header=None))
        if ext == "json":
            try:
                return json.loads(content.decode("utf-8", errors="replace"))
            except Exception:
                return content.decode("utf-8", errors="replace")
        if ext in TEXT_EXTS:
            return content.decode("utf-8", errors="replace")
        return content

    def read_raw_bytes(self, file_id: str):
        """Raw file bytes + name + mime, unparsed — for attach_file (persist
        the ORIGINAL file) and the read_file tool's PDF→images vision fallback.
        Google-native files (Docs/Sheets/Slides) are exported to PDF since they
        have no binary original to download."""
        file_id = self._resolve_file_id(file_id)
        meta = self._get(
            f"{DRIVE_BASE}/files/{file_id}",
            params={"fields": "id,name,mimeType", "supportsAllDrives": "true"},
        )
        mime = meta.get("mimeType", "")
        name = meta.get("name", "")
        if mime.startswith("application/vnd.google-apps"):
            content = self._get_bytes(
                f"{DRIVE_BASE}/files/{file_id}/export",
                params={"mimeType": "application/pdf"},
            )
            return content, f"{name}.pdf", "application/pdf"
        content = self._get_bytes(
            f"{DRIVE_BASE}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        return content, name, mime

    def search_files(self, query: str, **_) -> List[dict]:
        safe = query.replace("'", "\\'")
        # Search both filename AND file content (fullText index covers Docs,
        # Sheets, PDFs, Word, Excel, plain text). Mirrors Microsoft Graph's
        # /search(q=) semantics so the two backends behave consistently
        # from the agent's point of view.
        params = {
            **self._drive_params(),
            "q": f"(name contains '{safe}' or fullText contains '{safe}') and trashed=false",
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
            "pageSize": "100",
        }
        data = self._get(f"{DRIVE_BASE}/files", params=params)
        results = []
        for entry in data.get("files", []):
            mime = entry.get("mimeType", "")
            if mime == GOOGLE_FOLDER_MIME:
                continue
            if not self._allowed(entry.get("name", ""), mime):
                continue
            results.append({
                "id": entry["id"],
                "name": entry.get("name"),
                "mime_type": mime,
                "size": entry.get("size"),
                "modified_at": entry.get("modifiedTime"),
                "web_url": entry.get("webViewLink"),
            })
        return results

    # ----------------------------------------- DataSourceClient compatibility

    def test_connection(self) -> dict:
        """Verify what we can without a user token.

        Drive access is per-user; without a user OAuth token there's nothing
        meaningful to call. When admin saves the connection (admin app creds
        only), just acknowledge — the real test happens when a user signs in.
        """
        if not self.access_token:
            return {
                "success": True,
                "message": (
                    "OAuth client saved. Have a user sign in with Google to "
                    "access Drive files."
                ),
            }
        try:
            self._get(f"{DRIVE_BASE}/about", params={"fields": "user(emailAddress)"})
            self._list_in_folder(self._root_folder_id())
            return {"success": True, "message": "Connected"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_schemas(self) -> List[Table]:
        files = self.list_files()
        tables: List[Table] = []
        for f in files:
            tables.append(Table(
                name=f["path"] or f["name"],
                description=f"File '{f['name']}' ({f.get('mime_type') or 'unknown'}).",
                columns=[],
                pks=[],
                fks=[],
                metadata_json={
                    "google_drive": {
                        "file_id": f["id"],
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
            meta = (t.metadata_json or {}).get("google_drive", {})
            lines.append(f"- {t.name} ({meta.get('mime_type') or 'file'})")
        return "\n".join(lines)

    def execute_query(self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs):
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
                    fid = (t.metadata_json or {}).get("google_drive", {}).get("file_id")
                    if fid:
                        return self.read_file(fid, sheet=kwargs.get("sheet"))
            raise ValueError(f"File not found in scope: {table_name}")

        if query:
            return self.read_file(query, sheet=kwargs.get("sheet"))

        raise ValueError("Provide table_name or query (file_id) to read a file")
