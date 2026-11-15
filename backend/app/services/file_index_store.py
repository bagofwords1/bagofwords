"""Read/write access to the full-text file index (index tier ``fulltext``).

The store owns everything dialect-specific about the index:

- Postgres keeps each section's terms in ``file_index_sections.tsv`` (GIN).
  Postgres' own ``ts_rank`` ignores how rare a word is, so ranking here weights
  each query word by its inverse document frequency in the connection.
- SQLite keeps them in the FTS5 table ``file_index_fts`` and ranks with FTS5's
  built-in BM25.

Both are fed by the same tokenizer (``data_sources/clients/_fulltext.py``), so
a query matches the same files on either database; only the order of
near-ties may differ.

``FileIndexSink`` is the piece handed to a file client's ``get_schemas`` — it
runs on the indexing worker thread and commits files in small batches, so an
interrupted run keeps what it finished.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_sources.clients._fulltext import (
    FULLTEXT_INDEX_VERSION,
    FileSections,
    ParsedQuery,
    Section,
)
from app.models.file_index import FILE_ERROR, FileIndexFile, FileIndexSection

logger = logging.getLogger(__name__)

# A match in the file's own name/path counts for more than the same words in
# its body: a file called "refund policy.pdf" is the refund policy.
_NAME_SECTION_BOOST = 2.0
# Extra sections of the same file add a little to its score, so a file that is
# about the topic outranks one that mentions it once.
_EXTRA_SECTION_WEIGHT = 0.15
_EXTRA_SECTIONS_COUNTED = 4
# Sections reported per file in a search result.
_SECTIONS_PER_FILE = 3
# A query word found in more than this share of all sections cannot narrow the
# search; it still counts toward the score but is not used to pick candidates
# (as long as a rarer word is).
_COMMON_TERM_SHARE = 0.25


def file_key(file_id: str) -> str:
    return hashlib.sha1(str(file_id).encode("utf-8", "surrogatepass")).hexdigest()


def _dialect(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


# ------------------------------------------------------------------ encoding


def _tsvector_literal(section: Section) -> str:
    """Postgres tsvector text for a section: every form with its positions.
    Alternative forms share the position of the word they came from, so a
    phrase matches whichever form was searched."""
    positions: Dict[str, List[int]] = {}
    for t in section.terms:
        for form in (t.base, *t.alts):
            positions.setdefault(form, []).append(t.pos)
    parts = []
    for form, pos in positions.items():
        # Forms are letters/digits only (see _fulltext._WORD_RE); quoting keeps
        # tsvector keywords out of the way.
        uniq = sorted(set(pos))[:255]
        parts.append("'" + form.replace("'", "''") + "':" + ",".join(str(p) for p in uniq))
    return " ".join(parts)


def _fts_columns(section: Section) -> Tuple[str, str]:
    """(terms, alts) text for the SQLite FTS5 row."""
    terms = " ".join(t.base for t in section.terms)
    alts = " ".join(a for t in section.terms for a in t.alts)
    return terms, alts


def _tsquery_or(forms: Iterable[str]) -> str:
    return " | ".join("'" + f.replace("'", "''") + "'" for f in forms)


def _fts_quote(form: str) -> str:
    return '"' + form.replace('"', '""') + '"'


# -------------------------------------------------------------------- results


@dataclass
class SectionHit:
    kind: str
    loc: Dict[str, Any]
    score: float


@dataclass
class FileHit:
    file_id: str
    path: Optional[str]
    status: str
    score: float
    sections: List[SectionHit] = field(default_factory=list)


# ---------------------------------------------------------------------- store


class FileIndexStore:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.dialect = _dialect(db)

    # ------------------------------------------------------------- reading

    async def prior_versions(self, connection_id: str) -> Dict[str, Tuple[Optional[str], int, str, Optional[str]]]:
        """{file_id: (version, index_version, status, meta json)} for every indexed file."""
        rows = (await self.db.execute(
            select(FileIndexFile.file_id, FileIndexFile.version,
                   FileIndexFile.index_version, FileIndexFile.status, FileIndexFile.meta)
            .where(FileIndexFile.connection_id == str(connection_id))
        )).all()
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}

    async def stats(self, connection_id: str) -> Dict[str, Any]:
        cid = str(connection_id)
        by_status = dict((await self.db.execute(
            select(FileIndexFile.status, func.count()).where(FileIndexFile.connection_id == cid)
            .group_by(FileIndexFile.status)
        )).all())
        sections, chars, last = (await self.db.execute(
            select(func.coalesce(func.sum(FileIndexFile.section_count), 0),
                   func.coalesce(func.sum(FileIndexFile.char_count), 0),
                   func.max(FileIndexFile.indexed_at))
            .where(FileIndexFile.connection_id == cid)
        )).one()
        return {
            "files": int(sum(by_status.values())),
            "by_status": {k: int(v) for k, v in by_status.items()},
            "sections": int(sections or 0),
            "chars": int(chars or 0),
            "last_indexed_at": last,
        }

    async def has_index(self, connection_id: str) -> bool:
        row = (await self.db.execute(
            select(FileIndexFile.id).where(FileIndexFile.connection_id == str(connection_id)).limit(1)
        )).first()
        return row is not None

    # ------------------------------------------------------------- writing

    async def _delete_file_rows(self, file_refs: Sequence[int]) -> None:
        if not file_refs:
            return
        refs = list(file_refs)
        for i in range(0, len(refs), 500):
            chunk = refs[i:i + 500]
            if self.dialect == "sqlite":
                ids = (await self.db.execute(
                    select(FileIndexSection.id).where(FileIndexSection.file_ref.in_(chunk))
                )).scalars().all()
                for j in range(0, len(ids), 500):
                    await self.db.execute(
                        text("DELETE FROM file_index_fts WHERE rowid IN (%s)"
                             % ",".join(str(int(x)) for x in ids[j:j + 500]))
                    )
            await self.db.execute(delete(FileIndexSection).where(FileIndexSection.file_ref.in_(chunk)))
            await self.db.execute(delete(FileIndexFile).where(FileIndexFile.id.in_(chunk)))

    async def write_file(self, connection_id: str, file_id: str, path: Optional[str],
                         version: Optional[str], doc: FileSections,
                         meta: Optional[Dict[str, Any]] = None) -> None:
        """Replace one file's sections. The caller commits."""
        cid = str(connection_id)
        key = file_key(file_id)
        old = (await self.db.execute(
            select(FileIndexFile.id).where(
                FileIndexFile.connection_id == cid, FileIndexFile.file_key == key)
        )).scalars().all()
        await self._delete_file_rows(old)

        row = FileIndexFile(
            connection_id=cid, file_key=key, file_id=str(file_id), path=path,
            version=version, status=doc.status, section_count=len(doc.sections),
            char_count=int(doc.chars or 0), index_version=FULLTEXT_INDEX_VERSION,
            meta=json.dumps(meta, ensure_ascii=False) if meta else None,
            indexed_at=datetime.utcnow(),
        )
        self.db.add(row)
        await self.db.flush()
        ref = row.id

        if self.dialect == "postgresql":
            if doc.sections:
                await self.db.execute(
                    text(
                        "INSERT INTO file_index_sections "
                        "(connection_id, file_ref, section_no, kind, loc, token_count, tsv) "
                        "VALUES (:cid, :ref, :no, :kind, :loc, :n, CAST(:tsv AS tsvector))"
                    ),
                    [{
                        "cid": cid, "ref": ref, "no": i, "kind": s.kind,
                        "loc": json.dumps(s.loc, ensure_ascii=False), "n": len(s.terms),
                        "tsv": _tsvector_literal(s),
                    } for i, s in enumerate(doc.sections)],
                )
            return

        for i, s in enumerate(doc.sections):
            sid = (await self.db.execute(
                text(
                    "INSERT INTO file_index_sections "
                    "(connection_id, file_ref, section_no, kind, loc, token_count) "
                    "VALUES (:cid, :ref, :no, :kind, :loc, :n) RETURNING id"
                ),
                {"cid": cid, "ref": ref, "no": i, "kind": s.kind,
                 "loc": json.dumps(s.loc, ensure_ascii=False), "n": len(s.terms)},
            )).scalar_one()
            terms, alts = _fts_columns(s)
            await self.db.execute(
                text("INSERT INTO file_index_fts (rowid, terms, alts) VALUES (:id, :terms, :alts)"),
                {"id": sid, "terms": terms, "alts": alts},
            )

    async def prune(self, connection_id: str, keep_file_ids: Iterable[str]) -> int:
        """Drop index entries for files that are no longer at the source."""
        keep = {file_key(f) for f in keep_file_ids}
        rows = (await self.db.execute(
            select(FileIndexFile.id, FileIndexFile.file_key)
            .where(FileIndexFile.connection_id == str(connection_id))
        )).all()
        gone = [r[0] for r in rows if r[1] not in keep]
        await self._delete_file_rows(gone)
        return len(gone)

    async def clear(self, connection_id: str) -> int:
        rows = (await self.db.execute(
            select(FileIndexFile.id).where(FileIndexFile.connection_id == str(connection_id))
        )).scalars().all()
        await self._delete_file_rows(rows)
        return len(rows)

    # ------------------------------------------------------------ searching

    async def search(self, connection_id: str, query: ParsedQuery, limit: int = 50) -> List[FileHit]:
        """Files ranked by how well their sections match the query.

        Any query word may match (a long question still finds the file that
        has its distinctive words); rarer words and exact phrases count more.
        """
        if query.empty:
            return []
        cid = str(connection_id)
        want = max(50, min(int(limit) * 8, 800))
        if self.dialect == "postgresql":
            rows = await self._search_postgres(cid, query, want)
        else:
            rows = await self._search_sqlite(cid, query, want)
        return await self._to_file_hits(cid, rows, limit)

    async def _search_sqlite(self, cid: str, query: ParsedQuery, want: int):
        required = ["terms : " + _fts_quote(" ".join(p)) for p in query.phrases]
        optional = [_fts_quote(f) for t in query.terms for f in t.forms]
        optional += ["terms : " + _fts_quote(" ".join(p)) for p in query.soft_phrases]
        if required:
            # Required phrases narrow the result; every word and phrase of the
            # query still feeds the ranking through the OR group.
            match = "(" + " AND ".join(required) + ")"
            if optional:
                match += " AND (" + " OR ".join(required + optional) + ")"
        elif optional:
            match = " OR ".join(optional)
        else:
            return []
        res = await self.db.execute(
            text(
                "SELECT s.file_ref, s.kind, s.loc, -bm25(file_index_fts, 1.0, 0.8) AS score "
                "FROM file_index_fts JOIN file_index_sections s ON s.id = file_index_fts.rowid "
                "WHERE file_index_fts MATCH :q AND s.connection_id = :cid "
                "ORDER BY bm25(file_index_fts, 1.0, 0.8) LIMIT :n"
            ),
            {"q": match, "cid": cid, "n": want},
        )
        return res.all()

    async def _search_postgres(self, cid: str, query: ParsedQuery, want: int):
        def phrase_query(words: List[str]) -> str:
            return " <-> ".join("'" + w.replace("'", "''") + "'" for w in words)

        terms = [(_tsquery_or(t.forms)) for t in query.terms]
        required = [phrase_query(p) for p in query.phrases]
        all_phrases = list(query.phrases) + list(query.soft_phrases)
        phrases = [phrase_query(p) for p in all_phrases]
        if not terms and not phrases:
            return []
        total = (await self.db.execute(
            select(func.coalesce(func.sum(FileIndexFile.section_count), 0))
            .where(FileIndexFile.connection_id == cid)
        )).scalar_one() or 0
        if not total:
            return []

        # How many sections hold each word: one index-only count per word.
        params: Dict[str, Any] = {"cid": cid}
        selects = []
        for i, t in enumerate(terms):
            params[f"t{i}"] = t
            selects.append(
                f"(SELECT count(*) FROM file_index_sections WHERE connection_id = :cid "
                f"AND tsv @@ to_tsquery('simple', :t{i})) AS d{i}"
            )
        dfs: List[int] = []
        if selects:
            dfs = [int(x or 0) for x in (await self.db.execute(
                text("SELECT " + ", ".join(selects)), params)).one()]

        def idf(df: int) -> float:
            return math.log(1.0 + (total - df + 0.5) / (df + 0.5))

        live = [(i, df) for i, df in enumerate(dfs) if df > 0]
        if not live and not required:
            return []
        weights = {i: idf(df) for i, df in live}
        rare = [i for i, df in live if df <= total * _COMMON_TERM_SHARE]
        filter_idx = rare or [i for i, _ in live]

        score_parts = [f"(:w{i} * ts_rank(s.tsv, to_tsquery('simple', :t{i}), 1))" for i, _ in live]
        for i, _ in live:
            params[f"w{i}"] = weights[i]
        filter_parts = [f"to_tsquery('simple', :t{i})" for i in filter_idx]
        max_w = max(weights.values(), default=1.0)
        for j, p in enumerate(phrases):
            params[f"p{j}"] = p
            # A phrase hit is worth about as much as a strong single-word hit.
            params[f"pw{j}"] = 0.1 * max(max_w, 1.0) * max(2, len(all_phrases[j]))
            score_parts.append(
                f"(CASE WHEN s.tsv @@ to_tsquery('simple', :p{j}) THEN :pw{j} ELSE 0 END)"
            )
        params["n"] = want
        if required:
            # Required phrases pick the candidates; words only rank them.
            where = " AND ".join(
                f"s.tsv @@ to_tsquery('simple', :p{j})" for j in range(len(required)))
        else:
            where = " OR ".join(f"s.tsv @@ {f}" for f in filter_parts)
        sql = (
            "SELECT s.file_ref, s.kind, s.loc, (" + " + ".join(score_parts) + ") AS score "
            "FROM file_index_sections s "
            f"WHERE s.connection_id = :cid AND ({where}) "
            "ORDER BY score DESC LIMIT :n"
        )
        return (await self.db.execute(text(sql), params)).all()

    async def _to_file_hits(self, cid: str, rows, limit: int) -> List[FileHit]:
        per_file: Dict[int, List[Tuple[float, str, str]]] = {}
        for file_ref, kind, loc, score in rows:
            s = float(score or 0.0)
            if kind == "name":
                s *= _NAME_SECTION_BOOST
            per_file.setdefault(int(file_ref), []).append((s, kind, loc))
        if not per_file:
            return []
        ranked: List[Tuple[float, int]] = []
        for ref, secs in per_file.items():
            secs.sort(key=lambda x: -x[0])
            extra = sum(x[0] for x in secs[1:1 + _EXTRA_SECTIONS_COUNTED])
            ranked.append((secs[0][0] + _EXTRA_SECTION_WEIGHT * extra, ref))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        ranked = ranked[: int(limit)]
        refs = [r for _, r in ranked]
        files = {
            f.id: f for f in (await self.db.execute(
                select(FileIndexFile).where(FileIndexFile.id.in_(refs))
            )).scalars().all()
        }
        out: List[FileHit] = []
        for score, ref in ranked:
            f = files.get(ref)
            if f is None:
                continue
            seen_loc = set()
            sections: List[SectionHit] = []
            for s, kind, loc in per_file[ref]:
                if kind == "name" or loc in seen_loc:
                    continue
                seen_loc.add(loc)
                try:
                    parsed = json.loads(loc) if loc else {}
                except ValueError:
                    parsed = {}
                sections.append(SectionHit(kind=kind, loc=parsed, score=round(s, 4)))
                if len(sections) >= _SECTIONS_PER_FILE:
                    break
            out.append(FileHit(file_id=f.file_id, path=f.path, status=f.status,
                               score=round(score, 4), sections=sections))
        return out


# ----------------------------------------------------------------------- sink


class FileIndexSink:
    """Receives sections from a file client while it walks the source.

    The client runs on a worker thread; each ``write`` is queued and committed
    on the event loop in small batches, in its own short transaction. A run
    that is cancelled or times out therefore keeps every batch it committed,
    and the next run skips those files (``is_current``).
    """

    #: Files per commit. Small enough that an interrupted run loses little,
    #: large enough that a 50k-file build is not 50k transactions.
    BATCH_FILES = 25

    def __init__(self, loop: asyncio.AbstractEventLoop, session_factory, connection_id: str,
                 prior: Dict[str, Tuple[Optional[str], int, str, Optional[str]]]):
        self._loop = loop
        self._session_factory = session_factory
        self.connection_id = str(connection_id)
        self._prior = prior or {}
        self._pending: List[tuple] = []
        # Set by the client when its walk stopped at the connection's file cap:
        # files past the cap were not seen, so nothing may be pruned.
        self.walk_truncated = False
        self.seen: set = set()
        self.written = 0
        self.reused = 0
        self.statuses: Dict[str, int] = {}

    # Called from the worker thread ---------------------------------------

    def is_current(self, file_id: str, version: Optional[str]) -> bool:
        """True when this file is already indexed at this version — the client
        skips extracting it. Marks the file as still present."""
        prior = self._prior.get(str(file_id))
        ok = bool(
            version and prior and prior[0] == version
            and prior[1] == FULLTEXT_INDEX_VERSION and prior[2] != FILE_ERROR
        )
        if ok:
            self.seen.add(str(file_id))
            self.reused += 1
            self.statuses[prior[2]] = self.statuses.get(prior[2], 0) + 1
        return ok

    def prior_meta(self, file_id: str) -> Dict[str, Any]:
        """What was stored with the file's last index entry (keywords, hash)."""
        prior = self._prior.get(str(file_id))
        if not prior or not prior[3]:
            return {}
        try:
            data = json.loads(prior[3])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def prior_status(self, file_id: str) -> Optional[str]:
        prior = self._prior.get(str(file_id))
        return prior[2] if prior else None

    def write(self, file_id: str, path: Optional[str], version: Optional[str], doc: FileSections,
              meta: Optional[Dict[str, Any]] = None) -> None:
        self.seen.add(str(file_id))
        self.statuses[doc.status] = self.statuses.get(doc.status, 0) + 1
        self._pending.append((str(file_id), path, version, doc, meta))
        if len(self._pending) >= self.BATCH_FILES:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        fut = asyncio.run_coroutine_threadsafe(self._commit(batch), self._loop)
        fut.result()

    # Runs on the event loop -----------------------------------------------

    async def _commit(self, batch) -> None:
        async with self._session_factory() as db:
            store = FileIndexStore(db)
            for file_id, path, version, doc, meta in batch:
                await store.write_file(self.connection_id, file_id, path, version, doc, meta)
            await db.commit()
        self.written += len(batch)

    async def aflush(self) -> None:
        """Commit whatever the worker thread left queued (it has stopped)."""
        if self._pending:
            batch, self._pending = self._pending, []
            await self._commit(batch)

    async def finish(self, *, prune: bool) -> Dict[str, Any]:
        await self.aflush()
        pruned = 0
        async with self._session_factory() as db:
            store = FileIndexStore(db)
            if prune and not self.walk_truncated:
                pruned = await store.prune(self.connection_id, self.seen)
                await db.commit()
            stats = await store.stats(self.connection_id)
        stats.update({"written": self.written, "reused": self.reused, "pruned": pruned})
        stats.pop("last_indexed_at", None)
        return stats
