"""Semantic embedding of document-source files (network_dir/S3/SharePoint/Drive).

File catalog rows are ConnectionTable records whose metadata_json nests a
per-source blob (`{"network_dir": {...}}`, `{"s3": {...}}`, `{"graph": {...}}`,
`{"google_drive": {...}}`) carrying `keywords` and `content_hash`. We embed a
cheap file-level representation (name + path + ranked keywords) — no re-reading
of the file — so embedding is incremental via `content_hash` and rides the same
hash-skip logic that already makes reindexing cheap.

Two write paths (mixed, OpenClaw-style):
- Index time: `embed_connection_tables` embeds new/changed files after the
  catalog upsert (bounded per run so a cold 100k index never blocks).
- Lazy: `embed_rows` embeds a bounded set of candidates at search/read time and
  writes the vectors back (write-through cache).

Everything is best-effort: if the embedding backend is unavailable, these are
no-ops and callers fall back to lexical (BM25) ranking.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.ai.embeddings import get_backend
from app.models.connection_table import ConnectionTable

logger = logging.getLogger(__name__)

# Bound how many files we embed in a single index run (rest caught by later runs
# or the lazy path) so a large cold corpus doesn't stall indexing.
MAX_EMBED_PER_INDEX_RUN = 2000
# Bound lazy per-query embedding so search latency stays predictable.
MAX_LAZY_EMBED_PER_QUERY = 64


def _file_meta(metadata_json: dict | None) -> dict | None:
    """Return the nested file-source metadata blob (the sub-dict carrying
    keywords/content_hash), source-key-agnostic. None for non-file rows."""
    if not isinstance(metadata_json, dict):
        return None
    for v in metadata_json.values():
        if isinstance(v, dict) and ("keywords" in v or "content_hash" in v):
            return v
    return None


def file_content_hash(row: ConnectionTable) -> str | None:
    meta = _file_meta(row.metadata_json)
    return meta.get("content_hash") if meta else None


def build_embed_text(row: ConnectionTable) -> str:
    """Cheap file-level text to embed: name/path + ranked keywords.

    Uses only already-indexed fields (no file re-read), so it's identical work
    whether called at index time or lazily.
    """
    parts: list[str] = []
    if row.name:
        parts.append(str(row.name).replace("/", " ").replace("_", " "))
    meta = _file_meta(row.metadata_json)
    if meta:
        kws = meta.get("keywords") or []
        if kws:
            parts.append(", ".join(str(k) for k in kws[:50]))
    return ". ".join(p for p in parts if p).strip()


def is_stale(row: ConnectionTable, model_tag: str) -> bool:
    """True if the row needs (re)embedding for the given model: no vector, a
    different model, or the file content changed since it was embedded."""
    if row.embedding is None or row.embedding_model != model_tag:
        return True
    ch = file_content_hash(row)
    # If the file has a content hash, require it to match; hash-less files
    # (metadata-only index mode) stay embedded once done.
    return bool(ch) and row.embedding_hash != ch


async def embed_rows(
    db: AsyncSession,
    rows: Sequence[ConnectionTable],
    *,
    limit: int = MAX_LAZY_EMBED_PER_QUERY,
    commit: bool = True,
) -> int:
    """Embed and persist vectors for the given rows that are stale, bounded by
    `limit`. Returns the number embedded. No-op (0) when the backend is off.

    Rows must have `embedding` loaded/undeferred by the caller when they came
    from a query that deferred it — this function reads row.embedding to decide
    staleness.
    """
    backend = get_backend()
    if backend is None or not rows:
        return 0
    model_tag = backend.model_tag

    todo = []
    for row in rows:
        if len(todo) >= limit:
            break
        text = build_embed_text(row)
        if not text:
            continue
        if is_stale(row, model_tag):
            todo.append((row, text))

    if not todo:
        return 0

    try:
        vectors = await backend.embed_texts_async([t for _, t in todo])
    except Exception as e:
        logger.warning(f"file embedding failed: {e}")
        return 0

    for (row, _), vec in zip(todo, vectors, strict=False):
        row.embedding = vec
        row.embedding_model = model_tag
        row.embedding_hash = file_content_hash(row)
    if commit:
        try:
            await db.commit()
        except Exception as e:
            logger.warning(f"file embedding commit failed: {e}")
            await db.rollback()
            return 0
    return len(todo)


async def embed_connection_tables(
    db: AsyncSession,
    connection_id: str,
    *,
    limit: int = MAX_EMBED_PER_INDEX_RUN,
) -> int:
    """Index-time hook: embed new/changed files for a connection after the
    catalog upsert. Best-effort and bounded. Returns count embedded.

    Loads the (deferred) embedding column explicitly so staleness can be judged
    without a second round-trip.
    """
    if get_backend() is None:
        return 0
    result = await db.execute(
        select(ConnectionTable)
        .options(undefer(ConnectionTable.embedding))
        .where(ConnectionTable.connection_id == str(connection_id))
    )
    rows = result.scalars().all()
    # Only file-source rows (those with a nested keywords/hash blob) are
    # embeddable; skip plain DB tables.
    file_rows = [r for r in rows if _file_meta(r.metadata_json) is not None]
    if not file_rows:
        return 0
    n = await embed_rows(db, file_rows, limit=limit, commit=True)
    if n:
        logger.info(f"embedded {n} file(s) for connection {connection_id}")
    return n
