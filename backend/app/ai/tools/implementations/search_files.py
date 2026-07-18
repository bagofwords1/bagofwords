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
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import FileEntry, SearchFilesInput, SearchFilesOutput
from app.data_sources.clients.base import Capability

from ._file_tool_common import resolve_file_client

# Keys under a catalog row's metadata_json that may carry a keyword index.
_FILE_METADATA_KEYS = ("network_dir", "graph", "google_drive", "s3")
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

# How many BM25-ranked candidates to consider for the vector leg (and to
# lazily embed). Keeps cosine + lazy-embed cost bounded on large catalogs.
_HYBRID_CANDIDATE_POOL = 200


def _file_sub(meta_json) -> dict[str, Any]:
    meta_json = meta_json or {}
    return next((meta_json.get(k) for k in _FILE_METADATA_KEYS if meta_json.get(k)), {}) or {}


def _entry_for(t, sub) -> dict[str, Any]:
    return FileEntry(
        id=sub.get("file_id") or t.name,
        name=(t.name.split("/")[-1] if t.name else t.name),
        path=t.name,
        mime_type=sub.get("mime_type"),
        size=sub.get("size"),
        modified_at=sub.get("modified_at"),
        web_url=sub.get("web_url"),
    ).model_dump()


async def _hybrid_index_search(db, tables, query: str, max_results: int) -> tuple[list[dict[str, Any]], bool]:
    """Hybrid BM25 + (optional) vector search over the cached catalog.

    Returns (entries, indexed_any). `indexed_any` is False when no row carries a
    keyword index, so the caller falls back to a live scan (behavior unchanged
    for providers that don't populate keywords).

    Ranking:
      - BM25 over each file's stored keywords + filename/path tokens.
      - When the embedding backend is available: embed the query, lazily embed
        the top BM25 candidates that lack a fresh vector (write-through), cosine
        against the pool, and fuse (RRF while coverage is partial, weighted once
        warm). No backend → BM25-only.
    """
    from app.core.hybrid_ranking import BM25, fuse, tokenize

    file_rows = [(t, _file_sub(t.metadata_json)) for t in (tables or [])]
    file_rows = [(t, sub) for t, sub in file_rows if sub.get("keywords") is not None]
    if not file_rows:
        return [], False

    # BM25 corpus: keywords + tokenized name/path.
    corpus = []
    for t, sub in file_rows:
        toks = [str(k).lower() for k in (sub.get("keywords") or [])]
        toks += tokenize(t.name or "")
        corpus.append(toks)
    bm = BM25(corpus)
    qtokens = tokenize(query)
    bm_scores = bm.scores(qtokens)

    # Filename affinity: a hit in the file's own name/path is a strong signal
    # for file search (a report literally called "acme.csv"). Boost the lexical
    # score so it outranks a mere body-keyword match, preserving the pre-hybrid
    # behavior. Full-query substring is the strongest; per-token name hits add
    # a smaller bump.
    q_lower = (query or "").strip().lower()
    for i, (t, _sub) in enumerate(file_rows):
        name = (t.name or "").lower()
        if not name:
            continue
        name_tokens = set(tokenize(name))
        if q_lower and q_lower in name:
            bm_scores[i] += 5.0
        bm_scores[i] += sum(1.0 for qt in qtokens if qt in name_tokens)

    order = sorted(range(len(file_rows)), key=lambda i: bm_scores[i], reverse=True)

    # Vector leg (best-effort). Only consider the top BM25 candidates so cosine
    # and lazy embedding stay bounded on large catalogs.
    vector_scores: list[float | None] = [None] * len(file_rows)
    coverage = 0.0
    try:
        from app.ai.embeddings import cosine, get_backend
        from app.services.file_embedding_service import embed_rows, is_stale

        backend = get_backend()
        if backend is not None and qtokens:
            pool_idx = order[:_HYBRID_CANDIDATE_POOL]
            pool_rows = [file_rows[i][0] for i in pool_idx]
            # Lazy write-through: embed stale candidates (bounded inside embed_rows).
            await embed_rows(db, pool_rows, commit=True)
            qvec = (await backend.embed_texts_async([query]))[0]
            tag = backend.model_tag
            embedded = 0
            for i in pool_idx:
                row = file_rows[i][0]
                if row.embedding is not None and row.embedding_model == tag and not is_stale(row, tag):
                    vector_scores[i] = cosine(qvec, row.embedding)
                    embedded += 1
            coverage = embedded / len(pool_idx) if pool_idx else 0.0
    except Exception:
        # Any embedding failure → pure BM25.
        vector_scores = [None] * len(file_rows)
        coverage = 0.0

    fused = fuse(bm_scores, vector_scores, coverage)
    ranked = sorted(range(len(file_rows)), key=lambda i: fused[i], reverse=True)

    out: list[dict[str, Any]] = []
    for i in ranked:
        if fused[i] <= 0:
            continue
        t, sub = file_rows[i]
        out.append(_entry_for(t, sub))
        if len(out) >= max_results:
            break
    return out, True


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
    def input_model(self) -> type[BaseModel]:
        return SearchFilesInput

    @property
    def output_model(self) -> type[BaseModel]:
        return SearchFilesOutput

    async def _resolve_connection_id(self, runtime_ctx: dict[str, Any], connection_id: str):
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
        self, tool_input: dict[str, Any], runtime_ctx: dict[str, Any]
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
        #    Scoped to THIS connection's ConnectionTable rows — the per-data-
        #    source schema unions every connection, which would leak one
        #    connection's files into another's search results.
        entries: list[dict[str, Any]] = []
        used_index = False
        if not data.deep:
            conn_id = await self._resolve_connection_id(runtime_ctx, data.connection_id)
            if conn_id is not None:
                try:
                    from sqlalchemy import select
                    from sqlalchemy.orm import undefer

                    from app.models.connection_table import ConnectionTable
                    tables = (await runtime_ctx["db"].execute(
                        select(ConnectionTable)
                        .options(undefer(ConnectionTable.embedding))
                        .where(ConnectionTable.connection_id == str(conn_id))
                    )).scalars().all()
                    entries, used_index = await _hybrid_index_search(
                        runtime_ctx["db"], tables, data.query, data.max_results
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

    #: Max hit rows rendered into the model-facing observation (the planner
    #: never sees the output — names/ids must live here or the model re-searches).
    _OBS_HITS_MAX_ROWS = 30

    def _done(self, data, entries, used_index) -> ToolEndEvent:
        how = "keyword index" if used_index else "live scan"
        observation = {
            "summary": f"Found {len(entries)} file(s) matching '{data.query}' ({how})",
            "success": True,
        }
        if entries:
            rows = []
            for e in entries[: self._OBS_HITS_MAX_ROWS]:
                bits = [str(e.get("name") or "?")]
                if e.get("path") and e.get("path") != e.get("name"):
                    bits.append(str(e["path"]))
                rows.append(" — ".join(bits) + f" [id={e.get('id')}]")
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
            },
            "observation": observation,
        })
