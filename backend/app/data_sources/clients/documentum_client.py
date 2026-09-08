"""Read-only OpenText Documentum connector over Documentum REST Services.

Talks to ``https://host/dctm-rest`` only (hypermedia JSON,
``application/vnd.emc.documentum+json``). Documents are addressed by their
stable ``r_object_id``; display paths are relative to the configured root
folder. Scope (root folder + include globs) is re-enforced on every read, and
Documentum's own ACLs do the security trimming because every call runs as the
authenticated identity.

Auth (see docs/documentum-auth.md):
  * Basic — a repository user (``username`` / ``password``), system or per user.
  * OTDS client credentials — a confidential OAuth client (``otds_url``,
    ``client_id``, ``client_secret``) acting as its service user.
  * OTDS impersonation — the same client plus ``documentum_login``; an
    RFC 8693 token exchange yields a token *for that user*, so the session and
    the ACL evaluation are theirs. This is the per-user overlay variant.
  * Delegated OAuth — a per-user OTDS ``access_token`` obtained by BOW's
    authorization-code flow.
"""
from __future__ import annotations

import io
import json
import mimetypes
import re
import threading
import time
from collections import deque
from urllib.parse import quote, unquote, urlsplit

import pandas as pd
import requests

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

DCTM_JSON = "application/vnd.emc.documentum+json"
LINKREL = "http://identifiers.emc.com/linkrel/"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
OTDS_USER_ID = "urn:opentext.com:oauth:string:user_id"
_OBJECT_ID = re.compile(r"^[0-9a-f]{16}$")

# Documentum format name -> (MIME, extension). `a_content_type` is a FORMAT
# name, not a MIME type; unknown formats are looked up live via /formats.
FORMAT_MAP = {
    "pdf": ("application/pdf", "pdf"),
    "msw8": ("application/msword", "doc"), "msw12": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "excel8book": ("application/vnd.ms-excel", "xls"), "excel12book": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "ppt8": ("application/vnd.ms-powerpoint", "ppt"), "ppt12": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
    "crtext": ("text/plain", "txt"), "text": ("text/plain", "txt"), "csv": ("text/csv", "csv"), "json": ("application/json", "json"),
    "html": ("text/html", "html"), "xml": ("application/xml", "xml"), "jpeg": ("image/jpeg", "jpg"), "png": ("image/png", "png"),
    "gif": ("image/gif", "gif"), "tiff": ("image/tiff", "tif"), "zip": ("application/zip", "zip"),
}


class DocumentumHTTPError(ValueError):
    def __init__(self, status):
        self.status = status
        # Never include provider response bodies (may echo credentials or DQL).
        hint = {
            400: "Documentum rejected the request (bad DQL or parameter).",
            401: "Authentication rejected. Check the selected auth method and account.",
            403: "This identity does not have permission to read the requested object.",
            404: "Repository, folder, or document was not found.",
            429: "Documentum throttled the request. Retry later or narrow the scope.",
        }.get(status, "Documentum request failed.")
        super().__init__(f"Documentum REST HTTP {status}: {hint}")


def _safe_path(value: str) -> str:
    """Reject traversal (including encoded variants) in repository paths."""
    probe = value
    for _ in range(4):
        if "\\" in probe or any(ord(c) < 32 for c in probe):
            raise GlobScopeError("Invalid Documentum path.")
        if any(p in (".", "..") for p in probe.split("/")) or probe.startswith("//"):
            raise GlobScopeError("Path is outside the connection scope.")
        decoded = unquote(probe)
        if decoded == probe:
            break
        probe = decoded
    return value


def _dql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class DocumentumClient(DataSourceClient):
    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}
    # Always list with the caller's identity: a shared catalog must not leak
    # the service account's inventory to a user-required connection.
    cheap_live_listing = True
    is_document_based = True

    def __init__(self, rest_url: str, repository: str, root_path: str = "/",
                 include_globs: str | None = None, recursive: bool = True,
                 object_types: str | None = None, index_mode: str = "metadata",
                 max_catalog_objects: int = 5000, max_file_size_mb: int = 50,
                 allow_http: bool = False,
                 username: str | None = None, password: str | None = None,
                 otds_url: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, partition: str | None = None,
                 documentum_login: str | None = None, access_token: str | None = None,
                 **kwargs):
        self.rest_url = (rest_url or "").strip().rstrip("/")
        parsed = urlsplit(self.rest_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("REST URL must be an HTTP(S) URL such as https://dctm.example.com/dctm-rest, without credentials, query, or fragment.")
        if parsed.scheme == "http" and not allow_http:
            raise ValueError("HTTPS is required. Enable Allow HTTP only for an isolated test lab.")
        _safe_path(unquote(parsed.path))
        self._origin = (parsed.scheme.lower(), parsed.netloc.lower())
        self._prefix = unquote(parsed.path).rstrip("/")
        self.repository = (repository or "").strip()
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_\-.]*$", self.repository):
            raise ValueError("Repository name is required (letters, digits, '_', '-', '.').")
        self._repo_url = f"{self.rest_url}/repositories/{quote(self.repository, safe='')}"
        root = "/" + (root_path or "/").strip().strip("/")
        _safe_path(root)
        self.root_path = root.rstrip("/") or "/"
        self.include_globs = globs_from_str(include_globs)
        self.recursive = bool(recursive)
        types = {t.strip() for t in (object_types or "").split(",") if t.strip()}
        for t in types:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t):
                raise ValueError("Object types must be Documentum type names such as dm_document.")
        self.object_types = None if not types or "dm_document" in types or "dm_sysobject" in types else types
        if index_mode not in ("none", "metadata"):
            raise ValueError("Indexing must be none or metadata.")
        self.index_mode = index_mode
        if not 1 <= int(max_catalog_objects) <= 50000 or not 1 <= int(max_file_size_mb) <= 250:
            raise ValueError("Max files must be 1–50000; max file size must be 1–250 MB.")
        self.max_catalog_objects = int(max_catalog_objects)
        self._max_bytes = int(max_file_size_mb) * 1024 * 1024

        # --- identity -------------------------------------------------------
        self._username, self._password = username, password
        self._otds_url = (otds_url or "").strip().rstrip("/")
        if self._otds_url.endswith("/otdsws"):
            self._otds_url = self._otds_url[: -len("/otdsws")]
        if self._otds_url:
            ot = urlsplit(self._otds_url)
            if ot.scheme not in ("http", "https") or not ot.hostname or ot.username or ot.password:
                raise ValueError("OTDS URL must be an HTTP(S) URL such as https://otds.example.com.")
            if ot.scheme == "http" and not allow_http:
                raise ValueError("HTTPS is required for OTDS. Enable Allow HTTP only for an isolated test lab.")
        self._client_id, self._client_secret = client_id, client_secret
        self._partition = (partition or "").strip()
        self._login = (documentum_login or "").strip()
        self._static_token = access_token
        if access_token:
            self.auth_method = "oauth"
        elif self._login:
            if not (self._otds_url and client_id and client_secret):
                missing = [n for n, v in (("OTDS URL", self._otds_url), ("client ID", client_id), ("client secret", client_secret)) if not v]
                raise ValueError(
                    "OTDS impersonation needs the connection's OTDS OAuth client; missing: " + ", ".join(missing)
                    + ". Save an 'OTDS OAuth client' as the connection's system credentials.")
            self.auth_method = "otds_impersonation"
        elif self._otds_url or client_id or client_secret:
            if not (self._otds_url and client_id and client_secret):
                raise ValueError("OTDS client credentials need the OTDS URL, client ID and client secret.")
            self.auth_method = "otds_client"
        elif username and password:
            self.auth_method = "basic"
        else:
            raise ValueError("Provide a repository username and password, an OTDS OAuth client, or a signed-in OTDS token.")
        self._token, self._token_exp = None, 0.0
        self._token_lock = threading.Lock()
        self._local = threading.local()
        self._folder_paths: dict = {}
        self._formats: dict = dict(FORMAT_MAP)

    # ------------------------------------------------------------------ meta
    @property
    def description(self):
        who = {"basic": "the configured repository user", "otds_client": "the connection's OTDS service identity",
               "otds_impersonation": "the signed-in user (OTDS impersonation)", "oauth": "the signed-in user (OTDS token)"}[self.auth_method]
        return (
            f"Read-only OpenText Documentum repository '{self.repository}' under '{self.root_path}', "
            f"accessed as {who}; Documentum ACLs apply. Use list_files, search_files and read_file "
            "with the returned r_object_id (or scoped path). Search combines the repository's "
            "full-text index with live name/title matches. CSV/Excel reads return DataFrames; "
            "PDF/DOCX/PPTX reads return document text."
        )

    # ------------------------------------------------------------------ auth
    def _session(self):
        # requests.Session is not thread-safe: one per worker thread, no
        # ambient proxy/.netrc credentials, deployment-owned CA bundle only.
        if not hasattr(self._local, "session"):
            import os
            session = requests.Session()
            session.trust_env = False
            session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
            session.headers["Accept"] = DCTM_JSON
            if self.auth_method == "basic":
                session.auth = (self._username, self._password)
            self._local.session = session
        session = self._local.session
        if self.auth_method != "basic":
            session.headers["Authorization"] = "Bearer " + self._bearer()
        return session

    def _bearer(self, force: bool = False) -> str:
        if self.auth_method == "oauth":
            return self._static_token
        with self._token_lock:
            if force or not self._token or time.time() > self._token_exp - 60:
                self._token, self._token_exp = self._fetch_token()
            return self._token

    def _fetch_token(self):
        if self.auth_method == "otds_impersonation":
            subject = self._login if "@" in self._login else (
                f"{self._login}@{self._partition}" if self._partition else self._login)
            form = {"grant_type": TOKEN_EXCHANGE, "subject_token": subject, "subject_token_type": OTDS_USER_ID,
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token"}
        else:
            form = {"grant_type": "client_credentials"}
        form.update({"client_id": self._client_id, "client_secret": self._client_secret})
        import os
        try:
            response = requests.post(self._otds_url + "/otdsws/oauth2/token", data=form, timeout=(10, 30),
                                     headers={"Accept": "application/json"},
                                     verify=os.environ.get("REQUESTS_CA_BUNDLE") or True, allow_redirects=False)
        except requests.exceptions.SSLError as exc:
            raise ValueError("OTDS TLS verification failed. Install the issuing CA in the backend trust store.") from exc
        except requests.RequestException as exc:
            raise ValueError("Cannot reach OTDS. Check the OTDS URL and network access.") from exc
        if response.status_code != 200:
            try:
                err = response.json().get("error_description") or response.json().get("error") or ""
            except ValueError:
                err = ""
            what = "impersonate the user" if self.auth_method == "otds_impersonation" else "authenticate the OTDS client"
            detail = f" ({err})" if err and len(err) < 160 and "secret" not in err.lower() else ""
            raise ValueError(f"OTDS refused to {what}{detail}. Check the OAuth client, its impersonation setting, and the login name.")
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise ValueError("OTDS returned no access token.")
        return token, time.time() + float(data.get("expires_in") or 3600)

    # ------------------------------------------------------------------ http
    def _check_url(self, url: str) -> str:
        p = urlsplit(url)
        if ((p.scheme.lower(), p.netloc.lower()) != self._origin
                or not unquote(p.path).startswith(self._prefix + "/") or p.username or p.password or p.fragment):
            raise ValueError("Refusing a Documentum link outside the configured REST URL (e.g. an ACS/BOCS content server).")
        _safe_path(unquote(p.path))
        return url

    def _request(self, url, *, params=None, binary=False, byte_limit=None):
        # A stale keep-alive connection can fail midway through a streamed GET.
        # Retry once on a fresh session; never return partial bytes or retry a
        # TLS trust failure. An expired Bearer gets exactly one refresh.
        refreshed = False
        for attempt in range(3):
            try:
                return self._request_once(url, params=params, binary=binary, byte_limit=byte_limit)
            except DocumentumHTTPError as exc:
                if exc.status == 401 and self.auth_method in ("otds_client", "otds_impersonation") and not refreshed:
                    refreshed = True
                    self._bearer(force=True)
                    continue
                raise
            except requests.exceptions.SSLError as exc:
                raise ValueError("Documentum TLS verification failed. Install the issuing CA in the backend trust store.") from exc
            except requests.RequestException as exc:
                if hasattr(self._local, "session"):
                    self._local.session.close()
                    del self._local.session
                if attempt >= 1:
                    raise ValueError("Cannot reach Documentum REST Services. Check DNS, network access, and server availability.") from exc

    def _request_once(self, url, *, params=None, binary=False, byte_limit=None):
        url = url if url.startswith(("http://", "https://")) else self._repo_url + url
        self._check_url(url)
        with self._session().get(url, params=params, timeout=(10, 60), allow_redirects=False, stream=True) as response:
            if 300 <= response.status_code < 400:
                raise ValueError("Documentum redirected the request. Use the REST Services base URL as clients see it.")
            if response.status_code != 200:
                raise DocumentumHTTPError(response.status_code)
            limit = min(byte_limit or self._max_bytes, self._max_bytes) if binary else 16 * 1024 * 1024
            if int(response.headers.get("Content-Length", "0") or 0) > limit:
                raise ValueError("Documentum response exceeds the configured byte limit.")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError("Documentum response exceeds the configured byte limit.")
                chunks.append(chunk)
            content = b"".join(chunks)
            if binary:
                return content
            return json.loads(content)

    def _pages(self, url, params=None):
        """Iterate feed entries across `next` links (same-origin only)."""
        visited = set()
        for _ in range(1000):
            if url in visited:
                raise ValueError("Documentum returned a pagination cycle.")
            visited.add(url)
            data = self._request(url, params=params)
            for entry in data.get("entries") or []:
                yield entry
            nxt = next((l.get("href") for l in data.get("links") or [] if l.get("rel") == "next"), None)
            params = None
            if not nxt:
                return
            url = nxt
        raise ValueError("Documentum pagination limit exceeded; narrow the connection scope.")

    @staticmethod
    def _props(entry):
        content = entry.get("content") or {}
        return content.get("properties") or entry.get("properties") or {}

    def _dql(self, statement: str, page_size: int = 200):
        return (self._props(e) for e in self._pages(self._repo_url, {"dql": statement, "items-per-page": page_size}))

    # -------------------------------------------------------------- folders
    def _roots(self):
        """(folder_id, absolute_path) for the configured root(s)."""
        if self.root_path == "/":
            roots = []
            for entry in self._pages(self._repo_url + "/cabinets", {"inline": "true", "items-per-page": 200}):
                props = self._props(entry)
                path = (props.get("r_folder_path") or ["/" + props.get("object_name", "")])[0]
                roots.append((props["r_object_id"], path))
            if not roots:
                raise DocumentumHTTPError(403)
            return roots
        rows = list(self._dql(f"SELECT r_object_id, r_folder_path FROM dm_folder WHERE ANY r_folder_path = {_dql_literal(self.root_path)}"))
        if not rows:
            raise ValueError(f"Root folder '{self.root_path}' was not found or is not visible to this identity.")
        return [(rows[0]["r_object_id"], self.root_path)]

    def _folder_path(self, folder_id: str) -> str | None:
        if folder_id not in self._folder_paths:
            try:
                props = self._request(f"/folders/{folder_id}").get("properties") or {}
                paths = props.get("r_folder_path") or []
                self._folder_paths[folder_id] = paths[0] if paths else None
            except DocumentumHTTPError as exc:
                if exc.status not in (403, 404):
                    raise
                self._folder_paths[folder_id] = None
        return self._folder_paths[folder_id]

    def _display(self, folder_path: str, name: str, *, enforce: bool = True) -> str | None:
        """Path relative to the root; None when outside the root. Enforces globs."""
        _safe_path(folder_path)
        if self.root_path == "/":
            rel_dir = folder_path.strip("/")
        elif folder_path == self.root_path:
            rel_dir = ""
        elif folder_path.startswith(self.root_path + "/"):
            rel_dir = folder_path[len(self.root_path) + 1:]
        else:
            return None
        display = f"{rel_dir}/{name}" if rel_dir else name
        if enforce and not path_matches_globs(display, self.include_globs):
            raise GlobScopeError("File does not match the connection's include patterns.")
        return display

    def _format(self, name: str):
        if name not in self._formats:
            try:
                props = self._request(f"/formats/{quote(name, safe='')}").get("properties") or {}
                self._formats[name] = (props.get("mime_type") or "application/octet-stream", (props.get("dos_extension") or "").lstrip(".").lower())
            except (ValueError, DocumentumHTTPError):
                self._formats[name] = ("application/octet-stream", "")
        return self._formats[name]

    def _entry(self, props, folder_path: str, display: str):
        name = props.get("object_name") or props["r_object_id"]
        fmt = props.get("a_content_type") or ""
        mime, fmt_ext = self._format(fmt) if fmt else (None, "")
        if not _ext(name) and fmt_ext:
            name = f"{name}.{fmt_ext}"
            display = display if _ext(display) else f"{display}.{fmt_ext}"
        size = props.get("r_full_content_size") or props.get("r_content_size") or 0
        return {"id": props["r_object_id"], "name": name, "path": display, "size": int(size or 0),
                "modified_at": props.get("r_modify_date"), "mime_type": mime or mimetypes.guess_type(name)[0],
                "is_folder": False, "object_type": props.get("r_object_type"), "folder_path": folder_path,
                "web_url": f"{self._repo_url}/objects/{props['r_object_id']}"}

    def _type_ok(self, props) -> bool:
        return self.object_types is None or props.get("r_object_type") in self.object_types

    # ------------------------------------------------------------------ list
    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None):
        cap = min(max(0, int(limit)) if limit is not None else self.max_catalog_objects, self.max_catalog_objects)
        if cap == 0:
            return []
        roots = self._roots()
        if folder_id:
            fid = str(folder_id)
            if not _OBJECT_ID.match(fid):
                raise ValueError("folder_id must be a Documentum folder r_object_id.")
            path = self._folder_path(fid)
            if path is None or self._display(path, "", enforce=False) is None:
                raise GlobScopeError("Folder is outside the configured root folder.")
            roots = [(fid, path)]
        recurse = self.recursive if recursive is None else bool(recursive)
        reporter = make_reporter(progress_callback) if progress_callback else None
        if reporter:
            reporter.phase("listing Documentum folders", total=len(roots))
        queue, seen, files, ids = deque(roots), set(), [], set()
        while queue:
            fid, path = queue.popleft()
            if fid in seen:
                continue  # multi-filed folders are reachable twice
            seen.add(fid)
            if len(seen) > self.max_catalog_objects:
                raise ValueError("Folder traversal limit exceeded; narrow the root folder or include patterns.")
            self._folder_paths[fid] = path
            for entry in self._pages(f"{self._repo_url}/folders/{fid}/documents", {"inline": "true", "items-per-page": 200}):
                props = self._props(entry)
                if props.get("r_object_id") in ids or not self._type_ok(props):
                    continue
                try:
                    display = self._display(path, props.get("object_name") or props["r_object_id"])
                except GlobScopeError:
                    continue
                if display is None:
                    continue
                ids.add(props["r_object_id"])
                files.append(self._entry(props, path, display))
                if reporter:
                    reporter.tick(display)
                if len(files) >= cap:
                    return files
            if recurse:
                for entry in self._pages(f"{self._repo_url}/folders/{fid}/folders", {"inline": "true", "items-per-page": 200}):
                    props = self._props(entry)
                    child_path = (props.get("r_folder_path") or [f"{path.rstrip('/')}/{props.get('object_name', '')}"])[0]
                    queue.append((props["r_object_id"], child_path))
        return files

    # --------------------------------------------------------------- resolve
    def _object(self, object_id: str) -> dict:
        body = self._request(f"/objects/{object_id}")
        props = body.get("properties") or {}
        if not props.get("r_object_id"):
            raise DocumentumHTTPError(404)
        return props

    def _scoped_props(self, object_id: str):
        """Object properties plus the in-scope (folder_path, display) or a scope error."""
        props = self._object(object_id)
        if props.get("r_object_type") in ("dm_folder", "dm_cabinet"):
            raise ValueError("The id names a folder, not a document.")
        if not self._type_ok(props):
            raise GlobScopeError("Object type is outside the connection's object types.")
        scope_error = None
        for fid in props.get("i_folder_id") or []:
            fpath = self._folder_path(fid)
            if not fpath:
                continue
            try:
                display = self._display(fpath, props.get("object_name") or object_id)
            except GlobScopeError as exc:
                scope_error = exc
                continue
            if display is not None:
                return props, fpath, display
        raise scope_error or GlobScopeError("Document is outside the configured root folder.")

    def _resolve(self, file_id):
        value = str(file_id or "").strip()
        if _OBJECT_ID.match(value):
            return self._scoped_props(value)
        _safe_path(value)
        rel = value.strip("/")
        folder, _, name = rel.rpartition("/")
        if not name:
            raise ValueError("File id must be a Documentum r_object_id or a path relative to the root folder.")
        abs_folder = self.root_path.rstrip("/") + ("/" + folder if folder else "")
        if self.root_path == "/":
            abs_folder = "/" + folder
        rows = list(self._dql(
            f"SELECT r_object_id FROM dm_document WHERE FOLDER({_dql_literal(abs_folder)}) AND object_name = {_dql_literal(name)}"))
        if len(rows) != 1:
            raise ValueError("File path is missing or ambiguous. Use the id returned by list_files/search_files.")
        return self._scoped_props(rows[0]["r_object_id"])

    # ------------------------------------------------------------------ read
    def read_raw_bytes(self, file_id, *, max_bytes=None):
        props, _, _ = self._resolve(file_id)
        entry = self._entry(props, "", "")
        limit = min(self._max_bytes, max_bytes) if max_bytes is not None else self._max_bytes
        if limit < 1 or entry["size"] > limit:
            raise ValueError("File exceeds the configured byte limit; increase it explicitly to read this file.")
        content_res = self._request(f"/objects/{props['r_object_id']}/contents/content", params={"media-url-policy": "local"})
        href = next((l.get("href") for l in content_res.get("links") or [] if l.get("rel") == "enclosure"), None)
        if not href:
            raise ValueError("Documentum returned no content for this document (a folder, a virtual document root, or a record without a rendition).")
        content = self._request(href, binary=True, byte_limit=limit)
        return content, entry["name"], entry["mime_type"]

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

    # ---------------------------------------------------------------- search
    def _entries_for_hits(self, rows, found, cap):
        for props in rows:
            oid = props.get("r_object_id")
            if not oid or oid in found:
                continue
            try:
                props, fpath, display = self._scoped_props(oid) if "i_folder_id" not in props else (props, None, None)
                if fpath is None:
                    for fid in props.get("i_folder_id") or []:
                        fp = self._folder_path(fid)
                        if fp is None:
                            continue
                        try:
                            display = self._display(fp, props.get("object_name") or oid)
                        except GlobScopeError:
                            continue
                        if display is not None:
                            fpath = fp
                            break
                if fpath is None or not self._type_ok(props):
                    continue
                found[oid] = self._entry(props, fpath, display)
            except (GlobScopeError, DocumentumHTTPError):
                continue
            if len(found) >= cap:
                return

    def search_files(self, query, limit=100, **_):
        term = re.sub(r"[%_'\"\\]", " ", str(query or "")).strip()
        if not term:
            return []
        cap = min(max(0, int(limit)), self.max_catalog_objects, 500)
        if not cap:
            return []
        found: dict = {}
        # 1. Repository full-text index (xPlore when installed; database search
        #    otherwise). Missing index / unsupported → fall through silently.
        try:
            rows = [self._props(e) for e in self._pages(self._repo_url + "/search", {"q": term, "inline": "true", "items-per-page": min(cap, 200)})]
            self._entries_for_hits(rows, found, cap)
        except DocumentumHTTPError as exc:
            if exc.status not in (400, 404, 500, 501):
                raise
        # 2. Live name/title match under the root — indexing is asynchronous, so
        #    never pretend a brand-new document is unsearchable.
        if len(found) < cap:
            low = term.lower()
            like = _dql_literal(f"%{low}%")
            rows = self._dql(
                "SELECT r_object_id, object_name, title, r_object_type, a_content_type, r_full_content_size, r_modify_date, i_folder_id "
                f"FROM dm_document WHERE FOLDER({_dql_literal(self.root_path)}, DESCEND) "
                f"AND (LOWER(object_name) LIKE {like} OR LOWER(title) LIKE {like})")
            self._entries_for_hits(rows, found, cap)
        return list(found.values())[:cap]

    # -------------------------------------------------------------- contract
    def test_connection(self):
        try:
            repo = self._request(self._repo_url)
            me = (self._request("/currentuser").get("properties") or {})
            self.list_files(limit=1, recursive=False)
            return {"success": True, "message": f"Connected to Documentum repository '{self.repository}'.",
                    "details": {"repository": repo.get("name") or self.repository, "identity": me.get("user_login_name") or me.get("user_name"),
                                "auth_method": self.auth_method, "root_path": self.root_path}}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def get_schemas(self, progress_callback=None):
        if self.index_mode == "none":
            return []
        return [Table(name=f["path"], description=f"Documentum {f.get('object_type') or 'document'} ({_ext(f['name']) or 'file'})",
                      columns=[], pks=[], fks=[], metadata_json={"documentum": {
                          "file_id": f["id"], "mime_type": f["mime_type"], "size": f["size"], "modified_at": f["modified_at"],
                          "web_url": f["web_url"], "object_type": f.get("object_type"), "folder_path": f.get("folder_path"),
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
