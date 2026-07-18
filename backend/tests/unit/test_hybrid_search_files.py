"""Unit test for the hybrid file-search ranking (BM25 + vector fusion).

Uses a fake in-memory embedding backend so no model download is needed, and an
in-memory fake DB session (embed_rows writes vectors back onto the row objects).
Exercises the key win: a file whose keywords DON'T lexically overlap the query
but ARE semantically related gets surfaced via the vector leg.
"""
import asyncio
from types import SimpleNamespace

import app.ai.embeddings as emb
import app.services.file_embedding_service as fes
from app.ai.tools.implementations.search_files import _hybrid_index_search


class _FakeBackend:
    """Deterministic toy embeddings: map known phrases to fixed vectors so
    'revenue'-ish text is close to 'sales'-ish text and far from 'cat'."""
    model_tag = "fake:test"

    _VECTORS = {
        "revenue": [1.0, 0.0, 0.0],
        "sales": [0.9, 0.2, 0.0],      # near revenue
        "income": [0.85, 0.25, 0.0],   # near revenue
        "cat": [0.0, 0.0, 1.0],        # far
    }

    def _vec(self, text: str):
        t = text.lower()
        for key, v in self._VECTORS.items():
            if key in t:
                return v
        return [0.0, 1.0, 0.0]

    def embed_texts(self, texts):
        return [self._vec(t) for t in texts]

    async def embed_texts_async(self, texts):
        return self.embed_texts(texts)


class _FakeDB:
    async def commit(self):
        return None

    async def rollback(self):
        return None


def _row(name, keywords, content_hash="h"):
    return SimpleNamespace(
        name=name,
        metadata_json={"network_dir": {
            "keywords": keywords, "content_hash": content_hash,
            "file_id": name, "mime_type": "application/pdf",
        }},
        embedding=None, embedding_model=None, embedding_hash=None,
    )


def test_semantic_leg_surfaces_non_lexical_match(monkeypatch):
    emb.reset_backend_for_tests()
    backend = _FakeBackend()
    monkeypatch.setattr(emb, "get_backend", lambda: backend)
    monkeypatch.setattr(fes, "get_backend", lambda: backend)

    rows = [
        _row("annual_sales_summary.pdf", ["sales", "annual", "summary"]),
        _row("cat_photos.pdf", ["cat", "photos", "pets"]),
        _row("income_statement.pdf", ["income", "statement"]),
    ]

    # Query 'revenue' shares NO keyword with any file. Pure BM25 would score
    # everything 0; the vector leg must pull the sales/income files up.
    entries, indexed = asyncio.run(
        _hybrid_index_search(_FakeDB(), rows, "revenue", max_results=10)
    )
    assert indexed is True
    ids = [e["id"] for e in entries]
    assert "annual_sales_summary.pdf" in ids
    assert "income_statement.pdf" in ids
    # The unrelated cat file must not outrank the finance files: either dropped
    # (zero fused score) or strictly last.
    if "cat_photos.pdf" in ids:
        assert ids.index("cat_photos.pdf") == len(ids) - 1
    emb.reset_backend_for_tests()


def test_lazy_write_through_populates_vectors(monkeypatch):
    emb.reset_backend_for_tests()
    backend = _FakeBackend()
    monkeypatch.setattr(emb, "get_backend", lambda: backend)
    monkeypatch.setattr(fes, "get_backend", lambda: backend)

    rows = [_row("sales.pdf", ["sales"]), _row("income.pdf", ["income"])]
    asyncio.run(_hybrid_index_search(_FakeDB(), rows, "revenue", max_results=10))
    # After search, the candidates were embedded and written back.
    assert all(r.embedding is not None for r in rows)
    assert all(r.embedding_model == "fake:test" for r in rows)
    assert all(r.embedding_hash == "h" for r in rows)
    emb.reset_backend_for_tests()


def test_no_backend_is_pure_bm25(monkeypatch):
    emb.reset_backend_for_tests()
    monkeypatch.setattr(emb, "get_backend", lambda: None)
    monkeypatch.setattr(fes, "get_backend", lambda: None)

    rows = [
        _row("revenue_report.pdf", ["revenue", "report"]),
        _row("cat.pdf", ["cat"]),
    ]
    entries, indexed = asyncio.run(
        _hybrid_index_search(_FakeDB(), rows, "revenue", max_results=10)
    )
    assert indexed is True
    # BM25 finds the lexical match; cat scores 0 and is dropped.
    assert entries[0]["id"] == "revenue_report.pdf"
    assert all(e["id"] != "cat.pdf" for e in entries)
    emb.reset_backend_for_tests()


def test_empty_catalog_falls_through(monkeypatch):
    # Rows without a keyword index → indexed=False so the tool live-scans.
    plain = [SimpleNamespace(name="t", metadata_json={"schema": "public"},
                             embedding=None, embedding_model=None, embedding_hash=None)]
    entries, indexed = asyncio.run(
        _hybrid_index_search(_FakeDB(), plain, "revenue", max_results=10)
    )
    assert indexed is False
    assert entries == []
