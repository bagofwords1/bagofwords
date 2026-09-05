"""Read-only SharePoint Server REST connector (NTLM or Kerberos).

No Graph/Entra dependency. Paths are decoded server-relative ResourcePaths;
credentials never follow redirects. Scope is enforced again on every read.
"""
from __future__ import annotations

import io
import json
import mimetypes
import threading
from collections import deque
from urllib.parse import quote, unquote, urlsplit

import pandas as pd
import requests
from requests_ntlm import HttpNtlmAuth

from app.ai.prompt_formatters import Table
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients._document_text import (
    DOC_EXTS, doc_text_is_usable, extract_document_text_from_bytes,
)
from app.data_sources.clients._file_source_common import (
    DocumentText, GlobScopeError, NamedBytes, globs_from_str, path_matches_globs,
)
from app.data_sources.clients.graph_drive_client import (
    TEXT_EXTS, _ext, _extract_pdf_pages_from_bytes, _trim_to_data,
)
from app.data_sources.clients.progress import make_reporter


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


class SharepointOnpremClient(DataSourceClient):
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
                 **kwargs):
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
        self._api = self.site_url + "/_api/"
        self.drive_name = drive_name or ""
        self.folder_path = _safe_path((folder_path or "").strip("/"))
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

    @property
    def description(self):
        return (
            "Read-only SharePoint Server document libraries. Use list_files, search_files, "
            "and read_file with the returned server-relative file id or scoped path. "
            "Search uses the SharePoint content index and live filename matches; newly "
            "uploaded document contents require a SharePoint search crawl. CSV/Excel "
            "reads return DataFrames. PDF/DOCX/PPTX reads return document text."
        )

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

    def _request(self, path, *, params=None, binary=False, byte_limit=None):
        # A stale IIS keep-alive connection can fail midway through a streamed
        # GET. Retry once on a fresh authenticated session; never return partial
        # bytes or retry a TLS trust failure. This connector makes no writes.
        for attempt in range(2):
            try:
                return self._request_once(path, params=params, binary=binary, byte_limit=byte_limit)
            except requests.exceptions.SSLError as exc:
                raise ValueError("SharePoint TLS verification failed. Install the issuing CA in the backend trust store.") from exc
            except requests.RequestException as exc:
                if hasattr(self._local, "session"):
                    self._local.session.close()
                    del self._local.session
                if attempt:
                    raise ValueError("Cannot reach SharePoint. Check DNS, network access, and server availability.") from exc

    def _request_once(self, path, *, params=None, binary=False, byte_limit=None):
        url = path if path.startswith(("http://", "https://")) else self._api + path
        p = urlsplit(url)
        if ((p.scheme.lower(), p.netloc.lower()) != self._origin
                or not unquote(p.path).startswith(self._site_path + "/_api/")
                or p.username or p.password or p.fragment):
            raise ValueError("Refusing a SharePoint API link outside the configured site.")
        _safe_path(unquote(p.path))
        try:
            with self._session().get(url, params=params, timeout=(10, 45),
                                     allow_redirects=False, stream=True) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("SharePoint redirected the request. Use the site's configured public URL (alternate access mapping).")
                if response.status_code != 200:
                    raise SharePointHTTPError(response.status_code)
                limit = byte_limit if binary else 8 * 1024 * 1024
                limit = min(limit or self._max_bytes, self._max_bytes) if binary else limit
                if int(response.headers.get("Content-Length", "0")) > limit:
                    raise ValueError("SharePoint response exceeds the configured byte limit.")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("SharePoint response exceeds the configured byte limit.")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if binary:
                    return content
                data = json.loads(content)
                return data.get("d", data)  # verbose and JSON-light OData
        except requests.RequestException:
            raise

    def _pages(self, path, params=None):
        visited = set()
        for _ in range(1000):
            if path in visited:
                raise ValueError("SharePoint returned a pagination cycle.")
            visited.add(path)
            data = self._request(path, params=params)
            for row in data.get("value", data.get("results", [])):
                yield row
            path = data.get("odata.nextLink") or data.get("@odata.nextLink") or data.get("__next")
            params = None
            if not path:
                return
        raise ValueError("SharePoint pagination limit exceeded; narrow the connection scope.")

    def _libraries(self):
        libs = list(self._pages("web/lists", {
            "$filter": "BaseTemplate eq 101 and Hidden eq false",
            "$select": "Id,Title,RootFolder/ServerRelativeUrl", "$expand": "RootFolder", "$top": 200,
        }))
        if self.drive_name == "*":
            chosen = libs
        elif self.drive_name:
            chosen = [lib for lib in libs if lib["Title"].casefold() == self.drive_name.casefold()]
        else:
            default = self._request("web/DefaultDocumentLibrary", params={"$select": "Id"})
            chosen = [lib for lib in libs if lib["Id"] == default["Id"]]
        if not chosen:
            raise ValueError("No matching document library is accessible on this site.")
        result = []
        for lib in chosen:
            root = _safe_path(lib["RootFolder"]["ServerRelativeUrl"].rstrip("/"))
            if not root.casefold().startswith((self._site_path + "/").casefold()):
                raise GlobScopeError("Library is outside the configured site.")
            result.append((lib["Title"], root + ("/" + self.folder_path if self.folder_path else "")))
        return result

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

    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None):
        cap = min(max(0, int(limit)) if limit is not None else self.max_catalog_objects, self.max_catalog_objects)
        if cap == 0:
            return []
        libraries = self._libraries()
        roots = [folder_id] if folder_id else [root for _, root in libraries]
        if folder_id:
            self._scope(folder_id, libraries, folder=True)
        queue, seen, files = deque(roots), set(), []
        recurse = self.recursive if recursive is None else recursive
        reporter = make_reporter(progress_callback) if progress_callback else None
        if reporter:
            reporter.phase("listing SharePoint libraries", total=len(roots))
        while queue:
            root = queue.popleft()
            self._scope(root, libraries, folder=True)
            if root.casefold() in seen:
                continue
            seen.add(root.casefold())
            if len(seen) > self.max_catalog_objects:
                raise ValueError("Folder traversal limit exceeded; narrow the folder scope.")
            endpoint = f"web/GetFolderByServerRelativePath(decodedurl={_literal(root)})"
            try:
                for meta in self._pages(endpoint + "/Files", {"$select": "Name,ServerRelativeUrl,Length,TimeLastModified", "$top": min(cap, 200)}):
                    try:
                        files.append(self._entry(meta, libraries))
                    except GlobScopeError:
                        continue
                    if len(files) >= cap:
                        return files
                if recurse:
                    for child in self._pages(endpoint + "/Folders", {"$select": "Name,ServerRelativeUrl", "$top": 200}):
                        if child["Name"] != "Forms":
                            queue.append(child["ServerRelativeUrl"])
            except SharePointHTTPError as exc:
                # Missing scoped folder across * libraries is normal; access
                # failures are NOT silently turned into an empty catalog.
                if exc.status == 404 and root in roots and self.drive_name == "*" and self.folder_path:
                    continue
                raise
        return files

    def _resolve(self, file_id, libraries):
        value = str(file_id)
        if value.startswith("/"):
            self._scope(value, libraries)
            return value
        _safe_path(value)
        candidates = []
        qualified = [(name, root) for name, root in libraries
                     if self.drive_name == "*" and value.startswith(name + "/")]
        scope_error = None
        for name, root in qualified or libraries:
            rel = value
            if self.drive_name == "*" and value.startswith(name + "/"):
                rel = value[len(name) + 1:]
            candidate = root + "/" + rel
            try:
                self._scope(candidate, libraries)
                meta = self._request(f"web/GetFileByServerRelativePath(decodedurl={_literal(candidate)})", params={"$select": "ServerRelativeUrl"})
                self._scope(meta["ServerRelativeUrl"], libraries)
                candidates.append(meta["ServerRelativeUrl"])
            except GlobScopeError as exc:
                scope_error = exc
            except SharePointHTTPError as exc:
                if exc.status != 404:
                    raise
        if not candidates and scope_error:
            raise scope_error
        if len(candidates) != 1:
            raise ValueError("File path is missing or ambiguous. Use the id returned by list_files/search_files.")
        return candidates[0]

    def read_raw_bytes(self, file_id, *, max_bytes=None):
        libraries = self._libraries()
        path = self._resolve(file_id, libraries)
        endpoint = f"web/GetFileByServerRelativePath(decodedurl={_literal(path)})"
        meta = self._request(endpoint, params={"$select": "Name,ServerRelativeUrl,Length,TimeLastModified"})
        self._scope(meta["ServerRelativeUrl"], libraries)
        limit = min(self._max_bytes, max_bytes) if max_bytes is not None else self._max_bytes
        if limit < 1 or int(meta.get("Length") or 0) > limit:
            raise ValueError("File exceeds the configured byte limit; increase it explicitly to read this file.")
        content = self._request(endpoint + "/$value", binary=True, byte_limit=limit)
        return content, meta["Name"], mimetypes.guess_type(meta["Name"])[0]

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

    def search_files(self, query, limit=100, **_):
        if not str(query).strip():
            return []
        cap = min(max(0, int(limit)), self.max_catalog_objects, 500)
        if not cap:
            return []
        libraries = self._libraries()
        # Quoted literal query: do not let model-generated KQL escape scope.
        term = str(query).replace('"', ' ').replace("\\", " ").strip()
        kql = f'"{term}" AND IsDocument:True AND Path:"{self.site_url}/"'
        data = self._request("search/query", params={
            "querytext": "'" + kql.replace("'", "''") + "'",
            "rowlimit": cap, "trimduplicates": "false", "selectproperties": "'Path,Title'",
        })
        table = data.get("PrimaryQueryResult", {}).get("RelevantResults", {}).get("Table", {})
        rows = table.get("Rows", [])
        rows = rows.get("results", []) if isinstance(rows, dict) else rows
        found = {}
        for row in rows:
            cells = row.get("Cells", [])
            cells = cells.get("results", []) if isinstance(cells, dict) else cells
            values = {cell["Key"]: cell.get("Value") for cell in cells}
            url = urlsplit(values.get("Path") or "")
            if (url.scheme.lower(), url.netloc.lower()) != self._origin:
                continue
            path = unquote(url.path)
            try:
                self._scope(path, libraries)
                meta = self._request(f"web/GetFileByServerRelativePath(decodedurl={_literal(path)})", params={"$select": "Name,ServerRelativeUrl,Length,TimeLastModified"})
                entry = self._entry(meta, libraries)
                found[entry["id"]] = entry
            except GlobScopeError:
                continue
            except SharePointHTTPError as exc:
                if exc.status not in (403, 404):
                    raise
        # Indexing is asynchronous on Server: supplement indexed content hits
        # with live filename matches (never pretend to search uncrawled content).
        for entry in self.list_files():
            if str(query).casefold() in entry["path"].casefold():
                found[entry["id"]] = entry
            if len(found) >= cap:
                break
        return list(found.values())[:cap]

    def test_connection(self):
        try:
            web = self._request("web", params={"$select": "Title,Url"})
            user = self._request("web/currentuser", params={"$select": "LoginName"})
            self.list_files(limit=1, recursive=False)
            return {"success": True, "message": "Connected to SharePoint Server.",
                    "details": {"site": web.get("Title"), "identity": user.get("LoginName"),
                                "auth_method": "kerberos" if self.kerberos else "ntlm"}}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def get_schemas(self, progress_callback=None):
        if self.index_mode == "none":
            return []
        return [Table(name=f["path"], description=f"SharePoint Server file ({_ext(f['name'])})",
                      columns=[], pks=[], fks=[], metadata_json={"sharepoint_onprem": {
                          "file_id": f["id"], "mime_type": f["mime_type"], "size": f["size"],
                          "modified_at": f["modified_at"], "web_url": f["web_url"],
                      }}) for f in self.list_files(progress_callback=progress_callback)]

    def get_schema(self, table_name):
        return next((t for t in self.get_schemas() if t.name == table_name), None)

    def prompt_schema(self):
        return "\n".join(t.name for t in self.get_schemas())

    def execute_query(self, query=None, table_name=None, **kwargs):
        if query and str(query).lstrip().startswith("{"):
            params = json.loads(query)
            return self.read_file(**params)
        return self.read_file(file_id=table_name or query or kwargs.pop("file_id", ""), **kwargs)
