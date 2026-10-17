"""search_files agent tool — index-first search over a file-based data source.

Two paths:
  1. Fast keyword index (default): match the query against filenames + the
     keywords extracted at index time (stored on the cached catalog row). No
     file re-parsing — instant. Used whenever the connection has been indexed.
  2. Live/deep scan (fallback, or `deep=true`): ask the client to walk the
     directory and grep full file contents — exhaustive but slower. Used when
     the catalog isn't indexed yet, or when the caller forces `deep`.

For providers that don't populate keywords (Graph SharePoint / OneDrive / Drive)
the index path finds nothing and it falls through to the live provider search —
so their behavior is unchanged. SharePoint Server with content indexing uses the
keyword catalog only when the connection runs as a service account.

Connectors whose live search is only a filename walk of the source (SharePoint
2010: one SOAP request per folder per page, minutes on a large library) answer
from the catalog whenever one exists — keyword index or, in metadata mode,
plain filename/path matching — and walk the source live only with deep=true or
when there is no catalog. Every live search gets a deadline; clients that
honor it (SharePoint 2010) stop crawling and return what they found so far.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, AsyncIterator, Dict, List, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, SearchFilesInput, SearchFilesOutput
from app.data_sources.clients.base import Capability, SearchStopped

from ._file_catalog import entries_from_tables
from ._file_tool_common import resolve_file_client, resolve_file_data_source

# Keys under a catalog row's metadata_json that may carry a keyword index.
_FILE_METADATA_KEYS = ("network_dir", "graph", "sharepoint_onprem", "documentum", "google_drive", "s3")
# Query tokens keep digit runs (case numbers, IDs) — unlike the indexer's
# body tokenizer — so "6044534" scores against a name/path that carries it.
_WORD_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)


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
            sender=sub.get("sender") or sub.get("from"),
            web_url=sub.get("web_url"),
        ).model_dump())
    return out


def _live_search_is_name_scan(connection_type, config) -> bool:
    """True when the connector's live search only walks the source matching
    filenames/paths (no provider-side search service). For those the catalog
    holds the same names, so a live walk only buys freshness and costs a full
    crawl. SharePoint 2010 has no usable search API over SOAP."""
    if isinstance(config, str):
        try:
            config = json.loads(config or "{}")
        except ValueError:
            config = {}
    return connection_type == "sharepoint_onprem" and str((config or {}).get("api_mode") or "") == "2010"


def _catalog_name_search(catalog: List[Dict[str, Any]], query: str, max_results: int) -> List[Dict[str, Any]]:
    """Match a query against catalog filenames and paths (no keyword index).

    Same scoring as the keyword index: the whole query as a case-insensitive
    substring of the path scores high; each query token found in the path
    adds one. Ties keep a stable path order."""
    q = (query or "").strip().lower()
    qtokens = set(_WORD_RE.findall(q))
    scored = []
    for e in catalog:
        hay = str(e.get("path") or e.get("name") or "").lower()
        score = (5 if q and q in hay else 0) + sum(1 for qt in qtokens if qt in hay)
        if score > 0:
            scored.append((score, hay, e))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [FileEntry(**e).model_dump() for _, _, e in scored[:max_results]]


_STOP_REASONS = {
    "deadline": "it reached its time limit",
    "cancelled": "it was cancelled",
    "folder_limit": "it reached the folder limit for a live search",
    "catalog_limit": "it reached the connection's Max files cap",
}


async def _last_indexed_at(db, connection_id) -> str | None:
    """When the connection's shared catalog last finished indexing (UTC ISO), or None."""
    from sqlalchemy import select

    from app.models.connection_indexing import ConnectionIndexing, ConnectionIndexingStatus
    last = (await db.execute(
        select(ConnectionIndexing.finished_at).where(
            ConnectionIndexing.connection_id == str(connection_id),
            ConnectionIndexing.user_id.is_(None),
            ConnectionIndexing.status == ConnectionIndexingStatus.COMPLETED.value,
        ).order_by(ConnectionIndexing.finished_at.desc()).limit(1)
    )).scalar_one_or_none()
    return last.replace(microsecond=0).isoformat() + "Z" if last is not None else None


class SearchFilesTool(Tool):
    # Capability the resolved connection must expose. Overridden by
    # SearchEmailsTool so the same search path backs a mailbox (SEARCH_EMAILS).
    _required_capability = Capability.SEARCH_FILES
    _item_noun = "file"
    _start_noun = "files"
    _operation_name = "search_files"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_files",
            description=(
                "Search files by free-text query in a Files & Directories, "
                "SharePoint, OneDrive, or Google Drive connection. Matches "
                "filename AND content — including inside PDF, Word, PowerPoint, "
                "Excel, CSV and email files (.eml/.msg: headers, body AND the "
                "text of their attachments). Fast: uses the keyword index built when the "
                "connection was indexed; a zero-hit index lookup automatically "
                "falls through to a live content scan. If a term you're sure is "
                "in a file isn't found, retry with deep=true for an exhaustive "
                "live scan. Returns FILES (not lines) with their IDs — pass an "
                "ID to read_file or attach_file. For exact tokens (error codes, "
                "request IDs) inside text/log files, or to extract the matching "
                "LINES themselves, prefer grep_files when available."
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

    async def _resolve_connection_id(self, runtime_ctx: Dict[str, Any], connection_id: str):
        """Map the passed id (Connection id OR DataSource id) to a single
        Connection id, so the keyword search is scoped to one connection.
        Returns None when it can't be pinned (unknown, or a data source with
        multiple file connections) — the caller then uses the per-connection
        live scan, which is scoped by construction."""
        from ._file_tool_common import FILE_SOURCE_TYPES
        report = runtime_ctx.get("report")
        sid = str(connection_id)
        attached = []
        if report:
            for ds in (report.data_sources or []):
                for conn in (ds.connections or []):
                    if conn.type in FILE_SOURCE_TYPES:
                        attached.append((ds, conn))
        for ds, conn in attached:
            if str(conn.id) == sid:
                return conn.id
        ds_conns = [conn for ds, conn in attached if str(ds.id) == sid]
        if len(ds_conns) == 1:
            return ds_conns[0].id
        db = runtime_ctx.get("db")
        org = runtime_ctx.get("organization")
        if db is not None and org is not None:
            from sqlalchemy import select

            from app.models.connection import Connection
            c = (await db.execute(
                select(Connection).where(
                    Connection.id == sid,
                    Connection.organization_id == str(org.id),
                    Connection.type.in_(list(FILE_SOURCE_TYPES)),
                )
            )).scalar_one_or_none()
            if c is not None:
                return c.id
        return None

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = SearchFilesInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Searching {self._start_noun}: {data.query!r}",
            "connection_id": data.connection_id,
        })

        def _fail(msg: str) -> ToolEndEvent:
            return ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id,
                           "query": data.query, "error": msg},
                "observation": {"summary": msg, "success": False},
            })

        # 1. Fast path: keyword index on the cached catalog.
        #    Scoped to THIS connection's ConnectionTable rows — the per-data-
        #    source schema unions every connection, which would leak one
        #    connection's files into another's search results.
        entries: List[Dict[str, Any]] = []
        used_index = False
        indexed_at = None
        # Name-scan connectors (see _live_search_is_name_scan) with a catalog
        # but no keyword index match catalog names instead of crawling.
        name_scan = False
        catalog_names = False
        catalog_total = 0
        # The keyword index runs even for deep=true: for connectors whose live
        # search matches filenames only (SharePoint Server) the index IS the
        # content search, so a deep scan is unioned with it, never a downgrade.
        if True:
            conn_id = await self._resolve_connection_id(runtime_ctx, data.connection_id)
            if conn_id is not None:
                try:
                    from sqlalchemy import select

                    from app.models.connection import Connection
                    from app.models.connection_table import KIND_TABLE, ConnectionTable
                    policy = (await runtime_ctx["db"].execute(
                        select(Connection.type, Connection.auth_policy, Connection.config).where(Connection.id == str(conn_id))
                    )).first()
                    connection_type, auth_policy, conn_config = tuple(policy) if policy else (None, None, None)
                    name_scan = _live_search_is_name_scan(connection_type, conn_config)
                    tables = (await runtime_ctx["db"].execute(
                        select(ConnectionTable).where(ConnectionTable.connection_id == str(conn_id))
                    )).scalars().all()
                    if connection_type in ("sharepoint_onprem", "documentum") and auth_policy != "system_only":
                        # Per-user identities (Windows / Documentum ACLs) are
                        # checked live; the shared catalog was crawled as one
                        # identity and must not stand in for the caller's. A
                        # service-account (system_only) connection runs every
                        # call as that same account, so its keyword catalog is
                        # exactly what a live crawl would see.
                        tables = []
                    entries = _index_search(tables, data.query, data.max_results)
                    used_index = any(
                        (getattr(t, "metadata_json", None) or {}).get(k, {}).get("keywords") is not None
                        for t in (tables or []) for k in _FILE_METADATA_KEYS
                    )
                    if used_index:
                        indexed_at = await _last_indexed_at(runtime_ctx["db"], conn_id)
                    elif name_scan:
                        catalog = entries_from_tables(
                            t for t in (tables or []) if getattr(t, "kind", KIND_TABLE) == KIND_TABLE
                        )
                        if catalog:
                            catalog_names, catalog_total = True, len(catalog)
                            entries = _catalog_name_search(catalog, data.query, data.max_results)
                            indexed_at = await _last_indexed_at(runtime_ctx["db"], conn_id)
                            if indexed_at is None:
                                last = max((t.updated_at for t in tables if getattr(t, "updated_at", None)), default=None)
                                indexed_at = last.replace(microsecond=0).isoformat() + "Z" if last else None
                except Exception:
                    used_index = False
                    catalog_names = False
                    entries = []

        # 2. Live scan when not indexed, or deep requested, or the index
        #    returned nothing (the term may be deep in a file's body). With
        #    deep=true the live hits are merged after the index hits.
        #    A name-scan connector with a catalog goes live only with deep=true:
        #    its live search matches the same names the catalog already has.
        live_too = False
        stop_reason = None
        if name_scan and (used_index or catalog_names):
            go_live = data.deep
        else:
            go_live = data.deep or not used_index or not entries
        if go_live:
            client, err = await resolve_file_client(
                runtime_ctx, data.connection_id, self._required_capability
            )
            if err:
                # If the index gave us something, return that rather than erroring.
                if entries or catalog_names:
                    yield self._done(data, entries, used_index, runtime_ctx=runtime_ctx, indexed_at=indexed_at,
                                     catalog_names=catalog_names, catalog_total=catalog_total)
                    return
                yield _fail(err)
                return
            # The tool must answer before the runner's timeout: a timed-out
            # tool leaves the UI spinning and the runner retries it, starting
            # a second crawl. Clients that honor `deadline` stop by
            # themselves; `wait_for` is the backstop for those that don't
            # (cancelling it signals the worker thread to stop too).
            budget_s, hard_s = self._live_search_budget()
            files = []
            try:
                files = await asyncio.wait_for(
                    client.asearch_files(data.query, deadline=time.monotonic() + budget_s),
                    timeout=hard_s,
                )
            except SearchStopped as stop:
                files, stop_reason = stop.files, stop.stop_reason
            except TimeoutError:
                if not (entries or catalog_names):
                    yield _fail(
                        f"The live search did not finish within {hard_s}s and was stopped. "
                        "Use more specific words, or list_files with folder_id/name_pattern."
                    )
                    return
                stop_reason = "deadline"
            except Exception as e:
                if entries or catalog_names:
                    yield self._done(data, entries, used_index, runtime_ctx=runtime_ctx, indexed_at=indexed_at,
                                     catalog_names=catalog_names, catalog_total=catalog_total)
                    return
                from app.ai.tools.implementations._file_tool_common import friendly_tool_error
                _cname = getattr(getattr(client, "_bow_connection", None), "name", "") or ""
                yield _fail(friendly_tool_error(self._operation_name, _cname, e))
                return
            live = [FileEntry(
                id=f.get("id"), name=f.get("name"),
                path=f.get("path") if isinstance(f.get("path"), str) else None,
                mime_type=f.get("mime_type"), size=f.get("size"),
                modified_at=f.get("modified_at"), created_at=f.get("created_at"),
                sender=f.get("sender") or f.get("from"),
                web_url=f.get("web_url"),
            ).model_dump() for f in files]
            if entries or catalog_names:
                seen = {str(e["id"]).casefold() for e in entries}
                entries = entries + [e for e in live if str(e["id"]).casefold() not in seen]
                live_too = True
            else:
                entries, used_index = live, False
            entries = entries[: data.max_results]

        yield self._done(data, entries, used_index, runtime_ctx=runtime_ctx, indexed_at=indexed_at, live_too=live_too,
                         catalog_names=catalog_names, catalog_total=catalog_total, stop_reason=stop_reason)

    def _live_search_budget(self) -> tuple:
        """(deadline for cooperative clients, hard cap for the rest), seconds.

        Derived from the tool's declared timeout so the tool always answers
        well before the runner's own idle/hard timeouts fire."""
        try:
            declared = int(self.metadata.timeout_seconds or 30)
        except Exception:
            declared = 30
        return max(5, declared - 5), declared + 30

    #: Max hit rows rendered into the model-facing observation (the planner
    #: never sees the output — names/ids must live here or the model re-searches).
    _OBS_HITS_MAX_ROWS = 30

    def _inventory_row(self, e: dict) -> str:
        """One line of the model-facing hit list. Overridden by SearchEmailsTool
        so mailbox hits carry sender + received date (see list_files)."""
        bits = [str(e.get("name") or "?")]
        if e.get("path") and e.get("path") != e.get("name"):
            bits.append(str(e["path"]))
        return " — ".join(bits) + f" [id={e.get('id')}]"

    def _done(self, data, entries, used_index, runtime_ctx=None, indexed_at=None, live_too=False,
              catalog_names=False, catalog_total=0, stop_reason=None) -> ToolEndEvent:
        from_catalog = used_index or catalog_names
        how = ("keyword index" if used_index else
               "catalog filename/path match" if catalog_names else "live scan")
        if from_catalog and indexed_at:
            how += f", indexed {indexed_at}"
        if from_catalog and live_too:
            how += " + live scan"
        hint = ""
        if catalog_names and not entries and not live_too:
            hint = (f" The catalog ({catalog_total} files) holds file names and paths only, not "
                    "document contents. Try other words from the file name, or deep=true to also "
                    "scan the source live for files added since the index run.")
        if stop_reason:
            hint += (f" The live scan stopped early because {_STOP_REASONS.get(stop_reason, stop_reason)}, "
                     "so results may be incomplete. Narrow the query, or use list_files with "
                     "folder_id/name_pattern.")
        # Repeat-enumeration guard — see list_files._end.
        enum_hint = ""
        if runtime_ctx is not None:
            from app.ai.tools.implementations._file_tool_common import note_enumeration
            n = note_enumeration(runtime_ctx, getattr(data, "connection_id", ""), len(entries))
            if n:
                enum_hint = f" {n}"
        observation = {
            "summary": (
                f"Found {len(entries)} {self._item_noun}(s) "
                f"matching '{data.query}' ({how})"
                + hint
                + enum_hint
            ),
            "success": True,
        }
        if entries:
            rows = [
                self._inventory_row(e) for e in entries[: self._OBS_HITS_MAX_ROWS]
            ]
            if len(entries) > self._OBS_HITS_MAX_ROWS:
                rows.append(f"… +{len(entries) - self._OBS_HITS_MAX_ROWS} more matches")
            observation["details"] = "\n".join(rows)
        return ToolEndEvent(type="tool.end", payload={
            "output": {
                "success": True,
                "connection_id": data.connection_id,
                "query": data.query,
                "file_count": len(entries),
                "files": entries,
                "source": ("catalog+live" if from_catalog and live_too else
                           "catalog" if from_catalog else "live"),
                "indexed_at": indexed_at if from_catalog else None,
                "incomplete": bool(stop_reason),
                "stop_reason": stop_reason,
            },
            "observation": observation,
        })
