"""Object-store file-source client (AWS S3).

Backs the `s3` data source type: an S3 bucket (optionally scoped to a
key prefix) exposed to the agent as a file catalog, exactly like `network_dir`
but over S3 instead of a mounted filesystem. It gives the agent the same file
primitives — `ls`/`find` (list_files), `cat` (read_file) — plus a **windowed
byte-range read** for objects too large to load whole (logs, ndjson, big CSVs).

Everything is confined to the configured `bucket` + `prefix`: every file id is a
key *relative* to the prefix, and any id that escapes it (`..`, absolute, leading
slash) is rejected at a single chokepoint (`_resolve_key`).

Declares LIST_FILES + READ_FILE. SEARCH_FILES is intentionally NOT declared —
a live content scan over an object store is a GET-per-object; keyword search runs
at the tool layer against the catalog indexed in `get_schemas()`. WRITE_FILE is
likewise out of scope for now.

Auth mirrors the Athena client: static keys, keys + STS assume-role, or boto3's
default credential chain. All parsing (csv/tsv/xlsx → DataFrame, pdf/docx/pptx →
extracted text, keyword indexing) is shared with `network_dir` via the same
helpers, so this client only owns S3 I/O + confinement.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import posixpath
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from app.ai.prompt_formatters import Table
from app.data_sources.clients._document_text import DOC_EXTS, doc_text_is_usable, extract_document_text
from app.data_sources.clients._file_source_common import (
    INDEX_CONTENT,
    INDEX_NONE,
    GlobScopeError,
    globs_from_str,
    normalize_index_mode,
    path_matches_globs,
)
from app.data_sources.clients._keywords import extract_keywords
from app.data_sources.clients.base import Capability, DataSourceClient

# Same parse/scan classes as network_dir so behavior matches across file sources.
TABULAR_EXTS = {"csv", "tsv", "xlsx", "xls"}
TEXT_EXTS = {"txt", "md", "json", "html", "htm", "log", "yaml", "yml", "xml", "py", "sql", "ndjson", "jsonl"}
GREPPABLE_EXTS = TABULAR_EXTS | TEXT_EXTS | DOC_EXTS

DEFAULT_WINDOW_BYTES = 1024 * 1024  # 1 MiB default page for windowed reads


def _ext(name: str) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


class S3Client(DataSourceClient):
    """S3-backed file source (bucket + optional prefix)."""

    capabilities = {
        Capability.LIST_FILES,
        Capability.READ_FILE,
    }

    # A bounded list_objects_v2 under the prefix is cheap enough to do live per
    # call → list per-connection from the source, not the shared catalog cache.
    cheap_live_listing = True

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        allowed_extensions: Optional[str] = None,
        include_globs: Optional[str] = None,
        recursive: bool = True,
        max_file_mb: int = 100,
        index_content: bool = True,
        index_mode: Optional[str] = None,
        max_catalog_objects: int = 5000,
        max_keywords: int = 50,
        # Credentials (one variant's worth is present at a time).
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        session_token: Optional[str] = None,
        role_arn: Optional[str] = None,
        **_ignored,
    ):
        super().__init__()
        self.bucket = (bucket or "").strip()
        # Normalize prefix to "" or "something/" — a folder-like prefix with a
        # single trailing slash and no leading slash.
        self.prefix = self._norm_prefix(prefix)
        self.region = (region or "").strip() or None
        self.endpoint_url = (endpoint_url or "").strip() or None
        self.allowed_extensions = self._parse_exts(allowed_extensions)
        # Include-globs (relative to the prefix) scope the connection AND act as
        # an access boundary enforced in _resolve_key.
        self.include_globs = globs_from_str(include_globs)
        self.recursive = bool(recursive)
        self.max_file_bytes = int(max_file_mb) * 1024 * 1024 if max_file_mb else None
        self.index_mode = normalize_index_mode(index_mode, index_content_legacy=index_content)
        self.index_content = self.index_mode == INDEX_CONTENT
        self.max_catalog_objects = int(max_catalog_objects) if max_catalog_objects else 5000
        self.max_keywords = int(max_keywords) if max_keywords else 50

        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.role_arn = role_arn

        self._s3 = None  # lazily constructed boto3 client

    # ---------------------------------------------------------------- utils

    @staticmethod
    def _norm_prefix(value: Optional[str]) -> str:
        p = (value or "").strip().lstrip("/")
        if not p:
            return ""
        return p if p.endswith("/") else p + "/"

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

    @staticmethod
    def _is_junk(key: str) -> bool:
        """Keys that shouldn't surface as real files: console 'folder marker'
        objects (a zero-byte key ending in '/'), Athena/tooling sidecars
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
        """Resolve a file id / relative path to an absolute S3 key INSIDE the
        configured prefix. This is the single security chokepoint: any id that
        escapes the prefix (`..`, absolute, leading slash tricks) is rejected.

        Accepts either a prefix-relative id (the normal case, what list_files
        returns) or a full key already under the prefix.
        """
        raw = (rel_or_id or "").strip()
        if not raw:
            raise ValueError("Empty file id")
        raw = raw.lstrip("/")
        # Accept a full key that already starts with the prefix; otherwise treat
        # the id as relative to the prefix.
        if self.prefix and raw.startswith(self.prefix):
            key = raw
        else:
            key = self.prefix + raw
        # Collapse any '.'/'..' segments, then verify we're still under prefix.
        normalized = posixpath.normpath(key)
        if normalized in (".", "/"):
            normalized = ""
        # normpath strips a trailing slash; prefix comparison uses the base form.
        base = self.prefix.rstrip("/")
        if base:
            if normalized != base and not normalized.startswith(base + "/"):
                raise ValueError(f"Key escapes the connection prefix: {rel_or_id}")
        if normalized.startswith("/") or normalized.startswith(".."):
            raise ValueError(f"Key escapes the connection prefix: {rel_or_id}")
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

    def _entry(self, key: str, size: int, modified: Optional[datetime], is_folder: bool = False) -> Dict[str, Any]:
        rel = self._rel_id(key)
        name = rel.rstrip("/").rsplit("/", 1)[-1]
        mime, _ = mimetypes.guess_type(name)
        return {
            "id": rel,
            "name": name,
            "path": rel,
            "mime_type": mime,
            "size": int(size or 0),
            "modified_at": (
                modified.astimezone(timezone.utc).isoformat() if modified else None
            ),
            "is_folder": bool(is_folder),
            "web_url": f"s3://{self.bucket}/{key}",
        }

    # ------------------------------------------------------------- s3 client

    def _client(self):
        if self._s3 is not None:
            return self._s3
        import boto3

        if self.role_arn:
            # Assume-role: mint short-lived creds via STS, then build a session.
            # Static keys are OPTIONAL here — when absent, the STS client itself
            # resolves via the default chain (instance profile / IRSA / env), so
            # a deployment role can assume the target role with no stored secrets.
            # Mirrors AWSAthenaClient._renew_session.
            sts_kwargs: Dict[str, Any] = {"region_name": self.region}
            if self.access_key and self.secret_key:
                sts_kwargs["aws_access_key_id"] = self.access_key
                sts_kwargs["aws_secret_access_key"] = self.secret_key
            sts = boto3.client("sts", **sts_kwargs)
            resp = sts.assume_role(RoleArn=self.role_arn, RoleSessionName="bow-s3")
            c = resp["Credentials"]
            session = boto3.Session(
                aws_access_key_id=c["AccessKeyId"],
                aws_secret_access_key=c["SecretAccessKey"],
                aws_session_token=c["SessionToken"],
                region_name=self.region,
            )
        elif self.access_key and self.secret_key:
            session = boto3.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                aws_session_token=self.session_token,
                region_name=self.region,
            )
        else:
            # Default credential chain (env, shared config, instance profile, IRSA).
            session = boto3.Session(region_name=self.region)

        self._s3 = session.client("s3", endpoint_url=self.endpoint_url)
        return self._s3

    # ---------------------------------------------------- public capabilities

    def list_files(
        self, folder_id: Optional[str] = None, recursive: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        s3 = self._client()
        rec = self.recursive if recursive is None else bool(recursive)
        # Scope the listing to prefix (+ optional sub-folder id).
        list_prefix = self.prefix
        if folder_id:
            sub = self._resolve_key(folder_id, enforce_scope=False)
            list_prefix = sub if sub.endswith("/") else sub + "/"

        paginator = s3.get_paginator("list_objects_v2")
        kwargs: Dict[str, Any] = {"Bucket": self.bucket, "Prefix": list_prefix}
        if not rec:
            kwargs["Delimiter"] = "/"

        entries: List[Dict[str, Any]] = []
        for page in paginator.paginate(**kwargs):
            # Sub-prefixes become folder entries when not recursing.
            for cp in page.get("CommonPrefixes", []) or []:
                pk = cp.get("Prefix")
                if pk:
                    entries.append(self._entry(pk, 0, None, is_folder=True))
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if self._is_junk(key):
                    continue
                if not self._allowed(key.rsplit("/", 1)[-1]):
                    continue
                # Scope filter: only prefix-relative keys matching include-globs.
                if self.include_globs and not path_matches_globs(
                    self._rel_id(key), self.include_globs
                ):
                    continue
                entries.append(self._entry(key, obj.get("Size", 0), obj.get("LastModified")))
        entries.sort(key=lambda e: (not e["is_folder"], e["path"].lower()))
        return entries

    def read_file(
        self,
        file_id: str,
        sheet: Optional[str] = None,
        offset: Optional[int] = None,
        length: Optional[int] = None,
        max_bytes: Optional[int] = None,
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

        # Structured read — pull the whole object (size-capped).
        ext = _ext(key)
        data, size = self._get_bytes(key)

        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(data, key)
            # Near-empty extraction on a rich doc (scanned / image-based / CID
            # font) → return raw bytes so the tool can render it for vision.
            return text if doc_text_is_usable(text) else data

        if ext == "csv":
            return pd.read_csv(io.BytesIO(data))
        if ext == "tsv":
            return pd.read_csv(io.BytesIO(data), sep="\t")
        if ext in ("xlsx", "xls"):
            return pd.read_excel(io.BytesIO(data), sheet_name=sheet or 0)
        if ext == "parquet":
            return pd.read_parquet(io.BytesIO(data))
        if ext == "json":
            try:
                return json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                return data.decode("utf-8", errors="replace")
        if ext in TEXT_EXTS:
            return data.decode("utf-8", errors="replace")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data

    def _get_bytes(self, key: str) -> Tuple[bytes, int]:
        """Fetch a whole object's bytes, enforcing the size cap via a HEAD first
        so we never stream a giant object into memory just to reject it."""
        s3 = self._client()
        head = s3.head_object(Bucket=self.bucket, Key=key)
        size = int(head.get("ContentLength", 0))
        if self.max_file_bytes and size > self.max_file_bytes:
            raise ValueError(
                f"Object {self._rel_id(key)} is {size / 1024 / 1024:.1f} MB, exceeds the "
                f"{self.max_file_bytes / 1024 / 1024:.0f} MB limit. Use a windowed read "
                f"(offset/length) for large objects."
            )
        obj = s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read(), size

    def _read_window(self, key: str, offset: int, length: Optional[int]) -> Dict[str, Any]:
        """Ranged byte read → a window plus a cursor to page forward.

        Text windows are snapped back to the last complete newline so the agent
        never sees a half-line (unless the window has no newline at all, or we're
        at EOF). Binary windows are returned base64-encoded.
        """
        if offset < 0:
            raise ValueError("offset must be >= 0")
        window = int(length) if length else DEFAULT_WINDOW_BYTES
        s3 = self._client()
        end = offset + window - 1
        obj = s3.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={offset}-{end}")
        data = obj["Body"].read()

        # total size from Content-Range ("bytes 0-199/27221"); fall back to len.
        total = offset + len(data)
        cr = obj.get("ContentRange") or ""
        if "/" in cr:
            try:
                total = int(cr.rsplit("/", 1)[-1])
            except ValueError:
                pass

        raw_end = offset + len(data)
        eof = raw_end >= total

        # Try text; snap to the last newline when not at EOF so paging is clean.
        try:
            text = data.decode("utf-8")
            is_text = True
        except UnicodeDecodeError:
            is_text = False

        if is_text:
            content = text
            next_cursor = raw_end
            if not eof:
                nl = text.rfind("\n")
                if nl != -1 and nl + 1 < len(text):
                    content = text[: nl + 1]
                    next_cursor = offset + len(content.encode("utf-8"))
            return {
                "content": content,
                "encoding": "text",
                "offset": offset,
                "length": len(content.encode("utf-8")),
                "next_cursor": next_cursor,
                "total_size": total,
                "eof": eof,
            }
        return {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
            "offset": offset,
            "length": len(data),
            "next_cursor": raw_end,
            "total_size": total,
            "eof": eof,
        }

    def read_raw_bytes(self, file_id: str) -> Tuple[bytes, str, Optional[str]]:
        """Raw object bytes + name + mime, unparsed — for attach_file, which
        persists the ORIGINAL object rather than a reparsed copy."""
        key = self._resolve_key(file_id)
        data, _size = self._get_bytes(key)
        name = key.rsplit("/", 1)[-1]
        mime, _ = mimetypes.guess_type(name)
        return data, name, mime

    def _file_text(self, key: str, size: int, max_chars: int = 200_000) -> str:
        """Extract plain text from a greppable object for keyword indexing.
        Returns "" for binary/oversized/unreadable — never raises."""
        ext = _ext(key)
        if ext not in GREPPABLE_EXTS:
            return ""
        if self.max_file_bytes and size > self.max_file_bytes:
            return ""
        try:
            s3 = self._client()
            data = s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            if ext in DOC_EXTS:
                return extract_document_text_from_bytes(data, key, max_chars=max_chars)
            if ext in ("xlsx", "xls"):
                frames = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
                parts = [f"{name}\n{df.to_csv(index=False, header=False)}" for name, df in frames.items()]
                return "\n".join(parts)[:max_chars]
            return data.decode("utf-8", errors="ignore")[:max_chars]
        except Exception:
            return ""

    # ---------------------------------------- DataSourceClient compatibility

    @property
    def description(self) -> str:
        scope = f"s3://{self.bucket}/{self.prefix}" if self.bucket else "(unconfigured)"
        return f"Object store {scope} (read-only)"

    @property
    def is_document_based(self) -> bool:
        return True

    def connect(self):
        # Nothing to open eagerly — validation happens in test_connection().
        return None

    def test_connection(self) -> Dict[str, Any]:
        try:
            if not self.bucket:
                return {"success": False, "message": "bucket is required"}
            s3 = self._client()
            # A bounded list under prefix proves auth + reachability without
            # enumerating a huge bucket.
            resp = s3.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
            n = resp.get("KeyCount", 0)
            where = f"s3://{self.bucket}/{self.prefix}".rstrip("/")
            return {"success": True, "message": f"Connected — objects visible under {where}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_schemas(self, progress_callback=None) -> List[Table]:
        """Index the bucket/prefix into catalog rows (bounded by
        max_catalog_objects). Honors the index tier: `none` caches nothing
        (live at the tool layer), `metadata` lists without reading contents,
        `content` also extracts keywords + a content hash for topic search."""
        if self.index_mode == INDEX_NONE:
            return []
        tables: List[Table] = []
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
            if self.index_content:
                try:
                    text = self._file_text(self._resolve_key(f["id"]), f.get("size", 0))
                    meta["keywords"] = extract_keywords(text, f["name"], self.max_keywords)
                    meta["content_hash"] = (
                        hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest() if text else None
                    )
                    meta["indexed"] = True
                    if meta["keywords"]:
                        description += " Keywords: " + ", ".join(meta["keywords"][:15]) + "."
                except Exception:
                    meta["indexed"] = False
            tables.append(Table(
                name=f["path"] or f["name"],
                description=description,
                columns=[],
                pks=[],
                fks=[],
                metadata_json={"s3": meta},
            ))
            if progress_callback:
                try:
                    progress_callback(i + 1, len(files))
                except Exception:
                    pass
        if truncated:
            import logging
            logging.getLogger(__name__).warning(
                "s3: catalog truncated to %d objects (max_catalog_objects); "
                "narrow the prefix to index more.", self.max_catalog_objects,
            )
        return tables

    def get_schema(self, table_name: str) -> Optional[Table]:
        for t in self.get_schemas():
            if t.name == table_name:
                return t
        return None

    def prompt_schema(self) -> str:
        tables = self.get_schemas()
        if not tables:
            return "No objects available under the configured bucket/prefix."
        lines = [f"Available objects ({len(tables)}):"]
        for t in tables:
            meta = (t.metadata_json or {}).get("s3", {})
            lines.append(f"- {t.name} ({meta.get('mime_type') or 'file'})")
        return "\n".join(lines)

    def execute_query(
        self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs
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


def extract_document_text_from_bytes(data: bytes, key: str, max_chars: int = 200_000) -> str:
    """Adapt the path-based document extractor to in-memory S3 bytes by writing
    to a NamedTemporaryFile with the right extension (the extractors dispatch on
    filename). Returns "" on any failure."""
    import os
    import tempfile

    name = key.rsplit("/", 1)[-1]
    suffix = "." + _ext(name) if _ext(name) else ""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as fh:
            fh.write(data)
            tmp = fh.name
        return extract_document_text(tmp, name, max_chars=max_chars) or ""
    except Exception:
        return ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
