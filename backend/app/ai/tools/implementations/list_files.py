"""list_files agent tool — list a file source's files.

Paths, in the order they are tried:
  - **catalog** (per-connection ConnectionTable rows): the default for
    SharePoint (both Graph and SharePoint Server) when the connection runs as a
    service account and has been indexed. Instant, supports folder/glob/date
    filters, sorting and cursor paging; as fresh as the last index run, which
    the result reports as `indexed_at`. `source="catalog"` forces it (and
    fails when there is none); `source="live"` skips it. Per-user connections
    never list from the shared catalog.
  - **live**: SharePoint's bounded enumeration, or the cheap walk/LIST of
    network_dir and S3 — the default for those two so a just-written file is
    visible immediately; pass `source="catalog"` for a large slow share.
  - the per-data-source cache (DataSourceTable + per-user overlay) for the
    remaining connectors, falling back to live when it is empty.

`group_by="folder"` lists folders (with file counts and dates) by grouping
the catalog in memory; it needs the catalog and never falls back to live.

All paths respect the connection's include-globs (the client filters live;
the catalog only ever held glob-matched files).
"""
from __future__ import annotations

import fnmatch
from typing import Any, AsyncIterator, Dict, List, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, FolderEntry, ListFilesInput, ListFilesOutput
from app.data_sources.clients._sharepoint_listing import SharepointBoundedListing
from app.data_sources.clients.base import Capability

from ._file_catalog import (
    FILE_METADATA_KEYS,
    CursorError,
    entries_from_tables,
    group_catalog_folders,
    page_catalog,
)
from ._file_tool_common import resolve_file_client, resolve_file_data_source

_MAX_RESULTS = 500

# Connector-specific metadata keys on the cached Table's metadata_json.
_FILE_METADATA_KEYS = FILE_METADATA_KEYS


def _scoped_listing(data: Any) -> bool:
    """A folder-scoped listing or a cursor page is a step of a walk, not a
    re-enumeration of the connection — the repeat-enumeration note must not
    tell the model to stop (folder-by-folder work lists every folder once)."""
    return bool((getattr(data, "folder_id", None) or "").strip().strip("/") or getattr(data, "cursor", None))


class ListFilesTool(Tool):
    # Capability the resolved connection must expose. Overridden by ListEmailsTool
    # so the same listing path backs a mailbox (LIST_EMAILS).
    _required_capability = Capability.LIST_FILES
    _item_noun = "file"
    _start_title = "Listing files"
    _operation_name = "list_files"
    _empty_hint_action = "search_files"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_files",
            description=(
                "List files in a file connection (SharePoint / OneDrive / "
                "Google Drive / network directory / S3). SharePoint lists from its "
                "indexed catalog by default (instant; the result's indexed_at says how "
                "fresh; page with cursor, sort with sort_by) and from the source only "
                "with source='live' or when no catalog exists. Network directories and "
                "S3 read the source directly unless source='catalog'. Only SharePoint "
                "uses limit (default 50; use 1 for any one file) and honors "
                "recursive=false; set recursive=true to include nested files. Only "
                "files matching the connection's configured patterns are "
                "visible. Filter with name_pattern (glob, matched against the filename "
                "and the relative path: '*.xlsx', '*6044534*'). SharePoint also honors "
                "created_after/created_before/modified_after/modified_before (ISO-8601) "
                "and returns created_at alongside modified_at. For 'one record per "
                "folder' tasks, first list the folders with group_by='folder' (catalog "
                "only; depth=1 for top-level folders): one call returns every folder with "
                "its file count and dates. Then work folder by folder, passing each "
                "folder's path as folder_id with recursive=true."
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

    #: Max inventory rows rendered into the model-facing observation. The
    #: planner consumes the OBSERVATION, not the output — without the names
    #: and ids here the model re-lists blindly until the repeated-call breaker
    #: kills the turn (observed live). History compaction collapses this once
    #: superseded, so only the latest listing pays the cost.
    _OBS_INVENTORY_MAX_ROWS = 50

    #: Folder mode: rows per page when no limit is given, and rows rendered
    #: into the observation. One short line each, so the planner sees the
    #: whole folder set of a large library in a single call.
    _OBS_FOLDER_MAX_ROWS = 300

    def _inventory_row(self, e: dict) -> str:
        """One line of the model-facing inventory. Overridden by ListEmailsTool
        so a mailbox listing carries sender + received date — the fields its
        tool description promises — instead of only a subject and an opaque id,
        which forced a read_email per message just to learn who sent it."""
        bits = [str(e.get("name") or "?")]
        if e.get("path") and e.get("path") != e.get("name"):
            bits.append(str(e["path"]))
        if e.get("size") is not None:
            bits.append(f"{e['size']} B")
        # Timestamps belong in the row: the planner reads this observation,
        # not the output, and without them it re-lists live "to get dates".
        if e.get("modified_at"):
            bits.append(f"modified {e['modified_at']}")
        if e.get("created_at"):
            bits.append(f"created {e['created_at']}")
        return " — ".join(bits) + f" [id={e.get('id')}]"

    def _end(self, connection_id: str, entries: List[dict], truncated: bool, source: str, hint: str = "", runtime_ctx: Dict[str, Any] = None, scoped: bool = False) -> ToolEndEvent:
        # Repeat-enumeration guard: rephrased re-listing of an already-listed
        # connection gets a result-time nudge to use the note/inventory instead.
        enum_hint = ""
        if runtime_ctx is not None:
            from app.ai.tools.implementations._file_tool_common import note_enumeration
            n = note_enumeration(runtime_ctx, connection_id, len(entries), scoped=scoped)
            if n:
                enum_hint = f" {n}"
        observation = {
            "summary": (
                f"Listed {len(entries)} {self._item_noun}(s) ({source})"
                + (f" (capped at {_MAX_RESULTS})" if truncated else "")
                + hint
                + enum_hint
            ),
            "success": True,
        }
        if entries:
            rows = [
                self._inventory_row(e) for e in entries[: self._OBS_INVENTORY_MAX_ROWS]
            ]
            if len(entries) > self._OBS_INVENTORY_MAX_ROWS:
                rows.append(
                    f"… +{len(entries) - self._OBS_INVENTORY_MAX_ROWS} more — "
                    "narrow with name_pattern or folder_id to see the rest."
                )
            observation["details"] = "\n".join(rows)
        return ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True,
                "connection_id": connection_id,
                "file_count": len(entries),
                "files": entries,
                "truncated": truncated,
            },
            "observation": observation,
        })

    def _fail(self, connection_id: str, err: str) -> ToolEndEvent:
        return ToolEndEvent(type="tool.end", payload={
            "output": {"success": False, "connection_id": connection_id, "error": err},
            "observation": {"summary": err, "success": False},
        })

    # ------------------------------------------------------------ catalog path

    def _catalog_by_default(self, client) -> bool:
        """SharePoint (Graph and Server) prefers the catalog; connectors whose
        live listing is cheap (network_dir, S3) stay live unless asked, so a
        file the agent just wrote is visible without a reindex."""
        return isinstance(client, SharepointBoundedListing) or not getattr(client, "cheap_live_listing", False)

    async def _load_catalog(self, runtime_ctx: Dict[str, Any], client) -> tuple:
        """The per-connection catalog usable for THIS caller, or (None, reason).

        Usable means: a file connection running as a service account
        (system_only — a per-user catalog would be one identity's view),
        indexing enabled, and at least one indexed file row. Returns
        (catalog, reason) where catalog = {entries, indexed_at, total, key}.
        """
        if self._required_capability != Capability.LIST_FILES:
            return None, "not a file connection"
        conn = getattr(client, "_bow_connection", None) if client is not None else None
        db = runtime_ctx.get("db")
        if conn is None or db is None:
            return None, "no connection context"
        if getattr(conn, "auth_policy", None) != "system_only":
            return None, "this connection runs as each user, so files are listed live under your identity"
        cfg = getattr(conn, "config", None) or {}
        if isinstance(cfg, str):
            import json as _json
            try:
                cfg = _json.loads(cfg or "{}")
            except Exception:
                cfg = {}
        if str((cfg or {}).get("index_mode") or "metadata") == "none":
            return None, "indexing is disabled on this connection (index_mode=none)"
        try:
            from sqlalchemy import select

            from app.models.connection_indexing import ConnectionIndexing, ConnectionIndexingStatus
            from app.models.connection_table import KIND_TABLE, ConnectionTable
            rows = (await db.execute(
                select(ConnectionTable).where(
                    ConnectionTable.connection_id == str(conn.id),
                    ConnectionTable.kind == KIND_TABLE,
                )
            )).scalars().all()
            entries = entries_from_tables(rows)
            if not entries:
                return None, "the connection has not been indexed yet (run a reindex, or use source='live')"
            last = (await db.execute(
                select(ConnectionIndexing.finished_at).where(
                    ConnectionIndexing.connection_id == str(conn.id),
                    ConnectionIndexing.user_id.is_(None),
                    ConnectionIndexing.status == ConnectionIndexingStatus.COMPLETED.value,
                ).order_by(ConnectionIndexing.finished_at.desc()).limit(1)
            )).scalar_one_or_none()
            if last is None:
                last = max((getattr(r, "updated_at", None) for r in rows if getattr(r, "updated_at", None)), default=None)
        except Exception as exc:  # DB unavailable in this context → live
            return None, f"catalog unavailable: {exc}"
        indexed_at = last.replace(microsecond=0).isoformat() + "Z" if last is not None else None
        return {"entries": entries, "indexed_at": indexed_at, "total": len(entries),
                "key": f"{conn.id}:{indexed_at}"}, None

    def _from_catalog(self, data, catalog: dict, runtime_ctx: Dict[str, Any]) -> ToolEndEvent:
        try:
            page, matched, next_cursor = page_catalog(
                catalog["entries"], scope_key=catalog["key"], folder_id=data.folder_id,
                recursive=bool(data.recursive), name_pattern=data.name_pattern,
                created_after=data.created_after, created_before=data.created_before,
                modified_after=data.modified_after, modified_before=data.modified_before,
                sort_by=data.sort_by, sort_order=data.sort_order, limit=data.limit, cursor=data.cursor,
            )
        except (CursorError, ValueError) as exc:
            return self._fail(data.connection_id, str(exc))
        entries, _ = self._apply(page, None)
        when = f"indexed {catalog['indexed_at']}" if catalog["indexed_at"] else "index time unknown"
        hint = (f" Catalog: {catalog['total']} files, {when}; {matched} match; "
                f"sorted by {data.sort_by} {data.sort_order}; rows carry modified/created times.")
        if next_cursor:
            hint += " More pages: pass next_cursor as cursor with the same inputs."
        hint += " Files changed since the index run need source='live'."
        event = self._end(data.connection_id, entries, False, "catalog", hint, runtime_ctx, scoped=_scoped_listing(data))
        event.payload["output"].update(
            source="catalog", indexed_at=catalog["indexed_at"], catalog_files=catalog["total"],
            matched=matched, next_cursor=next_cursor, incomplete=bool(next_cursor),
            stop_reason="result_limit" if next_cursor else None,
        )
        return event

    def _folders_from_catalog(self, data, catalog: dict) -> ToolEndEvent:
        # Default page = the whole rendered set; an explicit limit still pages.
        limit = data.limit if "limit" in data.model_fields_set else self._OBS_FOLDER_MAX_ROWS
        try:
            page, stats, next_cursor = group_catalog_folders(
                catalog["entries"], scope_key=catalog["key"], folder_id=data.folder_id, depth=data.depth,
                name_pattern=data.name_pattern,
                created_after=data.created_after, created_before=data.created_before,
                modified_after=data.modified_after, modified_before=data.modified_before,
                sort_by=data.sort_by, sort_order=data.sort_order, limit=limit, cursor=data.cursor,
            )
        except (CursorError, ValueError) as exc:
            return self._fail(data.connection_id, str(exc))
        folders = [FolderEntry(**f).model_dump() for f in page]
        when = f"indexed {catalog['indexed_at']}" if catalog["indexed_at"] else "index time unknown"
        scope = f" under {data.folder_id}" if data.folder_id else ""
        summary = (f"Listed {len(folders)} of {stats['folders']} folder(s) at depth {data.depth}{scope} (catalog); "
                   f"{stats['files']} matching file(s) across the catalog's {catalog['total']} files, {when}.")
        if stats["files_outside_folders"]:
            summary += f" {stats['files_outside_folders']} matching file(s) sit above that level, in no folder."
        if next_cursor:
            summary += " More folders: pass next_cursor as cursor with the same inputs."
        summary += (" Folders with no matching file are not shown. Next: list a folder's files with "
                    "folder_id=<path> and recursive=true.")
        observation: Dict[str, Any] = {"summary": summary, "success": True}
        if folders:
            rows = []
            for f in folders[: self._OBS_FOLDER_MAX_ROWS]:
                line = f"{f['path']} — {f['file_count']} file(s)"
                if f.get("latest_modified_at"):
                    line += f" — modified {f['latest_modified_at']}"
                rows.append(line)
            if len(folders) > self._OBS_FOLDER_MAX_ROWS:
                rows.append(f"… +{len(folders) - self._OBS_FOLDER_MAX_ROWS} more on this page — "
                            "page with a smaller limit and cursor to see the rest.")
            observation["details"] = "\n".join(rows)
        return ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True, "connection_id": data.connection_id, "group_by": "folder",
                "folders": folders, "folder_count": stats["folders"],
                "files_outside_folders": stats["files_outside_folders"],
                "file_count": 0, "files": [], "truncated": False,
                "source": "catalog", "indexed_at": catalog["indexed_at"], "catalog_files": catalog["total"],
                "matched": stats["files"], "next_cursor": next_cursor, "incomplete": bool(next_cursor),
                "stop_reason": "result_limit" if next_cursor else None,
            },
            "observation": observation,
        })

    def _apply(self, raw: List[dict], name_pattern) -> tuple:
        files = []
        pat = (name_pattern or "").lower()
        for f in raw:
            name = f.get("name") or f.get("path") or f.get("id")
            # Match the bare filename OR the source-relative path, mirroring
            # grep_files. A case folder named `6044534` holds files whose
            # names carry no case number — `*6044534*` must still find them.
            if pat and not (
                fnmatch.fnmatch(str(name).lower(), pat)
                or fnmatch.fnmatch(str(f.get("path") or name).lower(), pat)
            ):
                continue
            files.append({
                "id": f.get("id") or f.get("file_id") or name,
                "name": name,
                "path": f.get("path") or name,
                "mime_type": f.get("mime_type"),
                "size": f.get("size"),
                "modified_at": f.get("modified_at"),
                "created_at": f.get("created_at"),
                # Mail clients surface the sender as "from" (graph_mail_client /
                # gmail_mail_client `_msg_to_item`). It has no meaning for
                # ordinary files, so it stays None for them.
                "sender": f.get("sender") or f.get("from"),
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
            "title": self._start_title,
            "connection_id": data.connection_id,
        })

        # Resolve the client too. For sources where live listing is cheap
        # (network_dir, S3), list live from the per-connection client: it's
        # fresher AND correctly scoped to THIS connection, whereas the cache is
        # per-data-source and unions every connection's files together.
        client, _client_err = await resolve_file_client(
            runtime_ctx, data.connection_id, self._required_capability
        )
        # Catalog first: instant, filterable, pageable — when this caller may use it.
        catalog, why_not = (None, None)
        if client is not None and data.source != "live":
            catalog, why_not = await self._load_catalog(runtime_ctx, client)
        if data.source == "catalog" and catalog is None:
            yield self._fail(data.connection_id, f"No catalog listing for this connection: {why_not}.")
            return
        # Folder mode groups the indexed catalog in memory. It never falls
        # back to the source: that would mean crawling the whole library.
        if data.group_by == "folder":
            if catalog is not None:
                yield self._folders_from_catalog(data, catalog)
                return
            if client is None and _client_err:
                yield self._fail(data.connection_id, _client_err)
                return
            reason = ("source='live' was requested" if data.source == "live"
                      else (why_not or "this connection cannot be listed from a catalog"))
            yield self._fail(data.connection_id, (
                "group_by='folder' needs the connection's indexed catalog and never crawls the source live "
                f"({reason}). Use source='auto' or 'catalog' on an indexed connection, or list files "
                "with group_by='file' and a folder_id."
            ))
            return
        if catalog is not None and (data.source == "catalog" or self._catalog_by_default(client)):
            yield self._from_catalog(data, catalog, runtime_ctx)
            return
        if data.cursor:
            yield self._fail(data.connection_id, "cursor paging is only available for catalog listings; "
                             "this listing reads the source live (narrow with folder_id or name_pattern instead).")
            return
        # SharePoint opts into bounded live browsing under the resolved identity.
        # Do not pass new keywords to other connectors or alter their cache/walk paths.
        if isinstance(client, SharepointBoundedListing):
            try:
                result = await client.alist_files_bounded(
                    folder_id=data.folder_id, recursive=data.recursive,
                    limit=data.limit, name_pattern=data.name_pattern,
                    created_after=data.created_after, created_before=data.created_before,
                    modified_after=data.modified_after, modified_before=data.modified_before,
                )
                rows, hint = result.files, ''
                if (data.sort_by, data.sort_order) != ("name", "asc"):
                    if result.stop_reason:
                        hint += (f" sort_by={data.sort_by} was NOT applied: the live scan stopped early, so a sorted "
                                 "partial scan would mislead; use the catalog (source='auto') or narrow the scope.")
                    else:
                        rows, _, _ = page_catalog(rows, scope_key='live', recursive=True, sort_by=data.sort_by,
                                                  sort_order=data.sort_order, limit=max(len(rows), 1))
                        hint += f" Sorted by {data.sort_by} {data.sort_order}."
                entries, _ = self._apply(rows, None)
                if result.stop_reason == 'result_limit':
                    hint += (" Listing stopped at the requested limit; more matching files may exist. "
                             "Narrow folder_id or name_pattern, or increase limit (up to 500).")
                elif result.stop_reason == 'catalog_limit':
                    hint += (" Listing stopped at the connection's Max files cap; more matching files may exist. "
                            "Narrow folder_id or ask an administrator to review the connection scope/cap.")
                event = self._end(data.connection_id, entries, False, "live", hint, runtime_ctx, scoped=_scoped_listing(data))
                event.payload['output'].update(
                    source="live", incomplete=bool(result.stop_reason), stop_reason=result.stop_reason,
                )
                yield event
            except Exception as exc:
                from ._file_tool_common import friendly_tool_error
                yield self._fail(data.connection_id, friendly_tool_error(
                    self._operation_name, getattr(getattr(client, '_bow_connection', None), 'name', ''), exc,
                ))
            return
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
                yield self._end(data.connection_id, entries, truncated, "live", runtime_ctx=runtime_ctx, scoped=_scoped_listing(data))
                return
            except Exception as e:
                yield self._fail(
                    data.connection_id, f"live {self._operation_name} failed: {e}"
                )
                return

        # --- Cache path ---
        data_source, err = await resolve_file_data_source(runtime_ctx, data.connection_id)
        if err:
            # No cached data source but we may still have a live client.
            if client is not None:
                try:
                    entries, truncated = await _live()
                    yield self._end(data.connection_id, entries, truncated, "live", runtime_ctx=runtime_ctx, scoped=_scoped_listing(data))
                    return
                except Exception as e:
                    yield self._fail(
                        data.connection_id, f"live {self._operation_name} failed: {e}"
                    )
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

        # The catalog is flat (name == source-relative path), so scope
        # folder_id / recursive here — the live path gets them from the client.
        # Only when the id is a path the catalog knows: an opaque provider
        # folder id (Graph/Drive) matches nothing, and blanking the listing
        # for it would be worse than the unscoped list it always returned.
        folder = (data.folder_id or "").strip().strip("/")
        prefix = f"{folder}/" if folder else ""
        if prefix and not any(str(t.name or "").startswith(prefix) for t in (tables or [])):
            prefix = ""
        raw = []
        for t in (tables or []):
            rel = str(t.name or "")
            if prefix:
                if not rel.startswith(prefix):
                    continue
                if not data.recursive and "/" in rel[len(prefix):]:
                    continue
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
                yield self._end(data.connection_id, entries, truncated, "live (cache empty)", runtime_ctx=runtime_ctx, scoped=_scoped_listing(data))
                return
            except Exception:
                pass

        hint = (
            ""
            if entries
            else f" Catalog is empty — try {self._empty_hint_action} or run a refresh."
        )
        yield self._end(data.connection_id, entries, truncated, "cache", hint, runtime_ctx=runtime_ctx, scoped=_scoped_listing(data))
