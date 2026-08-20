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
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients._document_text import (
    DOC_EXTS,
    doc_text_is_usable,
    extract_document_text_from_bytes,
    extract_pdf_pages_text,
)
from app.data_sources.clients._file_source_common import (
    GlobScopeError,
    NamedBytes,
    INDEX_NONE,
    globs_from_str,
    normalize_index_mode,
    path_matches_globs,
)
from app.data_sources.clients.progress import ProgressCallback, make_reporter


logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_BASE = "https://login.microsoftonline.com"

# Children are fetched a page at a time; 200 is Graph's practical max for
# /children and keeps a big flat folder to a couple of round-trips.
GRAPH_PAGE_SIZE = 200

# Folders enumerated in parallel during a walk. Graph is round-trip bound (a
# `children` call is ~200-600 ms), so a serial walk of a folder-heavy drive —
# the shape of every personal OneDrive — spends its whole wall-clock waiting.
# Kept modest: Graph throttles aggressive per-app concurrency.
WALK_CONCURRENCY_DEFAULT = 8

# Page size for the connection-test probe: we only need to know that the
# scoped root is readable, not what is in it.
PROBE_PAGE_SIZE = 5


# Extensions we try to read as DataFrames; everything else is returned as
# bytes/text. Order matters only for human-readable docs.
TABULAR_EXTS = {"csv", "tsv", "xlsx", "xls"}
TEXT_EXTS = {"txt", "md", "json", "html", "htm", "log", "yaml", "yml"}


def _ext(name: str) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _extract_pdf_pages_from_bytes(data: bytes, name: str, first: int, last: int) -> tuple:
    """Page-range PDF extraction over in-memory Graph bytes via a temp file
    (the extractor dispatches on filename). Same approach as the S3 client's
    helper of the same name."""
    import os
    import tempfile

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as fh:
            fh.write(data)
            tmp = fh.name
        return extract_pdf_pages_text(tmp, first, last)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _trim_to_data(df: "pd.DataFrame") -> "pd.DataFrame":
    """Excel files exported from BI tools / users often have leading blank
    rows and blank columns before the actual table starts. Pandas with
    `header=0` then treats row 1 (empty) as the column names and returns
    a 0-row frame — even though the sheet has 10+ rows of real data.

    Drop fully-empty rows and columns, then promote the first remaining row
    to header. Idempotent for well-formed sheets (no leading blanks → no-op).
    """
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return pd.DataFrame()
    header = df.iloc[0].tolist()
    body = df.iloc[1:].reset_index(drop=True)
    body.columns = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(header)]
    return body


class GraphDriveClient(DataSourceClient):
    """Microsoft Graph file source. Used by SharePoint and OneDrive registry entries."""

    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}

    @property
    def description(self) -> str:
        if self.mode == "sharepoint":
            if self._all_libraries:
                library = " / all document libraries"
            elif self.drive_name:
                library = f" / library {self.drive_name}"
            else:
                library = ""
            return (
                f"SharePoint site {self.site_url}"
                + library
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
        include_globs: Optional[str] = None,
        allowed_extensions: Optional[str] = None,
        recursive: bool = False,
        # Catalog scope / cost controls (same knobs as network_dir + s3)
        index_mode: Optional[str] = None,
        max_catalog_objects: int = 5000,
        walk_concurrency: Optional[int] = None,
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
        # Glob scope relative to folder_path — the connection's access boundary,
        # enforced at the read chokepoint (mirrors network_dir / s3). Paths are
        # matched relative to the scoped root, so a pattern like 'Reports/**/*.xlsx'
        # is written the same way here as for the other file connectors.
        self.include_globs = globs_from_str(include_globs)
        self.allowed_extensions = self._parse_exts(allowed_extensions)
        self.recursive = bool(recursive)
        self.mode = mode

        # Index tier. Graph listings are metadata-only (no content extraction
        # here yet), so `metadata` and `content` behave the same; `none` skips
        # the catalog entirely and the tool layer lists/reads live.
        self.index_mode = normalize_index_mode(index_mode)
        # Hard ceiling on catalog rows. A drive with a six-figure file count
        # would otherwise build a catalog nothing can use, one Graph page at a
        # time. Mirrors s3's `max_catalog_objects`.
        self.max_catalog_objects = int(max_catalog_objects) if max_catalog_objects else 5000
        try:
            self.walk_concurrency = max(1, int(walk_concurrency or WALK_CONCURRENCY_DEFAULT))
        except (TypeError, ValueError):
            self.walk_concurrency = WALK_CONCURRENCY_DEFAULT

        # One pooled HTTP client per client instance. Every Graph call used to
        # go through the module-level `httpx.get`, which opens (and throws away)
        # a TCP+TLS connection per request — on a walk that is a fresh handshake
        # per folder. Reused here so a walk pays the handshake once.
        self._http: Optional[httpx.Client] = None
        self._http_lock = threading.Lock()
        # Serializes token acquisition: with a concurrent walk, N threads
        # hitting a cold client would otherwise each POST the token endpoint.
        self._token_lock = threading.Lock()

        # Snapshot whether a user OAuth token was provided up front. `_token()`
        # may later populate `self.access_token` with an app-only token in
        # service-principal mode, which would otherwise be indistinguishable
        # from a delegated user token — and `/me/*` endpoints reject app-only
        # tokens with HTTP 400.
        self._user_token_provided = bool(access_token)

        # Cached IDs (resolved lazily)
        self._site_id: Optional[str] = None
        self._drive_id: Optional[str] = None
        # Every drive this connection spans: [(drive_id, library_name), ...].
        # One entry unless drive_name == '*' (all libraries on the site).
        self._drives: Optional[List[tuple]] = None
        # Per-drive scoped roots, keyed by drive id (folder_path resolves per
        # library, and may legitimately be absent from some of them).
        self._root_item_ids: Dict[str, str] = {}
        self._root_item_id: Optional[str] = None  # scoped root (after folder_path)

    # ------------------------------------------------------------------ http

    def _client(self) -> httpx.Client:
        """The pooled HTTP client, created on first use.

        `follow_redirects` stays off (the JSON endpoints don't redirect); the
        content download passes it per-request so httpx keeps stripping the
        Authorization header on the cross-origin hop to the CDN, as before.
        """
        client = self._http
        if client is not None and not client.is_closed:
            return client
        with self._http_lock:
            if self._http is None or self._http.is_closed:
                pool = max(4, self.walk_concurrency + 2)
                self._http = httpx.Client(
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    limits=httpx.Limits(
                        max_keepalive_connections=pool, max_connections=pool * 2
                    ),
                )
            return self._http

    def close(self) -> None:
        """Release the pooled connections. Safe to call more than once."""
        with self._http_lock:
            client, self._http = self._http, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def __del__(self):
        """Clients are constructed per request / per indexing run and dropped;
        without this the pooled sockets would sit open until the process exited.
        Never raises — this can run during interpreter shutdown."""
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ auth

    def _token(self) -> str:
        """Return a valid bearer token. Prefers delegated; falls back to client-credentials."""
        if self.access_token:
            return self.access_token
        if not (self.tenant_id and self.client_id and self.client_secret):
            raise ValueError("No access_token and no service-principal credentials configured")
        with self._token_lock:
            # Another thread may have acquired one while we waited.
            if self.access_token:
                return self.access_token
            resp = self._client().post(
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

    def _can_remint_token(self) -> bool:
        """True when a fresh token can be obtained without the user.

        Delegated tokens arrive from the OAuth layer already minted; there is
        nothing this client can do to renew one mid-request.
        """
        return (
            not self._user_token_provided
            and bool(self.tenant_id and self.client_id and self.client_secret)
        )

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

    def _rel_from_parent(self, parent_path: Optional[str], name: str) -> str:
        """Path of an item relative to the connection's scoped root (folder_path).

        Graph reports a parent as e.g. '/drives/{id}/root:/Reports/2025'. We take
        the portion after 'root:', append the file name, then strip the
        folder_path prefix so globs are matched the same way they are written
        (relative to the configured folder), exactly like s3's prefix-relative id.
        """
        parent = parent_path or ""
        after = parent.split("root:", 1)[1] if "root:" in parent else ""
        full = "/".join(p for p in [after.strip("/"), (name or "").strip("/")] if p)
        root = (self.folder_path or "").strip("/")
        if root and (full == root or full.startswith(root + "/")):
            full = full[len(root):].strip("/")
        return full

    def _library_name(self, drive_id: str) -> str:
        for did, lib in (self._drives or []):
            if did == drive_id:
                return lib
        return ""

    def _scoped_path(self, drive_id: str, parent_path: Optional[str], name: str) -> str:
        """Path as the LISTING presents it — library-prefixed when the
        connection spans several libraries — so globs are matched against the
        same string the user sees and writes patterns for."""
        rel = self._rel_from_parent(parent_path, name)
        if not self._all_libraries:
            return rel
        lib = self._library_name(drive_id)
        return f"{lib}/{rel}" if lib and rel else (lib or rel)

    def _enforce_scope(self, rel_path: str) -> None:
        """Single access chokepoint: an in-drive but off-glob path is DENIED
        (not merely hidden), mirroring network_dir / s3."""
        if self.include_globs and not path_matches_globs(rel_path, self.include_globs):
            raise GlobScopeError(
                f"'{rel_path}' is outside this connection's allowed patterns "
                f"({', '.join(self.include_globs)}). Access denied."
            )

    def _get(self, path: str, **kwargs) -> dict:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        client = self._client()
        resp = client.get(url, headers=self._headers(), timeout=30, **kwargs)
        if resp.status_code == 401 and self._can_remint_token():
            # App-only token expired mid-call; drop it and mint a fresh one.
            #
            # Only when we can actually MINT a replacement. A delegated user
            # token cannot be re-minted in-process, so clearing it just made
            # the retry's `_headers()` raise "No access_token and no
            # service-principal credentials configured" — destroying the real
            # Graph error. That mattered most for OneNote, where a 401 is the
            # normal way Graph reports "this account has no personal notebook
            # store" (error 40001) and the useful message was being replaced by
            # a bogus credentials one.
            self.access_token = None
            resp = client.get(url, headers=self._headers(), timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise ValueError(f"Graph {url} → {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def _get_bytes(self, path: str) -> bytes:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        resp = self._client().get(
            url, headers=self._headers(), timeout=60, follow_redirects=True
        )
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

    @property
    def _all_libraries(self) -> bool:
        """True when the connection spans EVERY document library on the site.

        Opted into with the ``*`` sentinel in ``drive_name``. A blank field keeps
        the historical behaviour (the site's default library only) so existing
        connections do not silently change shape — their indexed paths, globs and
        catalog rows stay exactly as they were.
        """
        return self.mode != "onedrive" and (self.drive_name or "").strip() == "*"

    def _resolve_drives(self) -> List[tuple]:
        """Resolve the drives this connection spans as ``[(drive_id, name), ...]``.

        One entry for OneDrive, for a named library, and for the default-library
        case; every library on the site when ``drive_name == '*'``. Cached.
        """
        if self._drives is not None:
            return self._drives

        if self.mode == "onedrive":
            data = self._get("/me/drive")
            self._drives = [(data["id"], data.get("name") or "OneDrive")]
            return self._drives

        site_id = self._resolve_site_id()

        if self._all_libraries:
            data = self._get(f"/sites/{site_id}/drives")
            drives = [(d["id"], d.get("name") or d["id"]) for d in data.get("value", [])]
            if not drives:
                raise ValueError("No document libraries found on site")
            # Stable order so listings (and the 500-result cap) are deterministic.
            self._drives = sorted(drives, key=lambda t: t[1].lower())
            return self._drives

        if not self.drive_name:
            data = self._get(f"/sites/{site_id}/drive")
            self._drives = [(data["id"], data.get("name") or data["id"])]
            return self._drives

        data = self._get(f"/sites/{site_id}/drives")
        for d in data.get("value", []):
            if d.get("name") == self.drive_name:
                self._drives = [(d["id"], d.get("name") or d["id"])]
                return self._drives
        raise ValueError(f"Document library '{self.drive_name}' not found on site")

    def _resolve_drive_id(self) -> str:
        """The primary drive — first of :meth:`_resolve_drives`.

        Retained for single-drive call sites; multi-library paths must iterate
        ``_resolve_drives()`` instead.
        """
        if self._drive_id:
            return self._drive_id
        self._drive_id = self._resolve_drives()[0][0]
        return self._drive_id

    def _resolve_root_item_id(self, drive_id: Optional[str] = None) -> str:
        """Scoped root of a drive (``folder_path`` applied), cached per drive."""
        if drive_id is None:
            if self._root_item_id:
                return self._root_item_id
            drive_id = self._resolve_drive_id()
            self._root_item_id = self._fetch_root_item_id(drive_id)
            return self._root_item_id
        if drive_id not in self._root_item_ids:
            self._root_item_ids[drive_id] = self._fetch_root_item_id(drive_id)
        return self._root_item_ids[drive_id]

    def _fetch_root_item_id(self, drive_id: str) -> str:
        if not self.folder_path:
            data = self._get(f"/drives/{drive_id}/root")
        else:
            encoded = quote(self.folder_path)
            data = self._get(f"/drives/{drive_id}/root:/{encoded}")
        return data["id"]

    # ----------------------------------------------------------- enumeration

    def _list_children(
        self,
        drive_id: str,
        item_id: str,
        *,
        page_size: int = GRAPH_PAGE_SIZE,
        max_entries: Optional[int] = None,
    ) -> List[dict]:
        """All child entries of a folder, following Graph's paging.

        `max_entries` stops paging once enough entries are in hand — used by the
        connection-test probe, which only needs to prove the folder is readable.
        """
        out: List[dict] = []
        url: Optional[str] = (
            f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children?$top={page_size}"
        )
        while url:
            data = self._get(url)
            page = data.get("value", []) or []
            out.extend(page)
            if max_entries is not None and len(out) >= max_entries:
                return out[:max_entries]
            url = data.get("@odata.nextLink")
        logger.debug(
            "graph_drive._list_children: drive=%s item=%s → %d entries "
            "(folders=%d files=%d)",
            drive_id, item_id, len(out),
            sum(1 for e in out if "folder" in e),
            sum(1 for e in out if "file" in e),
        )
        return out

    def _collect_children(
        self,
        drive_id: str,
        entries: List[dict],
        prefix: str,
        files_out: List[dict],
        folders_out: List[tuple],
    ) -> None:
        """Split one folder listing into in-scope files and subfolders to visit.

        Shared by the serial and concurrent walks so the two can never disagree
        on filtering (extension allow-list, glob scope) or on the shape of a
        returned row.
        """
        for entry in entries:
            name = entry.get("name", "")
            path = f"{prefix}/{name}" if prefix else name
            if "folder" in entry:
                if self.recursive:
                    folders_out.append((entry["id"], path))
                continue
            if not self._allowed(name):
                continue
            # `path` is already relative to the scoped root — filter by the
            # connection's glob scope so listings never surface off-scope files.
            if self.include_globs and not path_matches_globs(path, self.include_globs):
                continue
            files_out.append({
                # Spanning several libraries makes a bare item id ambiguous —
                # read_file would have to guess which drive to open it from.
                # Qualify it so the id round-trips back to its own library.
                # Single-library connections keep the plain id they always had.
                "id": self._qualify_item_id(drive_id, entry["id"]),
                "name": name,
                "path": path,
                "mime_type": (entry.get("file") or {}).get("mimeType"),
                "size": entry.get("size"),
                "modified_at": entry.get("lastModifiedDateTime"),
                "is_folder": False,
                "web_url": entry.get("webUrl"),
                "drive_id": drive_id,
            })

    def _walk(
        self,
        drive_id: str,
        item_id: str,
        prefix: str = "",
        *,
        limit: Optional[int] = None,
        reporter=None,
    ) -> List[dict]:
        """Enumerate files under a folder, honoring `self.recursive`.

        Breadth-first, one level at a time, with the folders of each level
        listed CONCURRENTLY (`walk_concurrency`). Graph enumeration is pure
        round-trip latency, so a serial DFS over a folder-heavy drive — every
        personal OneDrive — spends its whole wall-clock waiting on one request
        at a time. `limit` stops the walk early once enough files are in hand
        (probes, bounded validation counts).
        """
        if self.walk_concurrency <= 1 or not self.recursive:
            return self._walk_serial(drive_id, item_id, prefix, limit=limit, reporter=reporter)

        results: List[dict] = []
        level: List[tuple] = [(item_id, prefix)]
        folders_done = 0
        folders_seen = 1
        with ThreadPoolExecutor(
            max_workers=self.walk_concurrency, thread_name_prefix="graph-walk"
        ) as pool:
            while level:
                # `map` keeps results in submission order, so the catalog stays
                # stable across runs even though the requests are concurrent.
                listings = list(pool.map(
                    lambda folder: self._list_children(drive_id, folder[0]), level
                ))
                next_level: List[tuple] = []
                for (_folder_id, folder_prefix), entries in zip(level, listings):
                    self._collect_children(
                        drive_id, entries, folder_prefix, results, next_level
                    )
                    folders_done += 1
                    if reporter is not None:
                        reporter.item(folder_prefix or "/", done=folders_done)
                folders_seen += len(next_level)
                if reporter is not None:
                    reporter.set_total(folders_seen)
                if limit is not None and len(results) >= limit:
                    break
                level = next_level

        if limit is not None:
            return results[:limit]
        return results

    def _walk_serial(
        self,
        drive_id: str,
        item_id: str,
        prefix: str = "",
        *,
        limit: Optional[int] = None,
        reporter=None,
    ) -> List[dict]:
        """Depth-first single-threaded walk. Used when concurrency is disabled
        and for the (single-request) non-recursive case."""
        results: List[dict] = []
        folders: List[tuple] = []
        self._collect_children(
            drive_id, self._list_children(drive_id, item_id), prefix, results, folders
        )
        if reporter is not None:
            reporter.item(prefix or "/")
        for folder_id, folder_prefix in folders:
            if limit is not None and len(results) >= limit:
                break
            results.extend(self._walk_serial(
                drive_id, folder_id, folder_prefix, limit=limit, reporter=reporter,
            ))
        if limit is not None:
            return results[:limit]
        return results

    # ------------------------------------------------------- id qualification

    def _qualify_item_id(self, drive_id: str, item_id: str) -> str:
        """``drive_id|item_id`` when the connection spans several libraries.

        Graph drive ids contain ``!`` and item ids are base64url, so ``|`` never
        occurs in either and is a safe separator.
        """
        if not self._all_libraries:
            return item_id
        return f"{drive_id}|{item_id}"

    @staticmethod
    def _split_item_id(value: str) -> tuple:
        """Split a qualified id back into ``(drive_id_or_None, item_id)``."""
        if value and "|" in value:
            did, _, iid = value.partition("|")
            if did and iid:
                return did, iid
        return None, value

    # ---------------------------------------------------- public capabilities

    def list_files(
        self,
        folder_id: Optional[str] = None,
        recursive: Optional[bool] = None,
        limit: Optional[int] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> List[dict]:
        """Files under the scoped root (or `folder_id`).

        `limit` caps the walk — a caller that only needs "are there files, and
        roughly how many" (the pre-save connection test) must not pay for a full
        enumeration of a large library.
        """
        # OneDrive enumeration goes through /me/drive, which only works with a
        # delegated user token. Without one (e.g. admin-save indexing before
        # any user has signed in), return an empty catalog rather than 400.
        # The real enumeration runs per-user once a user completes OAuth.
        if self.mode == "onedrive" and not self._user_token_provided:
            return []
        started = time.perf_counter()
        reporter = make_reporter(progress_callback) if progress_callback else None
        if reporter is not None:
            reporter.phase("listing folders", total=1)
        prev = self.recursive
        if recursive is not None:
            self.recursive = bool(recursive)
        try:
            if self._all_libraries and not folder_id:
                # Fan out across every library. Paths are prefixed with the
                # library name so they stay unique (two libraries can both hold
                # 'report.xlsx') and globs can address a specific library.
                results: List[dict] = []
                scanned = 0
                for did, lib in self._resolve_drives():
                    if limit is not None and len(results) >= limit:
                        break
                    try:
                        root = self._resolve_root_item_id(did)
                    except Exception as e:
                        # folder_path need not exist in every library, and a
                        # library can be inaccessible to this identity. Skip it
                        # rather than failing the whole listing.
                        logger.info(
                            "graph_drive: skipping library %r — %s", lib, str(e)[:200]
                        )
                        continue
                    remaining = None if limit is None else limit - len(results)
                    results.extend(self._walk(
                        did, root, prefix=lib, limit=remaining, reporter=reporter,
                    ))
                    scanned += 1
                logger.info(
                    "graph_drive.list_files: mode=%s libraries=%d recursive=%s "
                    "ext_filter=%s limit=%s concurrency=%d → %d file(s) in %.1fs",
                    self.mode, scanned, self.recursive,
                    sorted(self.allowed_extensions) if self.allowed_extensions else None,
                    limit, self.walk_concurrency, len(results),
                    time.perf_counter() - started,
                )
                return results

            drive_id = self._resolve_drive_id()
            item_id = folder_id or self._resolve_root_item_id()
            results = self._walk(drive_id, item_id, limit=limit, reporter=reporter)
        finally:
            self.recursive = prev
        logger.info(
            "graph_drive.list_files: mode=%s drive=%s root=%s recursive=%s "
            "ext_filter=%s limit=%s concurrency=%d → %d file(s) in %.1fs",
            self.mode, drive_id, item_id, self.recursive,
            sorted(self.allowed_extensions) if self.allowed_extensions else None,
            limit, self.walk_concurrency, len(results),
            time.perf_counter() - started,
        )
        return results

    def _looks_like_filename(self, value: str) -> bool:
        """Graph item IDs are opaque alphanumeric tokens (~36 chars, no dots
        or slashes). Filenames almost always have a `.ext` and may include
        `/`. Heuristic but cheap; on false positive we fall back through
        path-then-search resolution which either path-resolves or returns
        a clear 404 the caller can surface."""
        if not value:
            return False
        return ("." in value) or ("/" in value) or (" " in value)

    def _resolve_item_id(self, drive_id: str, file_id_or_name: str) -> str:
        """Accept either an opaque Graph item id OR a filename/path; return
        the opaque id. Tries in order:
          1. If it doesn't look like a filename, assume id (cheapest path).
          2. Path-based lookup under the connection's scoped root.
          3. Drive-wide search by name; take first non-folder hit.
        """
        if not self._looks_like_filename(file_id_or_name):
            return file_id_or_name
        # Path lookup: relative to the connection's root (folder_path).
        try:
            base_path = (self.folder_path or "").strip("/")
            rel = file_id_or_name.lstrip("/")
            joined = f"{base_path}/{rel}" if base_path else rel
            encoded = quote(joined)
            meta = self._get(f"/drives/{drive_id}/root:/{encoded}")
            if meta and meta.get("id"):
                return meta["id"]
        except Exception:
            pass
        # Search fallback: drive-wide name search; pick first file hit.
        try:
            encoded = self._search_term(file_id_or_name)
            data = self._get(f"/drives/{drive_id}/root/search(q='{encoded}')")
            for entry in (data.get("value") or []):
                if "folder" in entry:
                    continue
                if entry.get("name") == file_id_or_name or file_id_or_name in (entry.get("name") or ""):
                    return entry["id"]
        except Exception:
            pass
        # Couldn't resolve — pass original through so Graph raises a clear 404.
        return file_id_or_name

    def _locate(self, file_id: str) -> tuple:
        """Resolve ``file_id`` to ``(drive_id, item_id)``.

        Handles the three things a caller can hand us when the connection spans
        several libraries:
          1. a qualified ``drive|item`` id straight from list_files — routed
             directly, no extra calls;
          2. a bare item id — tried against each library in turn;
          3. a filename (which the model passes often) — resolved per library
             via the existing path-then-search fallback.
        Single-library connections take the first branch's fast path unchanged.
        """
        qualified_drive, raw_id = self._split_item_id(file_id)
        if qualified_drive:
            return qualified_drive, raw_id

        if not self._all_libraries:
            drive_id = self._resolve_drive_id()
            return drive_id, self._resolve_item_id(drive_id, raw_id)

        # Unqualified id/filename against a multi-library connection: probe each
        # library and keep the first that actually resolves to an item.
        last_error: Optional[Exception] = None
        for did, _lib in self._resolve_drives():
            try:
                candidate = self._resolve_item_id(did, raw_id)
                # _resolve_item_id echoes the input back when it cannot resolve;
                # confirm the item really lives in this drive before committing.
                self._get(f"/drives/{did}/items/{candidate}")
                return did, candidate
            except Exception as e:
                last_error = e
                continue
        if last_error is not None:
            raise ValueError(
                f"'{file_id}' was not found in any document library on this site "
                f"({last_error})"
            )
        return self._resolve_drive_id(), raw_id

    def read_file(
        self,
        file_id: str,
        sheet: Optional[str] = None,
        max_bytes: Optional[int] = None,
        page_range: Optional[Tuple[int, int]] = None,
        **_,
    ) -> Any:
        # Routes to the owning library AND resolves a filename → item id (the
        # LLM frequently passes "Book 7.xlsx" where an opaque id is expected,
        # which Graph rejects with 400).
        drive_id, resolved_id = self._locate(file_id)
        meta = self._get(f"/drives/{drive_id}/items/{resolved_id}")
        name = meta.get("name", "")
        # Access boundary: enforce the connection's glob scope BEFORE fetching
        # bytes. We already hold the item metadata (parentReference + name), so
        # this costs no extra round-trip. An in-drive but off-glob item is denied.
        # Across libraries the listed paths carry a library prefix, so the path
        # checked here must carry it too or the globs would be matched against a
        # different string than the one the user wrote them for.
        self._enforce_scope(self._scoped_path(drive_id, (meta.get("parentReference") or {}).get("path"), name))
        ext = _ext(name)
        content = self._get_bytes(f"/drives/{drive_id}/items/{resolved_id}/content")

        # Page-range read for PDFs — same sentinel contract as network_dir/s3.
        # Graph has no ranged read API for this; the file is already fully
        # downloaded above, so extraction is the same local pypdf pass those
        # clients use.
        if page_range is not None:
            if ext != "pdf":
                raise ValueError("page_range is supported for PDF files only.")
            text, pages_total = _extract_pdf_pages_from_bytes(content, name, page_range[0], page_range[1])
            return {
                "__doc_pages__": True,
                "text": text,
                "pages_total": pages_total,
                "first": max(1, page_range[0]),
                "last": min(page_range[1], pages_total),
            }

        if max_bytes and len(content) > max_bytes:
            content = content[:max_bytes]

        # Rich documents (pdf/docx/pptx): return extracted plain text so the
        # agent can actually read them instead of opaque bytes — same contract
        # as the network_dir and s3 clients. Near-empty extraction (scanned /
        # image-based PDF) falls back to raw bytes so the tool can render it
        # for a vision model.
        mime = (meta.get("file") or {}).get("mimeType")
        # Graph item ids are opaque tokens with no extension, so bare bytes
        # would reach the renderer unidentifiable. Carry the real name.
        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(content, name)
            if doc_text_is_usable(text, ext):
                return text
            return NamedBytes(content, name=name, mime=mime)

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
        return NamedBytes(content, name=name, mime=mime)

    def read_raw_bytes(self, file_id: str):
        """Raw item bytes + name + mime, unparsed — for attach_file (persist
        the ORIGINAL file) and the read_file tool's PDF→images vision fallback.
        Same access boundary as read_file: off-glob items are denied."""
        drive_id = self._resolve_drive_id()
        resolved_id = self._resolve_item_id(drive_id, file_id)
        meta = self._get(f"/drives/{drive_id}/items/{resolved_id}")
        name = meta.get("name", "")
        self._enforce_scope(self._rel_from_parent((meta.get("parentReference") or {}).get("path"), name))
        content = self._get_bytes(f"/drives/{drive_id}/items/{resolved_id}/content")
        return content, name, (meta.get("file") or {}).get("mimeType")

    @staticmethod
    def _search_term(query: str) -> str:
        """Make a user/LLM-authored query safe for Graph's ``search(q='…')``.

        The term is interpolated into the URL **path**, and ASP.NET rejects the
        whole request with
        ``400 invalidRequest "A potentially dangerous Request.Path value was
        detected"`` when it contains characters like ``*``, ``?``, ``%``, ``&``
        or ``#`` — even percent-encoded. A model asked to find images naturally
        searches for ``*.png``, and every such call 400'd (observed live on both
        OneDrive and SharePoint).

        Graph's drive search is a substring match with no wildcard support, so
        dropping the wildcards loses nothing: ``*.png`` becomes ``.png``, which
        is what the caller meant. Returns a percent-encoded, path-safe term.

        ``/`` is stripped as well and the result is encoded with ``safe=""``:
        ``quote`` leaves ``/`` alone by default, so a query like ``a/b`` — or
        ``../secrets`` — would survive into the URL path and change which Graph
        endpoint is addressed rather than what is searched for.
        """
        cleaned = (query or "")
        for ch in "*?%&#<>:\\\"|/":
            cleaned = cleaned.replace(ch, " ")
        cleaned = " ".join(cleaned.split()).strip()
        # A query of only wildcards ("*", "*.*") would collapse to nothing —
        # Graph rejects an empty term, so fall back to a bare dot which matches
        # any file that has an extension.
        if not cleaned:
            cleaned = "."
        return quote(cleaned, safe="")

    def search_files(self, query: str, **_) -> List[dict]:
        encoded = self._search_term(query)
        results = []
        for drive_id, _lib in self._resolve_drives():
            try:
                data = self._get(f"/drives/{drive_id}/root/search(q='{encoded}')")
            except Exception as e:
                # One unreadable library must not fail a site-wide search.
                logging.getLogger(__name__).info(
                    "graph_drive: search skipped library %r — %s", _lib, str(e)[:200]
                )
                continue
            results.extend(self._search_hits(drive_id, data))
        return results

    def _search_hits(self, drive_id: str, data: dict) -> List[dict]:
        results = []
        for entry in data.get("value", []):
            if "folder" in entry:
                continue
            if not self._allowed(entry.get("name", "")):
                continue
            # Drive-wide search can return hits outside the connection's scoped
            # root / globs — keep only in-scope results.
            rel = self._scoped_path(drive_id, (entry.get("parentReference") or {}).get("path"), entry.get("name", ""))
            root = (self.folder_path or "").strip("/")
            if root:
                pr = (entry.get("parentReference") or {}).get("path") or ""
                after = pr.split("root:", 1)[1].strip("/") if "root:" in pr else ""
                full = "/".join(p for p in [after, (entry.get("name") or "")] if p)
                if not (full == root or full.startswith(root + "/")):
                    continue  # outside the scoped folder entirely
            if self.include_globs and not path_matches_globs(rel, self.include_globs):
                continue
            results.append({
                "id": self._qualify_item_id(drive_id, entry["id"]),
                "name": entry.get("name"),
                # The clean root-relative path (same shape as list_files),
                # NOT the raw Graph parentReference ("/drives/b!…/root:/…")
                # — `rel` is already computed above for the scope check.
                "path": rel,
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

        BOUNDED BY DESIGN — this never enumerates the drive. Each step is a
        single Graph call and the last one reads only the first
        `PROBE_PAGE_SIZE` children of the scoped root, which is what proves
        access; counting the library is the indexing job's work, not the test's.

        Two modes:
        - Delegated (a user access_token is present): resolve the drive, touch
          the configured root, and probe one page of it — proves end-to-end
          access.
        - Admin-only (service-principal credentials, no user token yet): just
          verify the token can be acquired. Drive/root access needs a user
          token, which arrives after a user completes OAuth — testing it now
          would fail with `/me request is only valid with delegated
          authentication flow` for OneDrive, or with insufficient privileges
          for SharePoint depending on app permissions. For SharePoint we also
          resolve the site URL since `/sites/{id}` works app-only and proves
          the configured URL is reachable.

        Every result carries per-step `timings` and a `details` dict so a slow
        or failing test says WHICH step was slow (token vs site vs drive vs
        root) instead of just spinning.
        """
        t0 = time.perf_counter()
        timings: Dict[str, float] = {}
        details: Dict[str, Any] = {
            "mode": self.mode,
            "auth": "delegated" if self._user_token_provided else "service_principal",
            "scope": self.folder_path or "/",
        }

        def _step(key: str, fn):
            started = time.perf_counter()
            try:
                return fn()
            finally:
                timings[key] = round((time.perf_counter() - started) * 1000, 1)

        def _result(success: bool, message: str) -> dict:
            timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return {
                "success": success,
                "message": message,
                "timings": timings,
                "details": details,
            }

        try:
            # Acquire a token either way; this validates the credentials.
            # In service-principal mode this populates `self.access_token`
            # with an app-only token, so we use `_user_token_provided`
            # (captured at __init__) to distinguish.
            _step("token_ms", self._token)

            if self._user_token_provided:
                # We have a user token — exercise the real read path, but only
                # the first page of the PRIMARY library's root. A site with `*`
                # can span dozens of libraries; probing each one would rebuild
                # the very cost this check exists to avoid, and reaching one
                # proves the token, the site and the folder scope all resolve.
                drives = _step("drive_ms", self._resolve_drives)
                drive_id = drives[0][0]
                root_id = _step("root_ms", lambda: self._resolve_root_item_id(drive_id))
                sample = _step("probe_ms", lambda: self._list_children(
                    drive_id, root_id,
                    page_size=PROBE_PAGE_SIZE, max_entries=PROBE_PAGE_SIZE,
                ))
                details["root_readable"] = True
                details["sample_entry_count"] = len(sample)
                details["catalog"] = "indexed in the background"
                details["library_count"] = len(drives)
                if self._all_libraries:
                    names = ", ".join(n for _, n in drives[:5])
                    more = f" (+{len(drives) - 5} more)" if len(drives) > 5 else ""
                    return _result(
                        True,
                        f"Connected. Indexing {len(drives)} document "
                        f"librar{'y' if len(drives) == 1 else 'ies'}: {names}{more}.",
                    )
                where = self.folder_path or (
                    "the site library" if self.mode == "sharepoint" else "OneDrive"
                )
                if sample:
                    return _result(True, f"Connected — {where} is readable.")
                return _result(
                    True,
                    f"Connected — {where} is readable but currently empty.",
                )

            if self.mode == "sharepoint":
                # App-only token can resolve the site (proves URL + perms).
                details["site_id"] = _step("site_ms", self._resolve_site_id)
                return _result(
                    True,
                    "Service principal verified and site is reachable. "
                    "Have a user sign in to access files.",
                )

            # OneDrive admin-only: token-only check, since /me/drive needs
            # delegated auth.
            return _result(
                True,
                "Service principal credentials verified. Have a user sign "
                "in with Microsoft to access their OneDrive.",
            )
        except Exception as e:
            return _result(False, str(e))

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """Catalog rows for the files in scope, per the connection's index tier.

        `none` caches nothing (the tool layer lists and reads live); `metadata`
        and `content` both produce one metadata row per file — Graph listings
        carry no extracted text yet, so the two tiers coincide. The row count is
        capped at `max_catalog_objects`.

        `progress_callback` is what makes the run visible: the walk reports the
        folders it is enumerating and this loop reports files indexed, so a
        background indexing job shows real progress instead of a stalled bar.
        """
        if self.index_mode == INDEX_NONE:
            return []
        reporter = make_reporter(progress_callback)
        files = self.list_files(progress_callback=progress_callback)
        truncated = len(files) > self.max_catalog_objects
        if truncated:
            files = files[: self.max_catalog_objects]
            logger.warning(
                "graph_drive: catalog capped at %d file(s) (max_catalog_objects); "
                "narrow the folder scope or include patterns to index more.",
                self.max_catalog_objects,
            )
        reporter.phase("indexing files", total=len(files))
        tables: List[Table] = []
        for f in files:
            reporter.tick(f.get("path") or f.get("name"))
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
    """OneDrive data source — alias with mode preset.

    Defaults `recursive=True` because the user's OneDrive root almost always
    contains folders (Documents, Pictures, etc.) rather than loose files;
    a non-recursive walk returns empty for most users. SharePoint stays
    non-recursive by default since the admin picks a specific folder scope.
    """

    def __init__(self, **kwargs):
        kwargs["mode"] = "onedrive"
        kwargs.setdefault("recursive", True)
        super().__init__(**kwargs)
