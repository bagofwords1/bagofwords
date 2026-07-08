"""search_files agent tool — index-first search over a file-based data source.

Two paths:
  1. Fast keyword index (default): match the query against filenames + the
     keywords extracted at index time (stored on the cached catalog row). No
     file re-parsing — instant. Used whenever the connection has been indexed.
  2. Live/deep scan (fallback, or `deep=true`): ask the client to walk the
     directory and grep full file contents — exhaustive but slower. Used when
     the catalog isn't indexed yet, or when the caller forces `deep`.

For providers that don't populate keywords (SharePoint / OneDrive / Drive) the
index path finds nothing and it falls through to the live provider search — so
their behavior is unchanged.
"""
from __future__ import annotations

import re
from typing import Any, AsyncIterator, Dict, List, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, SearchFilesInput, SearchFilesOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import resolve_file_client, resolve_file_data_source

# Keys under a catalog row's metadata_json that may carry a keyword index.
_FILE_METADATA_KEYS = ("network_dir", "graph", "google_drive")
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _index_search(tables, query: str, max_results: int) -> List[Dict[str, Any]]:
    """Match a query against filename + stored keywords on the cached catalog.

    Returns entries sorted by how many query tokens hit, or [] if the catalog
    carries no keyword index (→ caller falls back to a live scan).
    """
    q = (query or "").strip().lower()
    qtokens = set(_WORD_RE.findall(q))
    indexed_any = False
    scored: List[tuple] = []
    for t in (tables or []):
        meta_json = getattr(t, "metadata_json", None) or {}
        sub = next((meta_json.get(k) for k in _FILE_METADATA_KEYS if meta_json.get(k)), {}) or {}
        keywords = sub.get("keywords")
        if keywords is None:
            continue
        indexed_any = True
        name = (t.name or "").lower()
        haystack_tokens = set(k.lower() for k in keywords) | set(_WORD_RE.findall(name))
        # score: full-query substring in name is a strong hit; otherwise count
        # query tokens present in name/keywords.
        score = 0
        if q and q in name:
            score += 5
        score += sum(1 for qt in qtokens if qt in haystack_tokens or qt in name)
        if score > 0:
            scored.append((score, t, sub))
    if not indexed_any:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for _, t, sub in scored[:max_results]:
        out.append(FileEntry(
            id=sub.get("file_id") or t.name,
            name=(t.name.split("/")[-1] if t.name else t.name),
            path=t.name,
            mime_type=sub.get("mime_type"),
            size=sub.get("size"),
            modified_at=sub.get("modified_at"),
            web_url=sub.get("web_url"),
        ).model_dump())
    return out


class SearchFilesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_files",
            description=(
                "Search files by free-text query in a Files & Directories, "
                "SharePoint, OneDrive, or Google Drive connection. Matches "
                "filename AND content — including inside PDF, Word, PowerPoint, "
                "Excel and CSV. Fast: uses the keyword index built when the "
                "connection was indexed. If a term you're sure is in a file "
                "isn't found, retry with deep=true for an exhaustive live scan. "
                "Returns matches with their IDs — pass an ID to read_file or "
                "attach_file."
            ),
            category="research",
            input_schema=SearchFilesInput.model_json_schema(),
            output_schema=SearchFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=30,
            tags=["files", "network_dir", "sharepoint", "onedrive", "drive", "search"],
            requires_capability="search_files",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchFilesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchFilesOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = SearchFilesInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Searching files: {data.query!r}",
            "connection_id": data.connection_id,
        })

        def _fail(msg: str) -> ToolEndEvent:
            return ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id,
                           "query": data.query, "error": msg},
                "observation": {"summary": msg, "success": False},
            })

        # 1. Fast path: keyword index on the cached catalog (unless deep).
        entries: List[Dict[str, Any]] = []
        used_index = False
        if not data.deep:
            data_source, err = await resolve_file_data_source(runtime_ctx, data.connection_id)
            if not err and data_source is not None:
                try:
                    from app.services.data_source_service import DataSourceService
                    tables = await DataSourceService().get_data_source_schema(
                        db=runtime_ctx.get("db"),
                        data_source_id=str(data_source.id),
                        organization=runtime_ctx.get("organization"),
                        current_user=runtime_ctx.get("user"),
                        include_inactive=True,
                    )
                    entries = _index_search(tables, data.query, data.max_results)
                    # indexed_any is signalled by a non-empty catalog carrying
                    # keywords; _index_search returns [] both when unindexed and
                    # when indexed-but-no-match. Distinguish via a re-check.
                    used_index = any(
                        (getattr(t, "metadata_json", None) or {}).get(k, {}).get("keywords") is not None
                        for t in (tables or []) for k in _FILE_METADATA_KEYS
                    )
                except Exception:
                    used_index = False

        # 2. Fall back to a live scan when not indexed, or deep requested, or the
        #    index returned nothing (the term may be deep in a file's body).
        if data.deep or not used_index or not entries:
            client, err = await resolve_file_client(
                runtime_ctx, data.connection_id, Capability.SEARCH_FILES
            )
            if err:
                # If the index gave us something, return that rather than erroring.
                if entries:
                    yield self._done(data, entries, used_index)
                    return
                yield _fail(err)
                return
            try:
                files = await client.asearch_files(data.query)
            except Exception as e:
                if entries:
                    yield self._done(data, entries, used_index)
                    return
                yield _fail(f"search_files failed: {e}")
                return
            files = files[: data.max_results]
            entries = [FileEntry(
                id=f.get("id"), name=f.get("name"),
                path=f.get("path") if isinstance(f.get("path"), str) else None,
                mime_type=f.get("mime_type"), size=f.get("size"),
                modified_at=f.get("modified_at"), web_url=f.get("web_url"),
            ).model_dump() for f in files]
            used_index = False

        yield self._done(data, entries, used_index)

    def _done(self, data, entries, used_index) -> ToolEndEvent:
        how = "keyword index" if used_index else "live scan"
        return ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True,
                "connection_id": data.connection_id,
                "query": data.query,
                "file_count": len(entries),
                "files": entries,
            },
            "observation": {
                "summary": f"Found {len(entries)} file(s) matching '{data.query}' ({how})",
                "success": True,
            },
        })
