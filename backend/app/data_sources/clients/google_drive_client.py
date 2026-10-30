"""Google Drive (+ Sheets) file-source client.

Two ways to authenticate:

- Per-user OAuth (delegated). The admin configures an OAuth client
  (client_id / client_secret); each user grants access via the
  authorization-code flow, and their access token is what this client uses.
- Service account (shared). The admin pastes a service-account JSON key.
  Everyone on the connection sees the files shared with the service
  account's email (and the Shared Drives it is a member of). No domain-wide
  delegation: the service account never acts as a user.
"""
from __future__ import annotations
from app.data_sources.clients.progress import discovery_progress, discovery_items

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
from app.data_sources.clients._file_source_common import (
    EXCEL_EXTS,
    DocumentText,
    pick_sheet,
    read_excel_sheet,
    tag_sheets,
    FileTooLargeError,
    NamedBytes,
)
from app.data_sources.clients.base import Capability, DataSourceClient


DRIVE_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
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
        # Shared mode: a service-account JSON key (content, not a path).
        service_account_json: Optional[str] = None,
        **_ignored,
    ):
        super().__init__()
        self.access_token = access_token
        self._sa_credentials = None
        # A user token always wins: a per-user connection never carries a
        # service-account key, and this keeps the two modes from mixing.
        if service_account_json and not access_token:
            self._sa_credentials = self._load_service_account(service_account_json)
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

    @staticmethod
    def _load_service_account(raw: Any):
        from google.oauth2 import service_account

        from app.core.egress import check_google_token_uri

        info = raw
        if isinstance(raw, str):
            try:
                info = json.loads(raw)
            except ValueError:
                raise ValueError("Service account JSON is not valid JSON.")
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise ValueError("Service account JSON must be a Google service-account key (type: service_account).")
        # token_uri is where google-auth POSTs the signed assertion.
        check_google_token_uri(info)
        return service_account.Credentials.from_service_account_info(info, scopes=[DRIVE_READONLY_SCOPE])

    @property
    def uses_service_account(self) -> bool:
        return self._sa_credentials is not None

    @property
    def _has_auth(self) -> bool:
        return bool(self.access_token) or self.uses_service_account

    def _bearer(self) -> str:
        if self.access_token:
            return self.access_token
        if self._sa_credentials is not None:
            if not self._sa_credentials.valid:
                from google.auth.transport.requests import Request
                self._sa_credentials.refresh(Request())
            return self._sa_credentials.token
        raise ValueError(
            "Google Drive client has no access token. Users must connect via OAuth "
            "before the connection can be used."
        )

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer()}", "Accept": "application/json"}

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
        elif self.uses_service_account:
            # A service account's files live in sharedWithMe and Shared
            # Drives; the default "user" corpus misses the Shared Drives.
            params["corpora"] = "allDrives"
        return params

    def _list_in_folder(self, folder_id: str) -> List[dict]:
        return self._list_query(f"'{folder_id}' in parents and trashed=false")

    def _list_query(self, q: str) -> List[dict]:
        out: List[dict] = []
        page_token: Optional[str] = None
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

    def _list_shared_drives(self) -> List[dict]:
        out: List[dict] = []
        page_token: Optional[str] = None
        while True:
            params = {"pageSize": "100", "fields": "nextPageToken,drives(id,name)"}
            if page_token:
                params["pageToken"] = page_token
            data = self._get(f"{DRIVE_BASE}/drives", params=params)
            out.extend(data.get("drives", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out

    def _service_account_roots(self) -> List[dict]:
        """Top-level entries a service account can see when no folder is
        configured. Its own "My Drive" is almost always empty: what admins
        give it is folders/files shared with its email (sharedWithMe) and
        Shared Drives it was added to. Shared Drives come back as folders so
        `_walk_entries` descends into them."""
        entries = self._list_query("sharedWithMe = true and trashed = false")
        entries += self._list_query("'root' in parents and trashed = false")
        for drive in self._list_shared_drives():
            entries.append({"id": drive["id"], "name": drive.get("name") or drive["id"], "mimeType": GOOGLE_FOLDER_MIME})
        return entries

    def _walk(self, folder_id: str, prefix: str = "") -> List[dict]:
        return self._walk_entries(self._list_in_folder(folder_id), prefix)

    def _walk_entries(self, entries: List[dict], prefix: str = "", _seen: Optional[set] = None) -> List[dict]:
        # A file shared directly AND inside a shared folder is listed twice;
        # keep the first.
        seen = _seen if _seen is not None else set()
        results: List[dict] = []
        for entry in discovery_items(entries, 'listing_files', label=lambda entry: entry.get('name')):
            if entry.get("id") in seen:
                continue
            seen.add(entry.get("id"))
            mime = entry.get("mimeType", "")
            name = entry.get("name", "")
            path = f"{prefix}/{name}" if prefix else name
            if mime == GOOGLE_FOLDER_MIME:
                if self.recursive:
                    results.extend(self._walk_entries(self._list_in_folder(entry["id"]), path, seen))
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
        if not self._has_auth:
            return []
        target = folder_id or self._root_folder_id()
        prev = self.recursive
        if recursive is not None:
            self.recursive = bool(recursive)
        try:
            if target == "root" and self.uses_service_account:
                return self._walk_entries(self._service_account_roots())
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
                # The tab list comes from the spreadsheet's metadata, so the
                # model sees every sheet name even on a single-sheet read.
                titles = [
                    str((t.get("properties") or {}).get("title") or "")
                    for t in self._get(
                        f"{SHEETS_BASE}/spreadsheets/{file_id}",
                        params={"fields": "sheets.properties.title"},
                    ).get("sheets", [])
                ]
                target = pick_sheet(titles, sheet)
                values = self._get(
                    f"{SHEETS_BASE}/spreadsheets/{file_id}/values/{quote(target)}",
                    params={"valueRenderOption": "UNFORMATTED_VALUE"},
                ).get("values", [])
                if not values:
                    return tag_sheets(pd.DataFrame(), titles, target)
                header = values[0]
                rows = [r + [None] * (len(header) - len(r)) for r in values[1:]]
                return tag_sheets(pd.DataFrame(rows, columns=header), titles, target)
            content = self._get_bytes(
                f"{DRIVE_BASE}/files/{file_id}/export",
                params={"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            )
            return read_excel_sheet(content)

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
            if doc_text_is_usable(text, ext):
                # Carry the bytes we already downloaded — see DocumentText.
                return DocumentText(text, raw=content, name=name)
            return NamedBytes(content, name=name)

        from app.data_sources.clients.graph_drive_client import _trim_to_data
        if ext == "csv":
            return _trim_to_data(pd.read_csv(io.BytesIO(content), header=None))
        if ext == "tsv":
            return _trim_to_data(pd.read_csv(io.BytesIO(content), sep="\t", header=None))
        if ext in EXCEL_EXTS:
            return read_excel_sheet(content, sheet)
        if ext == "json":
            try:
                return json.loads(content.decode("utf-8", errors="replace"))
            except Exception:
                return content.decode("utf-8", errors="replace")
        if ext in TEXT_EXTS:
            return content.decode("utf-8", errors="replace")
        return NamedBytes(content, name=name)

    def read_raw_bytes(self, file_id: str, *, max_bytes: Optional[int] = None):
        """Raw file bytes + name + mime, unparsed — for attach_file (persist
        the ORIGINAL file) and the read_file tool's PDF→images vision fallback.
        Google-native files (Docs/Sheets/Slides) are exported to PDF since they
        have no binary original to download. `max_bytes` rejects an oversize
        binary from its reported size before downloading it (native files
        report none; their exports are small)."""
        file_id = self._resolve_file_id(file_id)
        meta = self._get(
            f"{DRIVE_BASE}/files/{file_id}",
            params={"fields": "id,name,mimeType,size", "supportsAllDrives": "true"},
        )
        mime = meta.get("mimeType", "")
        name = meta.get("name", "")
        size = meta.get("size")
        if max_bytes and size is not None and int(size) > max_bytes:
            raise FileTooLargeError(
                f"'{name}' is {int(size) / 1024 / 1024:.1f} MB, over the {max_bytes / 1024 / 1024:.0f} MB limit."
            )
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
        """Verify what we can.

        Per-user OAuth: without a user token there's nothing meaningful to
        call, so the admin save just acknowledges the OAuth client. Service
        account: get a token and call Drive as the service account.
        """
        if self.uses_service_account:
            try:
                about = self._get(f"{DRIVE_BASE}/about", params={"fields": "user(emailAddress)"})
                email = (about.get("user") or {}).get("emailAddress") or ""
                return {"success": True, "message": f"Connected as {email}".strip()}
            except Exception as e:
                return {"success": False, "message": str(e)}
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

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> List[Table]:
        files = self.list_files()
        tables: List[Table] = []
        for f in discovery_items(files, 'files', label=lambda f: f.get('name')):
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
