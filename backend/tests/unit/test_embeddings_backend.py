"""Unit tests for the embeddings backend resolver + cosine.

These do NOT require fastembed or a model download — they exercise the
graceful-degradation contract and the pure helpers.
"""
import app.ai.embeddings as emb


def test_cosine_basic():
    assert emb.cosine([1, 0], [1, 0]) == 1.0
    assert abs(emb.cosine([1, 0], [0, 1])) < 1e-9
    assert emb.cosine([], [1, 2]) == 0.0
    assert emb.cosine([0, 0], [1, 1]) == 0.0
    assert abs(emb.cosine([1, 1], [1, 1]) - 1.0) < 1e-9


def test_disabled_via_env_returns_none(monkeypatch):
    emb.reset_backend_for_tests()
    monkeypatch.setenv("BOW_EMBEDDINGS_ENABLED", "false")
    assert emb.get_backend() is None
    emb.reset_backend_for_tests()


def test_backend_resolution_is_cached(monkeypatch):
    emb.reset_backend_for_tests()
    monkeypatch.setenv("BOW_EMBEDDINGS_ENABLED", "false")
    first = emb.get_backend()
    # Flip the env AFTER first resolution — cached result must not change.
    monkeypatch.setenv("BOW_EMBEDDINGS_ENABLED", "true")
    assert emb.get_backend() is first
    emb.reset_backend_for_tests()


def test_model_tag_format():
    b = emb.EmbeddingBackend("BAAI/bge-small-en-v1.5")
    assert b.model_tag == "fastembed:BAAI/bge-small-en-v1.5"


def test_embed_empty_is_empty():
    b = emb.EmbeddingBackend("BAAI/bge-small-en-v1.5")
    assert b.embed_texts([]) == []
