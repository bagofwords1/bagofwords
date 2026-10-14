"""Read-only SharePoint Server connector (NTLM or Kerberos).

No Graph/Entra dependency. Paths are decoded server-relative ResourcePaths;
credentials never follow redirects. Scope is enforced again on every read.
"""
from __future__ import annotations
from app.data_sources.clients.progress import discovery_progress, discovery_items

import io
import json
import mimetypes
import threading
from urllib.parse import quote, unquote, urlsplit

import pandas as pd
import requests
from requests_ntlm import HttpNtlmAuth

from app.ai.prompt_formatters import Table
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients._sharepoint_listing import SharepointBoundedListing
from app.data_sources.clients._document_text import (
    DOC_EXTS, doc_text_is_usable, extract_document_text_from_bytes,
)
from app.data_sources.clients._file_source_common import (
    DocumentText, FileTooLargeError, GlobScopeError, NamedBytes, globs_from_str, path_matches_globs,
)
from app.data_sources.clients.graph_drive_client import (
    TEXT_EXTS, _ext, _extract_pdf_pages_from_bytes, _trim_to_data,
)


class SharePointHTTPError(ValueError):
    def __init__(self, status):
        self.status = status
        # Never include provider response bodies (may contain credentials/HTML).
        hint = {
            401: "Authentication rejected. Check the selected auth method and account.",
            403: "This account does not have permission to read the requested resource.",
            404: "Site, document library, folder, or file was not found.",
            429: "SharePoint throttled the request. Retry later or narrow the scope.",
        }.get(status, "SharePoint request failed.")
        super().__init__(f"SharePoint Server HTTP {status}: {hint}")


def _safe_path(value: str) -> str:
    """Reject traversal, including encoded variants; preserve literal %/# names."""
    probe = value
    for _ in range(4):
        if "\\" in probe or any(ord(c) < 32 for c in probe):
            raise GlobScopeError("Invalid SharePoint path.")
        if any(p in (".", "..") for p in probe.split("/")) or probe.startswith("//"):
            raise GlobScopeError("Path is outside the connection scope.")
        decoded = unquote(probe)
        if decoded == probe:
            break
        probe = decoded
    return value


def _literal(value: str) -> str:
    return "'" + quote(value.replace("'", "''"), safe="/") + "'"


class SharepointOnpremClient(SharepointBoundedListing, DataSourceClient):
    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}
    # Always list with the caller's identity: a shared metadata catalog must
    # not leak the service account's inventory to a user-required connection.
    cheap_live_listing = True
    is_document_based = True

    def __init__(self, site_url: str, username: str | None = None,
                 password: str | None = None, kerberos: bool = False,
                 principal: str | None = None, drive_name: str = "*",
                 folder_path: str | None = None, include_globs: str | None = None,
                 allowed_extensions: str | None = None, recursive: bool = False,
                 index_mode: str = "metadata", max_catalog_objects: int = 5000,
                 allow_http: bool = False, max_file_size_mb: int = 50,
                 api_mode: str = "modern",
                 **kwargs):
        if api_mode not in ("modern", "2010"):
            raise ValueError("API mode must be modern or 2010.")
        if api_mode == "2010" and not (drive_name or "").strip():
            raise ValueError("SharePoint 2010 requires a document library name or * for all libraries.")
        self.api_mode = api_mode
        self.site_url = site_url.rstrip("/")
        parsed = urlsplit(self.site_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Site URL must be an HTTP(S) site URL without credentials, query, or fragment.")
        if parsed.scheme == "http" and not allow_http:
            raise ValueError("HTTPS is required. Enable Allow HTTP only for an isolated test lab.")
        _safe_path(unquote(parsed.path))
        self._origin = (parsed.scheme.lower(), parsed.netloc.lower())
        self._origin_url = f"{parsed.scheme}://{parsed.netloc}"
        self._site_path = unquote(parsed.path).rstrip("/")
        self.drive_name = (drive_name or "").strip()
        self.folder_path = "/".join(part for part in _safe_path(folder_path or "").split("/") if part)
        self.include_globs = globs_from_str(include_globs)
        self.allowed_extensions = {e.strip().lower().lstrip(".") for e in (allowed_extensions or "").split(",") if e.strip()}
        self.recursive = recursive
        if index_mode not in ("none", "metadata"):
            raise ValueError("Indexing must be none or metadata.")
        self.index_mode = index_mode
        if not 1 <= int(max_catalog_objects) <= 50000 or not 1 <= int(max_file_size_mb) <= 250:
            raise ValueError("Max files must be 1–50000; max file size must be 1–250 MB.")
        self.max_catalog_objects = int(max_catalog_objects)
        self._max_bytes = int(max_file_size_mb) * 1024 * 1024
        self._username, self._password = username, password
        self.kerberos, self.principal = kerberos, principal
        if not kerberos and (not username or not password):
            raise ValueError("NTLM requires a domain username and password.")
        self._local = threading.local()
        from app.data_sources.clients._sharepoint_2010 import Sharepoint2010API
        from app.data_sources.clients._sharepoint_rest import SharepointRestAPI
        adapter_class = Sharepoint2010API if api_mode == "2010" else SharepointRestAPI
        self._adapter = adapter_class(self)

    @property
    def description(self):
        return self._adapter.description

    def _session(self):
        # requests.Session is not thread-safe. Each worker gets its own auth
        # context and connection pool; never mutate process Kerberos variables.
        if self.kerberos and hasattr(self._local, "session") and self._local.creds.lifetime < 120:
            self._local.session.close()
            del self._local.session
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.trust_env = False  # no ambient proxy / .netrc credentials
            if self.kerberos:
                try:
                    import gssapi
                    from requests_gssapi import HTTPSPNEGOAuth, REQUIRED
                except ImportError as exc:
                    raise ValueError("Install the kerberos extra and mount krb5.conf plus a client keytab.") from exc
                import os
                keytab = os.environ.get("KRB5_CLIENT_KTNAME")
                name = gssapi.Name(self.principal, gssapi.NameType.kerberos_principal) if self.principal else None
                creds = gssapi.Credentials(usage="initiate", name=name,
                                          **({"store": {"client_keytab": keytab}} if keytab else {}))
                self._local.creds = creds
                session.auth = HTTPSPNEGOAuth(creds=creds, mutual_authentication=REQUIRED,
                                             mech=gssapi.MechType.kerberos,
                                             opportunistic_auth=True)
            else:
                session.auth = HttpNtlmAuth(self._username, self._password)
            # Internal CA support is deployment-owned, not an arbitrary file
            # path submitted through the connection form.
            import os
            session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
            session.headers["Accept"] = "application/json;odata=nometadata"
            self._local.session = session
        return self._local.session

    def _request_bytes(self, method, url, *, byte_limit, file_content=False, accepted_statuses=(200,), **kwargs):
        """Bounded read-only HTTP shared by REST and SOAP, with one fresh-session retry.

        Adapters validate their endpoints before reaching this transport. A SOAP
        HTTP 500 body is parsed by the adapter to distinguish a fault from HTML.
        """
        for attempt in range(2):
            try:
                send = getattr(self._session(), method)
                with send(url, timeout=(10, 45), allow_redirects=False, stream=True, **kwargs) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise ValueError("SharePoint redirected the request. Use the site's configured public URL (alternate access mapping).")
                    if status not in accepted_statuses:
                        raise SharePointHTTPError(status)
                    def too_large():
                        if file_content:
                            raise FileTooLargeError("File exceeds the configured byte limit; increase it explicitly to read this file.")
                        raise ValueError("SharePoint metadata response exceeds the 8 MiB limit; narrow the library or folder scope.")
                    if int(response.headers.get("Content-Length", "0")) > byte_limit:
                        too_large()
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > byte_limit:
                            too_large()
                        chunks.append(chunk)
                    return status, b"".join(chunks)
            except requests.exceptions.SSLError as exc:
                raise ValueError("SharePoint TLS verification failed. Install the issuing CA in the backend trust store.") from exc
            except requests.RequestException as exc:
                if hasattr(self._local, "session"):
                    self._local.session.close()
                    del self._local.session
                if attempt:
                    raise ValueError("Cannot reach SharePoint. Check DNS, network access, and server availability.") from exc

    def _scope(self, path, libraries, *, folder=False):
        _safe_path(path)
        for name, root in libraries:
            if path.casefold().startswith(root.casefold() + "/") or (folder and path.casefold() == root.casefold()):
                rel = path[len(root):].lstrip("/")
                display = f"{name}/{rel}" if self.drive_name == "*" else rel
                if not folder and (
                    not path_matches_globs(display, self.include_globs)
                    or (self.allowed_extensions and _ext(path) not in self.allowed_extensions)
                ):
                    raise GlobScopeError("File does not match the connection's include patterns or extensions.")
                return display
        raise GlobScopeError("Path is outside the configured document library/folder.")

    def _entry(self, meta, libraries):
        path = meta["ServerRelativeUrl"]
        return {"id": path, "name": meta["Name"], "path": self._scope(path, libraries),
                "size": int(meta.get("Length") or 0), "modified_at": meta.get("TimeLastModified"),
                "mime_type": mimetypes.guess_type(meta["Name"])[0], "is_folder": False,
                "web_url": self._origin_url + quote(path, safe="/")}

    def _iter_listing_files(self, folder_id, recursive, page_size):
        return self._adapter.iter_files(folder_id, recursive, page_size=page_size)

    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None):
        return self._adapter.list_files(folder_id, recursive, limit, progress_callback)


    def read_raw_bytes(self, file_id, *, max_bytes=None):
        return self._adapter.read_raw_bytes(file_id, max_bytes=max_bytes)

    def read_file(self, file_id, sheet=None, max_bytes=None, page_range=None, **_):
        content, name, mime = self.read_raw_bytes(file_id, max_bytes=max_bytes)
        ext = _ext(name)
        if page_range is not None:
            if ext != "pdf" or page_range[0] < 1 or page_range[1] < page_range[0]:
                raise ValueError("page_range requires a PDF and a positive, ordered page interval.")
            text, total = _extract_pdf_pages_from_bytes(content, name, *page_range)
            return {"__doc_pages__": True, "text": text, "pages_total": total,
                    "first": page_range[0], "last": min(page_range[1], total), "raw": content, "name": name}
        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(content, name)
            if doc_text_is_usable(text, ext):
                return DocumentText(text, raw=content, name=name, mime=mime)
            return NamedBytes(content, name=name, mime=mime)
        if ext in ("csv", "tsv"):
            return _trim_to_data(pd.read_csv(io.BytesIO(content), sep="\t" if ext == "tsv" else ",", header=None))
        if ext in ("xls", "xlsx"):
            return _trim_to_data(pd.read_excel(io.BytesIO(content), sheet_name=sheet if sheet is not None else 0, header=None))
        if ext == "json":
            return json.loads(content.decode("utf-8-sig"))
        if ext in TEXT_EXTS:
            return content.decode("utf-8-sig", errors="replace")
        return NamedBytes(content, name=name, mime=mime)

    def search_files(self, query, limit=100, **kwargs):
        return self._adapter.search_files(query, limit=limit, **kwargs)

    def test_connection(self):
        return self._adapter.test_connection()

    @discovery_progress
    def get_schemas(self, progress_callback=None):
        if self.index_mode == "none":
            return []
        return [Table(name=f["path"], description=f"SharePoint Server file ({_ext(f['name'])})",
                      columns=[], pks=[], fks=[], metadata_json={"sharepoint_onprem": {
                          "file_id": f["id"], "mime_type": f["mime_type"], "size": f["size"],
                          "modified_at": f["modified_at"], "web_url": f["web_url"],
                      }}) for f in discovery_items(self.list_files(progress_callback=progress_callback), "files", label=lambda f: f["path"])]

    def get_schema(self, table_name):
        return next((t for t in self.get_schemas() if t.name == table_name), None)

    def prompt_schema(self):
        return "\n".join(t.name for t in self.get_schemas())

    def execute_query(self, query=None, table_name=None, **kwargs):
        if query and str(query).lstrip().startswith("{"):
            params = json.loads(query)
            return self.read_file(**params)
        return self.read_file(file_id=table_name or query or kwargs.pop("file_id", ""), **kwargs)
