"""Shared object-store file source (S3, Google Cloud Storage, Azure Blob).

An object store exposed to the agent as a file catalog, exactly like
`network_dir` but over a bucket/container instead of a mounted filesystem. It
gives the agent the same file primitives — `ls`/`find` (list_files), `cat`
(read_file), line `grep` — plus a **windowed byte-range read** for objects too
large to load whole (logs, ndjson, big CSVs).

Everything is confined to the configured bucket + `prefix`: every file id is a
key *relative* to the prefix, and any id that escapes it (`..`, absolute,
leading slash) is rejected at a single chokepoint (`_resolve_key`).

`ObjectStoreClient` owns everything that does not depend on the provider:
confinement, include-glob scoping, junk filtering, parsing (csv/tsv/xlsx →
DataFrame, pdf/docx/pptx → text), keyword indexing, windowed reads, grep and
the catalog. A provider subclass implements only the storage I/O primitives:

    _iter_list(list_prefix, recursive) -> yields {"prefix": p} | {"key", "size", "modified"}
    _head(key)                         -> {"size", "etag", "modified", "version", ...}
    _download(key, start, end)         -> (bytes, total_size)   (range inclusive)
    _open_body(key, head, offset)      -> readable + closeable body for grep
    _probe()                           -> raises when the scope is unreachable
    _is_not_found(exc)                 -> bool

Declares LIST_FILES + READ_FILE + GREP_FILES. SEARCH_FILES is intentionally
NOT declared — a live content scan over an object store is a GET-per-object;
keyword search runs at the tool layer against the catalog indexed in
`get_schemas()`. GREP_FILES *is* declared because line-grep is explicitly
budgeted (max_files / max_bytes_per_file / time budget, resumable cursor).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import posixpath
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.ai.prompt_formatters import Table
from app.data_sources.clients._document_text import (
    DOC_EXTS,
    doc_text_is_usable,
    doc_text_looks_garbled,
    extract_document_text_from_bytes,
)
from app.data_sources.clients._email_text import EMAIL_EXTS
from app.data_sources.clients._file_source_common import (
    CONTENT_INDEX_MODES,
    EXCEL_EXTS,
    INDEX_FULLTEXT,
    INDEX_NONE,
    UTF8_SLACK,
    DocumentText,
    FileTooLargeError,
    GlobScopeError,
    NamedBytes,
    ScopeEscapeError,
    byte_limit,
    byte_window,
    globs_from_str,
    normalize_index_mode,
    path_matches_globs,
    read_excel_sheet,
)
from app.data_sources.clients._keywords import extract_file_keywords
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, discovery_progress

# Same parse/scan classes as network_dir so behavior matches across file sources.
TABULAR_EXTS = {"csv", "tsv", "xlsx", "xls"}
TEXT_EXTS = {"txt", "md", "json", "html", "htm", "log", "yaml", "yml", "xml", "py", "sql", "ndjson", "jsonl"}
GREPPABLE_EXTS = TABULAR_EXTS | TEXT_EXTS | DOC_EXTS

DEFAULT_WINDOW_BYTES = 1024 * 1024  # 1 MiB default page for windowed reads

logger = logging.getLogger(__name__)


def _ext(name: str) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


class ObjectNotFound(Exception):
    """A provider-neutral 'no such object' raised by `_head` implementations."""


class ObjectStoreClient(DataSourceClient):
    """Provider-neutral object-store file source (bucket/container + prefix)."""

    capabilities = {
        Capability.LIST_FILES,
        Capability.READ_FILE,
        Capability.GREP_FILES,
    }

    # A bounded listing under the prefix is cheap enough to do live per call →
    # list per-connection from the source, not the shared catalog cache.
    cheap_live_listing = True

    # ---- provider identity (overridden per subclass) ----
    #: URI scheme used for web_url / description ("s3", "gs", "az").
    scheme: str = "obj"
    #: Key under which catalog rows carry their file metadata (metadata_json).
    metadata_key: str = "object_store"
    #: Human noun for the bucket in user-facing messages.
    bucket_noun: str = "bucket"

    def __init__(
        self,
        bucket: str | None = None,
        prefix: str | None = None,
        allowed_extensions: str | None = None,
        include_globs: str | None = None,
        recursive: bool = True,
        max_file_mb: int = 100,
        index_content: bool = True,
        index_mode: str | None = None,
        max_catalog_objects: int = 5000,
        max_keywords: int = 50,
    ):
        super().__init__()
        self.bucket = (bucket or "").strip()
        # Normalize prefix to "" or "something/" — a folder-like prefix with a
        # single trailing slash and no leading slash.
        self.prefix = self._norm_prefix(prefix)
        self.allowed_extensions = self._parse_exts(allowed_extensions)
        # Include-globs (relative to the prefix) scope the connection AND act as
        # an access boundary enforced in _resolve_key.
        self.include_globs = globs_from_str(include_globs)
        self.recursive = bool(recursive)
        self.max_file_bytes = int(max_file_mb) * 1024 * 1024 if max_file_mb else None
        self.index_mode = normalize_index_mode(index_mode, index_content_legacy=index_content)
        self.index_content = self.index_mode in CONTENT_INDEX_MODES
        self.max_catalog_objects = int(max_catalog_objects) if max_catalog_objects else 5000
        self.max_keywords = int(max_keywords) if max_keywords else 50

    # ------------------------------------------------- provider primitives

    def _iter_list(self, list_prefix: str, recursive: bool) -> Iterator[dict[str, Any]]:
        """Yield `{"prefix": p}` for sub-folders (non-recursive only) and
        `{"key", "size", "modified"}` for objects under `list_prefix`."""
        raise NotImplementedError

    def _head(self, key: str) -> dict[str, Any]:
        """Object metadata: size, etag, modified, and `version` — an identity
        string that changes whenever the object's bytes change."""
        raise NotImplementedError

    def _download(self, key: str, start: int | None = None, end: int | None = None) -> tuple[bytes, int | None]:
        """Object bytes (whole, or the inclusive [start, end] range) and the
        object's total size when known."""
        raise NotImplementedError

    def _open_body(self, key: str, head: dict[str, Any], offset: int):
        """A readable, closeable body from `offset`, pinned to `head`'s version.
        Raises `FileChanged` when the object no longer matches `head`."""
        raise NotImplementedError

    def _probe(self) -> None:
        """Prove auth + reachability with one bounded call; raise on failure."""
        raise NotImplementedError

    def _is_not_found(self, exc: Exception) -> bool:
        return isinstance(exc, (ObjectNotFound, FileNotFoundError))

    def _web_url(self, key: str) -> str:
        return f"{self.scheme}://{self.bucket}/{key}"

    # ---------------------------------------------------------------- utils

    @staticmethod
    def _norm_prefix(value: str | None) -> str:
        p = (value or "").strip().lstrip("/")
        if not p:
            return ""
        return p if p.endswith("/") else p + "/"

    @staticmethod
    def _parse_exts(value: str | None) -> set | None:
        if not value:
            return None
        items = {e.strip().lower().lstrip(".") for e in value.split(",") if e.strip()}
        return items or None

    def _allowed(self, name: str) -> bool:
        if not self.allowed_extensions:
            return True
        return _ext(name) in self.allowed_extensions

    @staticmethod
    def _is_junk(key: str) -> bool:
        """Keys that shouldn't surface as real files: console 'folder marker'
        objects (a zero-byte key ending in '/'), tooling sidecars
        (`*.metadata`), and OS cruft. Mirrors network_dir._is_junk in spirit."""
        if not key or key.endswith("/"):
            return True
        name = key.rsplit("/", 1)[-1]
        if not name or name.startswith("~$") or name.startswith("."):
            return True
        if name.endswith(".metadata"):
            return True
        return name.lower() in {".ds_store", "thumbs.db", "desktop.ini"}

    def _resolve_key(self, rel_or_id: str, *, enforce_scope: bool = True) -> str:
        """Resolve a file id / relative path to an absolute key INSIDE the
        configured prefix. This is the single security chokepoint: any id that
        escapes the prefix (`..`, absolute, leading slash tricks) is rejected.

        Accepts either a prefix-relative id (the normal case, what list_files
        returns) or a full key already under the prefix.
        """
        raw = (rel_or_id or "").strip()
        if not raw:
            raise ValueError("Empty file id")
        raw = raw.lstrip("/")
        if self.prefix and raw.startswith(self.prefix):
            key = raw
        else:
            key = self.prefix + raw
        # Collapse any '.'/'..' segments, then verify we're still under prefix.
        normalized = posixpath.normpath(key)
        if normalized in (".", "/"):
            normalized = ""
        base = self.prefix.rstrip("/")
        if base:
            if normalized != base and not normalized.startswith(base + "/"):
                raise ScopeEscapeError(f"Key escapes the connection prefix: {rel_or_id}")
        if normalized.startswith("/") or normalized.startswith(".."):
            raise ScopeEscapeError(f"Key escapes the connection prefix: {rel_or_id}")
        # Access boundary: if include-globs are configured, the prefix-relative
        # key must match one — else a read/attach of an in-prefix but off-glob
        # object is denied here (single chokepoint), not merely hidden.
        if enforce_scope and self.include_globs:
            rel = self._rel_id(normalized)
            if not path_matches_globs(rel, self.include_globs):
                raise GlobScopeError(
                    f"'{rel}' is outside this connection's allowed patterns "
                    f"({', '.join(self.include_globs)}). Access denied."
                )
        return normalized

    def _rel_id(self, key: str) -> str:
        """Stable, human-readable file id: key relative to the prefix."""
        if self.prefix and key.startswith(self.prefix):
            return key[len(self.prefix):]
        return key

    def _entry(self, key: str, size: int, modified: datetime | None, is_folder: bool = False) -> dict[str, Any]:
        rel = self._rel_id(key)
        name = rel.rstrip("/").rsplit("/", 1)[-1]
        mime, _ = mimetypes.guess_type(name)
        if modified is not None and modified.tzinfo is None:
            modified = modified.replace(tzinfo=UTC)
        return {
            "id": rel,
            "name": name,
            "path": rel,
            "mime_type": mime,
            "size": int(size or 0),
            "modified_at": (
                modified.astimezone(UTC).isoformat() if modified else None
            ),
            "is_folder": bool(is_folder),
            "web_url": self._web_url(key),
        }

    def _scope_uri(self) -> str:
        return f"{self.scheme}://{self.bucket}/{self.prefix}"

    # ---------------------------------------------------- public capabilities

    def list_files(
        self, folder_id: str | None = None, recursive: bool | None = None,
        *, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._last_list_truncated = False
        rec = self.recursive if recursive is None else bool(recursive)
        # Scope the listing to prefix (+ optional sub-folder id).
        list_prefix = self.prefix
        if folder_id:
            sub = self._resolve_key(folder_id, enforce_scope=False)
            list_prefix = sub if sub.endswith("/") else sub + "/"

        entries: list[dict[str, Any]] = []
        for item in self._iter_list(list_prefix, rec):
            if "prefix" in item:
                if item["prefix"]:
                    entries.append(self._entry(item["prefix"], 0, None, is_folder=True))
                continue
            key = item["key"]
            if self._is_junk(key):
                continue
            if not self._allowed(key.rsplit("/", 1)[-1]):
                continue
            # Scope filter: only prefix-relative keys matching include-globs.
            if self.include_globs and not path_matches_globs(
                self._rel_id(key), self.include_globs
            ):
                continue
            entries.append(self._entry(key, item.get("size", 0), item.get("modified")))
            if limit and len(entries) >= limit:
                self._last_list_truncated = True
                break
        entries.sort(key=lambda e: (not e["is_folder"], e["path"].lower()))
        return entries

    def read_file(
        self,
        file_id: str,
        sheet: str | None = None,
        offset: int | None = None,
        length: int | None = None,
        max_bytes: int | None = None,
        page_range: tuple[int, int] | None = None,
        **_,
    ) -> Any:
        """Read an object.

        - `offset` set → **windowed** raw byte-range read: returns a dict with a
          cursor (next_cursor/total_size/eof) for paging through big objects. No
          parsing, no attachment.
        - `offset` absent → **structured** whole-object read, parsed like
          network_dir (csv/tsv/xlsx → DataFrame, pdf/docx/pptx → text, …), guarded
          by the size cap.
        """
        key = self._resolve_key(file_id)
        if offset is not None:
            return self._read_window(key, int(offset), length)

        # Page-range read for PDFs — same sentinel contract as network_dir.
        if page_range is not None:
            if _ext(key) != "pdf":
                raise ValueError("page_range is supported for PDF files only.")
            data, _size = self._get_bytes(key)
            text, pages_total = _extract_pdf_pages_from_bytes(
                data, key, page_range[0], page_range[1]
            )
            # The bytes this read already downloaded ride along (like
            # DocumentText.raw) so the tool layer doesn't fetch the object a
            # second time for the viewer; `name` carries the extension.
            return {
                "__doc_pages__": True,
                "text": text,
                "pages_total": pages_total,
                "first": max(1, page_range[0]),
                "last": min(page_range[1], pages_total),
                "raw": data,
                "name": key.rsplit("/", 1)[-1],
            }

        # Structured read — pull the whole object (size-capped).
        ext = _ext(key)
        data, size = self._get_bytes(key)

        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(data, key)
            # Near-empty extraction on a rich doc (scanned / image-based / CID
            # font) → return raw bytes so the tool can render it for vision.
            if doc_text_is_usable(text, ext):
                return DocumentText(text, raw=data, name=key)
            return NamedBytes(data, name=key)

        if ext == "csv":
            return pd.read_csv(io.BytesIO(data))
        if ext == "tsv":
            return pd.read_csv(io.BytesIO(data), sep="\t")
        if ext in EXCEL_EXTS:
            return read_excel_sheet(data, sheet, trim=False)
        if ext == "parquet":
            return pd.read_parquet(io.BytesIO(data))
        if ext == "json":
            try:
                return json.loads(data.decode("utf-8", errors="replace"))
            except IndexingCancelled:
                raise
            except Exception:
                return data.decode("utf-8", errors="replace")
        if ext in TEXT_EXTS:
            return data.decode("utf-8", errors="replace")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data

    def file_version(self, file_id: str) -> str | None:
        """ETag (falls back to size:last-modified) as the cache key. `_resolve_key`
        enforces the glob scope, so an off-scope id raises — a cache hit stays
        access-checked. One cheap metadata call, no object download."""
        key = self._resolve_key(file_id, enforce_scope=True)  # raises GlobScopeError off-glob
        try:
            head = self._head(key)
            etag = (head.get("etag") or "").strip('"')
            if etag:
                return etag
            lm = head.get("modified")
            return f"{int(head.get('size') or 0)}:{lm.timestamp() if lm else ''}"
        except IndexingCancelled:
            raise
        except Exception:
            return None

    def _get_bytes(self, key: str, max_bytes: int | None = None) -> tuple[bytes, int]:
        """Fetch a whole object's bytes, enforcing the size cap via a metadata
        call first so we never stream a giant object into memory just to reject
        it. `max_bytes` tightens the connection's own cap for this read."""
        head = self._head(key)
        size = int(head.get("size") or 0)
        limit = byte_limit(self.max_file_bytes, max_bytes)
        if limit and size > limit:
            raise FileTooLargeError(
                f"Object {self._rel_id(key)} is {size / 1024 / 1024:.1f} MB, exceeds the "
                f"{limit / 1024 / 1024:.0f} MB limit. Use a windowed read "
                f"(offset/length) for large objects."
            )
        data, _total = self._download(key)
        return data, size

    def _read_window(self, key: str, offset: int, length: int | None) -> dict[str, Any]:
        """Ranged byte read → a window plus a cursor to page forward.

        Same contract as NetworkDirClient._read_window (byte_window): the
        requested byte range, adjusted by at most 3 bytes at an edge so a
        multi-byte character is never split, with `next_cursor` at the first
        byte not returned. Binary windows are base64-encoded. The read asks for
        UTF8_SLACK bytes past the window so the straddling character can be
        completed without a second round trip.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        window = int(length) if length else DEFAULT_WINDOW_BYTES
        end = offset + window + UTF8_SLACK - 1
        data, total = self._download(key, offset, end)
        if total is None:
            total = offset + len(data)
        return byte_window(data, offset, total, window)

    @contextmanager
    def _open_grep_stream(self, entry, offset, expected_version, check=lambda: None):
        from app.data_sources.clients._streaming_grep import FileChanged, ScanStopped
        key = self._resolve_key(entry["id"])
        head = entry.get("head") or self._head(key)
        version = head["version"]
        if expected_version and expected_version != version:
            raise FileChanged()
        size = int(head["size"])
        is_email = _ext(key) in EMAIL_EXTS
        if offset >= size and not is_email:
            with io.BytesIO(b"") as empty:
                yield empty, version, size
            return
        body = self._open_body(key, head, offset if (offset and not is_email) else 0)
        try:
            if is_email:
                cap = min(self.max_file_bytes or 20_000_000, 20_000_000)
                if size > cap:
                    raise ScanStopped("too_large")
                data = body.read(cap + 1)
                if len(data) > cap:
                    raise ScanStopped("too_large")
                text = extract_document_text_from_bytes(data, key, max_chars=cap).encode("utf-8")
                with io.BytesIO(text) as rendered:
                    rendered.seek(offset)
                    yield rendered, version, len(text)
            else:
                yield body, version, size
        finally:
            body.close()

    def grep_files(
        self,
        pattern: str,
        *,
        is_regex: bool = True,
        ignore_case: bool = False,
        file_ids: list[str] | None = None,
        folder_id: str | None = None,
        name_pattern: str | None = None,
        recursive: bool = True,
        before: int = 0,
        after: int = 0,
        max_matches: int = 100,
        max_matches_per_file: int = 50,
        max_files: int = 500,
        max_bytes_per_file: int | None = None,
        cursor: str | None = None,
        time_budget_seconds: float = 60.0,
        cancel_check=None,
        **_,
    ) -> dict[str, Any]:
        """Line-level grep under the bucket/prefix — same contract as
        network_dir.grep_files (see `_grep_common.run_grep_sweep`).

        Listing is bounded by max_catalog_objects. Each candidate gets a
        metadata call and a streamed read pinned to its version; continuation
        uses a ranged read. Text scan budgets stop with a byte cursor instead of
        rejecting large objects. Email extraction remains size-capped. All
        candidates pass the same _resolve_key confinement check as read_file.
        """
        from app.data_sources.clients._grep_common import (
            DEFAULT_MAX_BYTES_PER_FILE,
            SKIP_ACCESS_DENIED,
            SKIP_NOT_FOUND,
            SKIP_UNREADABLE,
        )

        byte_cap = int(max_bytes_per_file) if max_bytes_per_file else DEFAULT_MAX_BYTES_PER_FILE

        candidates: list[dict[str, Any]] = []
        if file_ids:
            for fid in file_ids:
                try:
                    key = self._resolve_key(fid)
                    head = self._head(key)
                    rel = self._rel_id(key)
                    candidates.append({
                        "id": rel, "path": rel,
                        "size": int(head.get("size") or 0), "head": head,
                    })
                except GlobScopeError:
                    candidates.append({"id": str(fid), "skip_reason": SKIP_ACCESS_DENIED})
                except ValueError:
                    candidates.append({"id": str(fid), "skip_reason": SKIP_NOT_FOUND})
                except IndexingCancelled:
                    raise
                except Exception as e:
                    reason = SKIP_NOT_FOUND if self._is_not_found(e) else SKIP_UNREADABLE
                    candidates.append({"id": str(fid), "skip_reason": reason})
        else:
            import fnmatch
            pat = (name_pattern or "").strip().lower()
            for f in self.list_files(folder_id=folder_id, recursive=recursive, limit=self.max_catalog_objects):
                if f.get("is_folder"):
                    continue
                rel = f["id"]
                if pat and not (
                    fnmatch.fnmatch((f.get("name") or "").lower(), pat)
                    or fnmatch.fnmatch(str(rel).lower(), pat)
                ):
                    continue
                candidates.append({"id": rel, "path": rel, "size": f.get("size")})

        scope_key = "|".join([
            self.metadata_key, self.bucket, self.prefix, str(folder_id or ""), str(name_pattern or ""),
            str(bool(recursive)), ",".join(sorted(str(f) for f in (file_ids or []))),
            pattern, str(bool(is_regex)), str(bool(ignore_case)),
        ])
        from app.data_sources.clients._streaming_grep import run_streaming_grep
        result = run_streaming_grep(
            candidates=candidates,
            open_stream=self._open_grep_stream,
            pattern=pattern,
            is_regex=is_regex,
            ignore_case=ignore_case,
            before=before,
            after=after,
            max_matches=max_matches,
            max_matches_per_file=max_matches_per_file,
            max_files=max_files,
            max_bytes_per_file=byte_cap,
            scope_key=scope_key,
            cursor=cursor,
            time_budget_seconds=time_budget_seconds,
            cancel_check=cancel_check,
        )
        if not file_ids and getattr(self, "_last_list_truncated", False):
            result["coverage_complete"] = False
            if not result["next_cursor"]:
                result["stop_reason"] = "catalog_limit"
                result["truncated"] = True
        return result

    def read_raw_bytes(
        self, file_id: str, *, max_bytes: int | None = None
    ) -> tuple[bytes, str, str | None]:
        """Raw object bytes + name + mime, unparsed — for attach_file, which
        persists the ORIGINAL object rather than a reparsed copy."""
        key = self._resolve_key(file_id)
        data, _size = self._get_bytes(key, max_bytes=max_bytes)
        name = key.rsplit("/", 1)[-1]
        mime, _ = mimetypes.guess_type(name)
        return data, name, mime

    def _file_text(self, key: str, size: int, max_chars: int = 200_000) -> str:
        """Extract plain text from a greppable object for keyword indexing.
        Returns "" for binary/oversized/unreadable — never raises."""
        ext = _ext(key)
        if ext not in GREPPABLE_EXTS:
            return ""
        if ext not in TEXT_EXTS | {"csv", "tsv"} and self.max_file_bytes and size > self.max_file_bytes:
            return ""
        try:
            if ext in TEXT_EXTS | {"csv", "tsv"}:
                if not size:
                    return ""
                data, _ = self._download(key, 0, min(size, max_chars) - 1)
                data = data[:max_chars]
            else:
                data, _ = self._download(key)
            if ext in DOC_EXTS:
                text = extract_document_text_from_bytes(data, key, max_chars=max_chars)
                # Glyph-soup extraction (broken ToUnicode map) → index by
                # name only rather than poisoning keywords with garbage.
                return "" if doc_text_looks_garbled(text) else text
            if ext in ("xlsx", "xls"):
                frames = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
                parts = [f"{name}\n{df.to_csv(index=False, header=False)}" for name, df in frames.items()]
                return "\n".join(parts)[:max_chars]
            return data.decode("utf-8", errors="ignore")[:max_chars]
        except IndexingCancelled:
            raise
        except Exception:
            return ""

    def _file_sections(self, key: str, size: int, name: str):
        """Full-text sections of an object (index tier `fulltext`) plus the
        text sample its keywords are built from.

        The object is written to a temporary file and split by the same code a
        file share uses, so section locations mean the same thing on both.
        Text is fetched up to the full-text cap with a ranged download; other
        formats only when within the connection's size limit. Never raises for
        a bad object — it comes back as `error` with just its name section.
        """
        import os
        import tempfile

        from app.data_sources.clients._fulltext import FileSections, name_section
        from app.data_sources.clients._fulltext_extract import (
            FULLTEXT_MAX_TEXT_BYTES,
            INDEXABLE_EXTS,
            file_sections_from_path,
        )

        ext = _ext(key)
        if ext not in INDEXABLE_EXTS or not size:
            return FileSections(sections=[name_section(name)], status="no_text"), ""
        is_text = ext in TEXT_EXTS | {"csv", "tsv"}
        if not is_text and self.max_file_bytes and size > self.max_file_bytes:
            return FileSections(sections=[name_section(name)], status="partial"), ""
        tmp = None
        try:
            truncated = is_text and size > FULLTEXT_MAX_TEXT_BYTES
            if truncated:
                data, _ = self._download(key, 0, FULLTEXT_MAX_TEXT_BYTES - 1)
                data = data[:FULLTEXT_MAX_TEXT_BYTES]
            else:
                data, _ = self._download(key)
            with tempfile.NamedTemporaryFile(delete=False, suffix="." + ext) as fh:
                tmp = fh.name
                fh.write(data)
            del data
            return file_sections_from_path(
                tmp, name, max_file_bytes=self.max_file_bytes, ext=ext, text_truncated=truncated)
        except IndexingCancelled:
            raise
        except Exception:
            return FileSections(sections=[name_section(name)], status="error"), ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _prior_meta(self, prior_catalog, name: str) -> dict:
        """The previous run's metadata for a catalog row, or {}."""
        entry = (prior_catalog or {}).get(name)
        if isinstance(entry, dict):
            meta = entry.get(self.metadata_key)
            if isinstance(meta, dict):
                return meta
        return {}

    # ---------------------------------------- DataSourceClient compatibility

    @property
    def description(self) -> str:
        scope = self._scope_uri() if self.bucket else "(unconfigured)"
        return f"Object store {scope} (read-only)"

    @property
    def is_document_based(self) -> bool:
        return True

    def connect(self):
        # Nothing to open eagerly — validation happens in test_connection().
        return None

    def test_connection(self) -> dict[str, Any]:
        try:
            if not self.bucket:
                return {"success": False, "message": f"{self.bucket_noun} is required"}
            # A bounded list under prefix proves auth + reachability without
            # enumerating a huge bucket.
            self._probe()
            where = self._scope_uri().rstrip("/")
            return {"success": True, "message": f"Connected — objects visible under {where}"}
        except IndexingCancelled:
            raise
        except Exception as e:
            return {"success": False, "message": str(e)}

    @discovery_progress
    def get_schemas(self, progress_callback=None, prior_catalog=None, section_sink=None) -> list[Table]:
        """Index the bucket/prefix into catalog rows (bounded by
        max_catalog_objects). Honors the index tier: `none` caches nothing
        (live at the tool layer), `metadata` lists without reading contents,
        `content` also extracts keywords + a content hash for topic search,
        `fulltext` also hands every section of every object to `section_sink`
        (the full-text index).

        Both content tiers are INCREMENTAL: an object whose size and
        last-modified time match the previous run (`prior_catalog` for
        keywords, the sink's own record for full text) is not downloaded
        again."""
        if self.index_mode == INDEX_NONE:
            return []
        tables: list[Table] = []
        files = self.list_files(recursive=self.recursive)
        truncated = False
        if len(files) > self.max_catalog_objects:
            files = files[: self.max_catalog_objects]
            truncated = True
        for i, f in enumerate(files):
            if f.get("is_folder"):
                continue
            meta = {
                "file_id": f["id"],
                "mime_type": f.get("mime_type"),
                "size": f.get("size"),
                "modified_at": f.get("modified_at"),
                "web_url": f.get("web_url"),
            }
            description = (
                f"Object '{f['name']}' (type: {f.get('mime_type') or _ext(f['name']) or 'unknown'})."
            )
            row_name = f["path"] or f["name"]
            if self.index_mode == INDEX_FULLTEXT and section_sink is not None:
                meta["index_version"] = 2
                version = f"{f.get('size')}:{f.get('modified_at')}"
                if section_sink.is_current(f["id"], version):
                    stored = section_sink.prior_meta(f["id"])
                    meta["keywords"] = stored.get("keywords") or []
                    meta["content_hash"] = stored.get("content_hash")
                    status = section_sink.prior_status(f["id"]) or "indexed"
                else:
                    try:
                        doc, sample = self._file_sections(
                            self._resolve_key(f["id"]), f.get("size", 0), row_name)
                    except IndexingCancelled:
                        raise
                    except Exception:
                        from app.data_sources.clients._fulltext import FileSections, name_section
                        doc, sample = FileSections(sections=[name_section(row_name)], status="error"), ""
                    meta["keywords"] = extract_file_keywords(sample, f["name"], self.max_keywords)
                    meta["content_hash"] = (
                        hashlib.sha1(sample.encode("utf-8", "ignore")).hexdigest() if sample else None
                    )
                    status = doc.status
                    section_sink.write(f["id"], row_name, version, doc, meta={
                        "keywords": meta["keywords"], "content_hash": meta["content_hash"],
                    })
                meta["indexed"] = status != "error"
                meta["fulltext"] = status
                meta["content_coverage"] = {"indexed": "full", "partial": "partial"}.get(status, "unavailable")
                description += " Full text is indexed." if status == "indexed" else (
                    " Full text is indexed up to a size limit." if status == "partial"
                    else " No extractable text; findable by name only."
                )
                if meta.get("keywords"):
                    description += " Keywords: " + ", ".join(meta["keywords"][:15]) + "."
            elif self.index_content:
                meta["index_version"] = 2
                meta["content_coverage"] = "prefix_sample"
                meta["sample_max_bytes"] = 200_000 if _ext(f["name"]) in TEXT_EXTS | {"csv", "tsv"} else None
                prior = self._prior_meta(prior_catalog, row_name)
                if (
                    prior.get("indexed")
                    and prior.get("index_version") == 2
                    and prior.get("size") == f.get("size")
                    and prior.get("modified_at") == f.get("modified_at")
                ):
                    # Unchanged since the last run — reuse the stored keywords
                    # and hash instead of downloading the object again.
                    meta["keywords"] = prior.get("keywords") or []
                    meta["content_hash"] = prior.get("content_hash")
                    meta["indexed"] = True
                    meta["content_coverage"] = prior.get("content_coverage", "prefix_sample")
                else:
                    try:
                        text = self._file_text(self._resolve_key(f["id"]), f.get("size", 0))
                        meta["keywords"] = extract_file_keywords(text, f["name"], self.max_keywords)
                        meta["content_hash"] = (
                            hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest() if text else None
                        )
                        meta["indexed"] = True
                        if not text:
                            meta["content_coverage"] = "unavailable"
                    except IndexingCancelled:
                        raise
                    except Exception:
                        meta["indexed"] = False
                if meta.get("indexed"):
                    description += " Content keywords use a bounded sample."
                    if meta.get("keywords"):
                        description += " Keywords: " + ", ".join(meta["keywords"][:15]) + "."
            tables.append(Table(
                name=row_name,
                description=description,
                columns=[],
                pks=[],
                fks=[],
                metadata_json={self.metadata_key: meta},
            ))
            if progress_callback:
                try:
                    progress_callback("files", f.get("path") or f.get("name"), i + 1, len(files))
                except IndexingCancelled:
                    raise
                except Exception:
                    pass
        if section_sink is not None and self.index_mode == INDEX_FULLTEXT:
            section_sink.walk_truncated = truncated
            section_sink.flush()
        if truncated:
            logger.warning(
                "%s: catalog truncated to %d objects (max_catalog_objects); "
                "narrow the prefix to index more.", self.metadata_key, self.max_catalog_objects,
            )
        return tables

    def get_schema(self, table_name: str) -> Table | None:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        return None

    def prompt_schema(self) -> str:
        tables = self.get_schemas()
        if not tables:
            return f"No objects available under the configured {self.bucket_noun}/prefix."
        lines = [f"Available objects ({len(tables)}):"]
        for t in tables:
            meta = (t.metadata_json or {}).get(self.metadata_key, {})
            lines.append(f"- {t.name} ({meta.get('mime_type') or 'file'})")
        return "\n".join(lines)

    def execute_query(
        self, query: str | None = None, table_name: str | None = None, **kwargs
    ):
        """Document-based read: `table_name` or `query` names an object to read."""
        if isinstance(query, str) and query.strip().startswith("{"):
            try:
                spec = json.loads(query)
                fid = spec.get("file_id")
                if fid:
                    return self.read_file(
                        fid, sheet=spec.get("sheet"),
                        offset=spec.get("offset"), length=spec.get("length"),
                    )
            except json.JSONDecodeError:
                pass
        if table_name:
            return self.read_file(table_name, sheet=kwargs.get("sheet"))
        if query:
            return self.read_file(query, sheet=kwargs.get("sheet"))
        raise ValueError("Provide table_name or query (file id) to read an object")


class ChunkedBody:
    """Adapt an iterator of byte chunks into the `read(n)` + `close()` body the
    streaming grep consumes, holding at most one chunk of slack in memory."""

    def __init__(self, chunks, on_close=None):
        self._chunks = iter(chunks)
        self._buf = b""
        self._done = False
        self._on_close = on_close
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [self._buf]
            self._buf = b""
            for c in self._chunks:
                parts.append(c)
            self._done = True
            return b"".join(parts)
        while len(self._buf) < size and not self._done:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                self._done = True
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass


def _extract_pdf_pages_from_bytes(data: bytes, key: str, first: int, last: int) -> tuple:
    """Page-range PDF extraction over in-memory object bytes via a temp file
    (the extractor dispatches on filename). Raises on unreadable PDFs — a
    targeted page read must fail loudly, not return ''."""
    import os
    import tempfile

    from app.data_sources.clients._document_text import extract_pdf_pages_text

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
