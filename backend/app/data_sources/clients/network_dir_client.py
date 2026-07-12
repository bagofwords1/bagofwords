"""Network-directory file-source client.

Backs the `network_dir` data source type: a local folder or an already-mounted
network share (SMB/NFS) exposed to the agent as a file catalog. It gives the
agent the filesystem primitives it would otherwise reach for on a shell —
`ls`/`find` (list_files), `grep` (search_files, filename + content), `cat`
(read_file) — plus, when the connection is writable, `cp`/`put` (write_file).

Everything is confined to the configured `root_path`: every id resolves back to
a path *inside* root, and traversal outside it (`..`, absolute escapes,
symlinks that point out) is rejected. File ids are the POSIX-relative path under
root — stable and human-readable, so the LLM can round-trip them naturally.

Declares LIST_FILES + READ_FILE + SEARCH_FILES always; WRITE_FILE only when the
connection is configured writable (dropped from the *instance* capabilities
otherwise so runtime resolution rejects a write against a read-only dir).
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn
from app.data_sources.clients._document_text import DOC_EXTS, extract_document_text
from app.data_sources.clients._file_source_common import (
    INDEX_CONTENT,
    INDEX_METADATA,
    INDEX_NONE,
    GlobScopeError,
    globs_from_str,
    normalize_index_mode,
    path_matches_globs,
)
from app.data_sources.clients._keywords import extract_keywords
from app.data_sources.clients.base import Capability, DataSourceClient


# Extensions we parse into DataFrames / structured objects; everything else is
# returned as text (if decodable) or raw bytes.
TABULAR_EXTS = {"csv", "tsv", "xlsx", "xls"}
TEXT_EXTS = {"txt", "md", "json", "html", "htm", "log", "yaml", "yml", "xml", "py", "sql"}
# Rich document formats (pdf/docx/pptx) we extract plain text from so their
# contents are readable and searchable — see _document_text.DOC_EXTS.
# Extensions we're willing to scan for content matches in search_files: text,
# tabular, AND documents (extracted). Binary/unknown types are name-matched only.
GREPPABLE_EXTS = TABULAR_EXTS | TEXT_EXTS | DOC_EXTS

DEFAULT_WINDOW_BYTES = 1024 * 1024  # 1 MiB default page for windowed reads


def _ext(name: str) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


class NetworkDirClient(DataSourceClient):
    """Filesystem-backed file source (local path or mounted network share)."""

    # Class-level advertisement: the type *can* write, so the write_file tool is
    # offered in the catalog whenever a network_dir is attached. A read-only
    # instance narrows `self.capabilities` in __init__ so the runtime capability
    # check (resolve_file_client) still rejects writes.
    capabilities = {
        Capability.LIST_FILES,
        Capability.READ_FILE,
        Capability.SEARCH_FILES,
        Capability.WRITE_FILE,
    }

    # Local FS walk is cheap → list files live, per-connection (see base).
    cheap_live_listing = True

    def __init__(
        self,
        root_path: Optional[str] = None,
        allowed_extensions: Optional[str] = None,
        include_globs: Optional[str] = None,
        recursive: bool = True,
        writable: bool = False,
        max_file_mb: int = 100,
        index_content: bool = True,
        index_mode: Optional[str] = None,
        max_catalog_objects: int = 10000,
        max_keywords: int = 50,
        **_ignored,
    ):
        super().__init__()
        self.root_path = (root_path or "").strip()
        self.allowed_extensions = self._parse_exts(allowed_extensions)
        # Include-globs scope the connection to matching files AND act as an
        # access boundary enforced in `_resolve` (a read of a real-but-off-glob
        # file is rejected). Empty = whole directory (still root-confined).
        self.include_globs = globs_from_str(include_globs)
        self.recursive = bool(recursive)
        self.writable = bool(writable)
        self.max_file_bytes = int(max_file_mb) * 1024 * 1024 if max_file_mb else None
        # Effective index tier: none/metadata/content. Falls back to the legacy
        # index_content boolean when index_mode isn't set.
        self.index_mode = normalize_index_mode(index_mode, index_content_legacy=index_content)
        self.index_content = self.index_mode == INDEX_CONTENT
        # Hard cap on how many files we ever enumerate — guards indexing AND
        # live listing from a directory with millions of files (OOM / DB blowup).
        self.max_catalog_objects = int(max_catalog_objects) if max_catalog_objects else 10000
        self.max_keywords = int(max_keywords) if max_keywords else 50

        # Per-instance capability gating: a read-only connection must not expose
        # WRITE_FILE, so the resolve_file_client check blocks write_file calls.
        self.capabilities = {
            Capability.LIST_FILES,
            Capability.READ_FILE,
            Capability.SEARCH_FILES,
        }
        if self.writable:
            self.capabilities = self.capabilities | {Capability.WRITE_FILE}

    # ---------------------------------------------------------------- utils

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

    def _root(self) -> Path:
        if not self.root_path:
            raise ValueError("root_path is required for a network directory connection")
        root = Path(self.root_path).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"Directory does not exist: {self.root_path}")
        if not root.is_dir():
            raise ValueError(f"Path is not a directory: {self.root_path}")
        return root

    def _resolve(
        self, rel_or_id: str, *, must_exist: bool = True, enforce_scope: bool = True
    ) -> Path:
        """Resolve a file id / relative path to an absolute path INSIDE root.

        Rejects any path that escapes root (via `..`, an absolute path, or a
        symlink that points outside). This is the single security chokepoint
        for every read and write.
        """
        root = self._root()
        raw = (rel_or_id or "").strip()
        if not raw:
            raise ValueError("Empty file path")
        # Accept an absolute path only if it's already under root; otherwise
        # treat it as relative to root.
        candidate = Path(raw)
        if candidate.is_absolute():
            target = candidate
        else:
            target = root / raw
        # Resolve symlinks/.. for existing paths; for a not-yet-created write
        # target, resolve the parent and re-append the leaf.
        if target.exists():
            resolved = target.resolve()
        else:
            resolved = target.parent.resolve() / target.name
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            raise ValueError(f"Path escapes the connection root: {rel_or_id}")
        # Access boundary: if include-globs are configured, the resolved path
        # (relative to root) must match one — otherwise a read/attach of a
        # real-but-out-of-scope file (e.g. a `.env` next to the decks) is
        # rejected here, at the single chokepoint, not just hidden from listing.
        if enforce_scope and self.include_globs and not path_matches_globs(
            rel.as_posix(), self.include_globs
        ):
            raise GlobScopeError(
                f"'{rel.as_posix()}' is outside this connection's allowed patterns "
                f"({', '.join(self.include_globs)}). Access denied."
            )
        if must_exist and not resolved.exists():
            raise ValueError(f"File not found: {rel_or_id}")
        return resolved

    def _rel_id(self, path: Path) -> str:
        """Stable, human-readable file id: POSIX-relative path under root."""
        return path.relative_to(self._root()).as_posix()

    def _entry(self, path: Path) -> Dict[str, Any]:
        stat = path.stat()
        rel = self._rel_id(path)
        mime, _ = mimetypes.guess_type(path.name)
        return {
            "id": rel,
            "name": path.name,
            "path": rel,
            "mime_type": mime,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "is_folder": False,
            "web_url": path.as_uri(),
        }

    @staticmethod
    def _is_junk(name: str) -> bool:
        """OS/Office cruft that shouldn't appear as real files: Word/Excel lock
        stubs (`~$…`), macOS `.DS_Store`, Windows `Thumbs.db`, and hidden
        dotfiles. These otherwise pollute the catalog and make the doc
        extractors choke ("File is not a zip file")."""
        if not name:
            return True
        if name.startswith("~$") or name.startswith("."):
            return True
        return name.lower() in {".ds_store", "thumbs.db", "desktop.ini"}

    #: set True by the most recent _iter_files call when it hit the cap.
    _last_walk_truncated: bool = False

    def _iter_files(self, base: Path, recursive: bool, limit: Optional[int] = None) -> List[Path]:
        walker = base.rglob("*") if recursive else base.glob("*")
        root = self._root()
        cap = limit if limit is not None else self.max_catalog_objects
        out: List[Path] = []
        self._last_walk_truncated = False
        for p in walker:
            if not p.is_file():
                continue
            if self._is_junk(p.name):
                continue
            if not self._allowed(p.name):
                continue
            # Scope filter: only files matching the include-globs are visible.
            if self.include_globs:
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    continue
                if not path_matches_globs(rel, self.include_globs):
                    continue
            out.append(p)
            # Stop early rather than materialize millions of paths — the walk
            # itself is bounded, so a huge directory can't OOM us.
            if cap and len(out) >= cap:
                self._last_walk_truncated = True
                break
        return out

    # ---------------------------------------------------- public capabilities

    def list_files(
        self, folder_id: Optional[str] = None, recursive: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        root = self._root()
        # A folder is a container, not a glob-matched file — don't scope-enforce
        # the folder path itself; the files inside are filtered by _iter_files.
        base = self._resolve(folder_id, enforce_scope=False) if folder_id else root
        if not base.is_dir():
            raise ValueError(f"Not a folder: {folder_id}")
        rec = self.recursive if recursive is None else bool(recursive)
        files = self._iter_files(base, rec)
        files.sort(key=lambda p: p.as_posix().lower())
        return [self._entry(p) for p in files]

    def read_file(
        self,
        file_id: str,
        sheet: Optional[str] = None,
        offset: Optional[int] = None,
        length: Optional[int] = None,
        max_bytes: Optional[int] = None,
        **_,
    ) -> Any:
        path = self._resolve(file_id, must_exist=True)
        if not path.is_file():
            raise ValueError(f"Not a file: {file_id}")

        # Windowed (ranged) read — same contract as S3's _read_window: a raw
        # byte window plus a cursor (next_cursor/total_size/eof) to page through
        # arbitrarily large files. No parsing, no attachment. Text windows snap
        # to the last complete newline so paging never splits a line.
        if offset is not None:
            return self._read_window(path, int(offset), length)

        # Rich documents (pdf/docx/pptx): return extracted plain text so the
        # agent can actually read them instead of receiving opaque bytes.
        if _ext(path.name) in DOC_EXTS:
            if self.max_file_bytes and path.stat().st_size > self.max_file_bytes:
                raise ValueError(
                    f"File {file_id} is {path.stat().st_size / 1024 / 1024:.1f} MB, "
                    f"exceeds the {self.max_file_bytes / 1024 / 1024:.0f} MB limit."
                )
            text = extract_document_text(str(path), path.name)
            # Fall back to raw bytes if extraction yielded nothing (scanned PDF,
            # unusual encoding) so the caller can still attach/inspect the file.
            return text if text else path.read_bytes()

        cap = max_bytes or self.max_file_bytes
        size = path.stat().st_size
        if cap and size > cap and _ext(path.name) not in TABULAR_EXTS:
            # For non-tabular files honour the byte cap by slicing; tabular
            # files are parsed whole (pandas needs a complete file) but still
            # guarded by max_file_bytes below.
            with open(path, "rb") as fh:
                content = fh.read(cap)
        else:
            if self.max_file_bytes and size > self.max_file_bytes:
                raise ValueError(
                    f"File {file_id} is {size / 1024 / 1024:.1f} MB, exceeds the "
                    f"{self.max_file_bytes / 1024 / 1024:.0f} MB limit."
                )
            content = path.read_bytes()

        ext = _ext(path.name)
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
        # Unknown type: return text if it decodes cleanly, else raw bytes.
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content

    def _read_window(self, path: Path, offset: int, length: Optional[int]) -> Dict[str, Any]:
        """Ranged byte read of a local file → a window plus a cursor to page
        forward. Mirrors S3Client._read_window so the agent sees ONE contract
        across file sources: text windows snap back to the last complete newline
        (unless at EOF or the window has no newline); binary windows are base64.
        """
        import base64 as _b64

        if offset < 0:
            raise ValueError("offset must be >= 0")
        window = int(length) if length else DEFAULT_WINDOW_BYTES
        total = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(window)

        raw_end = offset + len(data)
        eof = raw_end >= total

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
                "next_cursor": None if eof else next_cursor,
                "total_size": total,
                "eof": eof,
            }
        return {
            "content": _b64.b64encode(data).decode("ascii"),
            "encoding": "base64",
            "offset": offset,
            "length": len(data),
            "next_cursor": None if eof else raw_end,
            "total_size": total,
            "eof": eof,
        }

    def _file_text(self, path: Path, max_chars: int = 200_000) -> str:
        """Extract plain text from a greppable file (doc/csv/tsv/text) for
        indexing and live search. Returns "" for binary/oversized/unreadable —
        never raises. Excel is flattened to its cell values as text."""
        ext = _ext(path.name)
        if ext not in GREPPABLE_EXTS:
            return ""
        try:
            if self.max_file_bytes and path.stat().st_size > self.max_file_bytes:
                return ""
            if ext in DOC_EXTS:
                return extract_document_text(str(path), path.name, max_chars=max_chars)
            if ext in ("xlsx", "xls"):
                frames = pd.read_excel(path, sheet_name=None, header=None)
                # Include sheet names — they're often meaningful labels
                # ("headcount", "budget") that don't appear in any cell.
                parts = [f"{name}\n{df.to_csv(index=False, header=False)}"
                         for name, df in frames.items()]
                return "\n".join(parts)[:max_chars]
            # csv / tsv / plain text
            return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
        except Exception:
            return ""

    def read_raw_bytes(self, file_id: str) -> Tuple[bytes, str, Optional[str]]:
        """Return the file's raw bytes + name + mime, unparsed. Used by the
        attach_file tool to persist the ORIGINAL file (a real .pdf/.xlsx) rather
        than a serialized/reparsed copy."""
        path = self._resolve(file_id, must_exist=True)
        if not path.is_file():
            raise ValueError(f"Not a file: {file_id}")
        if self.max_file_bytes and path.stat().st_size > self.max_file_bytes:
            raise ValueError(
                f"File {file_id} exceeds the "
                f"{self.max_file_bytes / 1024 / 1024:.0f} MB limit."
            )
        mime, _ = mimetypes.guess_type(path.name)
        return path.read_bytes(), path.name, mime

    def search_files(
        self, query: str, max_results: int = 200, content: bool = True, **_
    ) -> List[Dict[str, Any]]:
        """Live filename + content search under root (the exhaustive fallback).

        A file matches if the (case-insensitive) query is a substring of its
        relative path, OR appears in its extracted content. Mirrors `grep -ril`
        + a filename check. Fast keyword search runs at the tool layer against
        the indexed catalog; this is what the tool falls back to for the
        not-yet-indexed / deep case.
        """
        root = self._root()
        q = (query or "").strip().lower()
        if not q:
            return []
        results: List[Dict[str, Any]] = []
        for p in self._iter_files(root, recursive=True):
            rel = self._rel_id(p).lower()
            matched = q in rel
            if not matched and content:
                matched = q in self._file_text(p).lower()
            if matched:
                results.append(self._entry(p))
                if len(results) >= max_results:
                    break
        return results

    def write_file(
        self,
        filename: str,
        content: Any,
        folder_id: Optional[str] = None,
        overwrite: bool = False,
        **_,
    ) -> Dict[str, Any]:
        if not self.writable:
            raise ValueError(
                "This network directory connection is read-only. Enable 'Allow "
                "Writes' on the connection to permit write_file."
            )
        if not filename or not filename.strip():
            raise ValueError("filename is required")

        # Compose the destination relative path (folder_id is a folder under root).
        rel = filename.strip().lstrip("/")
        if folder_id:
            rel = f"{folder_id.strip().strip('/')}/{rel}"
        target = self._resolve(rel, must_exist=False)
        if not self._allowed(target.name):
            raise ValueError(
                f"Extension not allowed by this connection: {target.name}"
            )
        if target.exists() and not overwrite:
            raise ValueError(
                f"File already exists: {self._rel_id(target)} (pass overwrite=true to replace)"
            )
        if target.is_dir():
            raise ValueError(f"Target is a directory: {filename}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray)):
            data = bytes(content)
        else:
            # Fall back to a JSON/text serialization for structured payloads.
            data = json.dumps(content, default=str, ensure_ascii=False).encode("utf-8")
        if self.max_file_bytes and len(data) > self.max_file_bytes:
            raise ValueError(
                f"Refusing to write {len(data) / 1024 / 1024:.1f} MB — exceeds the "
                f"{self.max_file_bytes / 1024 / 1024:.0f} MB limit."
            )
        target.write_bytes(data)
        return self._entry(target)

    def copy_file(self, file_id: str, dest: str, overwrite: bool = False) -> Dict[str, Any]:
        """Convenience `cp` within the connection root: read a file's bytes and
        write them to a new relative path. Kept thin — write_file does the
        safety/overwrite checks."""
        src = self._resolve(file_id, must_exist=True)
        return self.write_file(dest, src.read_bytes(), overwrite=overwrite)

    # ---------------------------------------- DataSourceClient compatibility

    @property
    def description(self) -> str:
        scope = self.root_path or "(unconfigured)"
        mode = "read/write" if self.writable else "read-only"
        return f"Network directory {scope} ({mode})"

    @property
    def is_document_based(self) -> bool:
        return True

    def connect(self):
        # Nothing to open — filesystem access is on-demand. Validation happens
        # in test_connection() / on first access, so a not-yet-mounted share
        # surfaces a clean error there rather than crashing construction.
        return None

    def test_connection(self) -> Dict[str, Any]:
        try:
            root = self._root()
            if not os.access(root, os.R_OK):
                return {"success": False, "message": f"Directory not readable: {self.root_path}"}
            if self.writable and not os.access(root, os.W_OK):
                return {
                    "success": False,
                    "message": f"'Allow Writes' is on but the directory is not writable: {self.root_path}",
                }
            n = len(self.list_files(recursive=self.recursive))
            return {"success": True, "message": f"Connected — {n} file(s) visible in {self.root_path}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_schemas(self, progress_callback=None) -> List[Table]:
        """Index the directory into catalog rows per the connection's index tier:

        - `none`     → no catalog at all (listing/search go live at the tool
                       layer). Returns [].
        - `metadata` → one row per file with name/size/mtime, NO content read.
        - `content`  → metadata + extracted keywords/hash so search_files can
                       match by topic without re-parsing every file.
        """
        if self.index_mode == INDEX_NONE:
            # Live mode: nothing is cached; the tool layer lists/reads directly.
            return []
        root = self._root()
        tables: List[Table] = []
        for f in self.list_files(recursive=self.recursive):
            meta = {
                "file_id": f["id"],
                "mime_type": f.get("mime_type"),
                "size": f.get("size"),
                "modified_at": f.get("modified_at"),
                "web_url": f.get("web_url"),
            }
            description = (
                f"File '{f['name']}' (type: {f.get('mime_type') or _ext(f['name']) or 'unknown'})."
            )
            if self.index_content:
                try:
                    path = root / f["id"]
                    text = self._file_text(path)
                    meta["keywords"] = extract_keywords(text, f["name"], self.max_keywords)
                    meta["content_hash"] = hashlib.sha1(
                        (text or "").encode("utf-8", "ignore")
                    ).hexdigest() if text else None
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
                metadata_json={"network_dir": meta},
            ))
        if self._last_walk_truncated:
            import logging
            logging.getLogger(__name__).warning(
                "network_dir: catalog capped at %d files (max_catalog_objects); "
                "narrow the include-patterns to index more of %s.",
                self.max_catalog_objects, self.root_path,
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
            return "No files available in the configured directory."
        lines = [f"Available files ({len(tables)}):"]
        for t in tables:
            meta = (t.metadata_json or {}).get("network_dir", {})
            lines.append(f"- {t.name} ({meta.get('mime_type') or 'file'})")
        return "\n".join(lines)

    def execute_query(
        self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs
    ):
        """Document-based read: `table_name` or `query` names a file to read."""
        if isinstance(query, str) and query.strip().startswith("{"):
            try:
                spec = json.loads(query)
                fid = spec.get("file_id")
                if fid:
                    return self.read_file(fid, sheet=spec.get("sheet"), max_bytes=spec.get("max_bytes"))
            except json.JSONDecodeError:
                pass
        if table_name:
            return self.read_file(table_name, sheet=kwargs.get("sheet"))
        if query:
            return self.read_file(query, sheet=kwargs.get("sheet"))
        raise ValueError("Provide table_name or query (file id) to read a file")
