"""list_files agent tool — list a file source's files.

Two paths, chosen by the connection's index tier:
  - index_mode == "none"  → **live** listing straight from the source
    (network_dir walk / S3 list_objects). Always fresh; no catalog needed.
  - otherwise             → the persisted catalog (DataSourceTable +
    per-user UserOverlayTable), refreshed on schedule/refresh. Fast, no
    per-call source hit. Falls back to live if the cache is empty.

Both paths respect the connection's include-globs (the client filters live;
the catalog only ever held glob-matched files).
"""
from __future__ import annotations

import fnmatch
from typing import Any, AsyncIterator, Dict, List, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, ListFilesInput, ListFilesOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import resolve_file_client, resolve_file_data_source

_MAX_RESULTS = 500

# Connector-specific metadata keys on the cached Table's metadata_json.
_FILE_METADATA_KEYS = ("graph", "google_drive", "network_dir", "s3")


class ListFilesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_files",
            description=(
                "List files in a file connection (SharePoint / OneDrive / "
                "Google Drive / network directory / S3). For live connections "
                "this reads the source directly (always current); otherwise it "
                "reads the fast cached catalog. Returns up to 500 files. Only "
                "files matching the connection's configured patterns are "
                "visible. Filter by filename with name_pattern (glob: '*.xlsx')."
            ),
            category="research",
            input_schema=ListFilesInput.model_json_schema(),
            output_schema=ListFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=30,
            tags=["files", "sharepoint", "onedrive", "drive", "network_dir", "s3", "list"],
            requires_capability="list_files",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListFilesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListFilesOutput

    def _end(self, connection_id: str, entries: List[dict], truncated: bool, source: str, hint: str = "") -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True,
                "connection_id": connection_id,
                "file_count": len(entries),
                "files": entries,
                "truncated": truncated,
            },
            "observation": {
                "summary": (
                    f"Listed {len(entries)} file(s) ({source})"
                    + (f" (capped at {_MAX_RESULTS})" if truncated else "")
                    + hint
                ),
                "success": True,
            },
        })

    def _fail(self, connection_id: str, err: str) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={
            "output": {"success": False, "connection_id": connection_id, "error": err},
            "observation": {"summary": err, "success": False},
        })

    def _apply(self, raw: List[dict], name_pattern) -> tuple:
        files = []
        for f in raw:
            name = f.get("name") or f.get("path") or f.get("id")
            if name_pattern and not fnmatch.fnmatch(str(name).lower(), name_pattern.lower()):
                continue
            files.append({
                "id": f.get("id") or f.get("file_id") or name,
                "name": name,
                "path": f.get("path") or name,
                "mime_type": f.get("mime_type"),
                "size": f.get("size"),
                "modified_at": f.get("modified_at"),
                "web_url": f.get("web_url"),
            })
        truncated = len(files) > _MAX_RESULTS
        if truncated:
            files = files[:_MAX_RESULTS]
        return [FileEntry(**f).model_dump() for f in files], truncated

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = ListFilesInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": "Listing files",
            "connection_id": data.connection_id,
        })

        # Resolve the client too. For sources where live listing is cheap
        # (network_dir, S3), list live from the per-connection client: it's
        # fresher AND correctly scoped to THIS connection, whereas the cache is
        # per-data-source and unions every connection's files together.
        client, _client_err = await resolve_file_client(
            runtime_ctx, data.connection_id, Capability.LIST_FILES
        )
        # Per-user OAuth sources (SharePoint/OneDrive/Drive) have no cheap live
        # listing, but their catalog is either not centrally indexed (per_user)
        # or ACL-shared — so serving a cache would risk showing another
        # identity's files. List them LIVE with the current user's client
        # instead, so the browse always reflects the querying user's account.
        conn_obj = getattr(client, "_bow_connection", None) if client is not None else None
        per_user_live = bool(
            conn_obj is not None
            and getattr(conn_obj, "auth_policy", None) == "user_required"
            and "oauth" in (getattr(conn_obj, "allowed_user_auth_modes", None) or [])
        )
        live_listing = bool(client is not None and (
            getattr(client, "cheap_live_listing", False) or per_user_live
        ))

        async def _live() -> tuple:
            if data.folder_id:
                raw = await client.alist_files(folder_id=data.folder_id, recursive=bool(data.recursive))
            else:
                # No folder given: list the WHOLE scope recursively. The include-
                # globs already bound the result, and for nested layouts (S3
                # prefixes, subfoldered shares) a non-recursive root listing
                # would surface only folders and read as "empty".
                raw = await client.alist_files(recursive=True)
            return self._apply([e for e in raw if not e.get("is_folder")], data.name_pattern)

        if live_listing:
            try:
                entries, truncated = await _live()
                yield self._end(data.connection_id, entries, truncated, "live")
                return
            except Exception as e:
                yield self._fail(data.connection_id, f"live list_files failed: {e}")
                return

        # --- Cache path ---
        data_source, err = await resolve_file_data_source(runtime_ctx, data.connection_id)
        if err:
            # No cached data source but we may still have a live client.
            if client is not None:
                try:
                    entries, truncated = await _live()
                    yield self._end(data.connection_id, entries, truncated, "live")
                    return
                except Exception as e:
                    yield self._fail(data.connection_id, f"live list_files failed: {e}")
                    return
            yield self._fail(data.connection_id, err)
            return

        try:
            from app.services.data_source_service import DataSourceService
            tables = await DataSourceService().get_data_source_schema(
                db=runtime_ctx.get("db"),
                data_source_id=str(data_source.id),
                organization=runtime_ctx.get("organization"),
                current_user=runtime_ctx.get("user"),
                include_inactive=True,
            )
        except Exception as e:
            yield self._fail(data.connection_id, f"Failed to read cached catalog: {e}")
            return

        raw = []
        for t in (tables or []):
            meta_json = getattr(t, "metadata_json", None) or {}
            sub = next((meta_json.get(k) for k in _FILE_METADATA_KEYS if meta_json.get(k)), {}) or {}
            raw.append({
                "id": sub.get("file_id") or t.name,
                "name": t.name,
                "path": t.name,
                "mime_type": sub.get("mime_type"),
                "size": sub.get("size"),
                "modified_at": sub.get("modified_at"),
                "web_url": sub.get("web_url"),
            })
        entries, truncated = self._apply(raw, data.name_pattern)

        # Empty cache (e.g. not indexed yet) but a live client is available →
        # fall back to a live listing instead of returning nothing.
        if not entries and client is not None:
            try:
                entries, truncated = await _live()
                yield self._end(data.connection_id, entries, truncated, "live (cache empty)")
                return
            except Exception:
                pass

        hint = "" if entries else " Catalog is empty — try search_files or run a refresh."
        yield self._end(data.connection_id, entries, truncated, "cache", hint)
