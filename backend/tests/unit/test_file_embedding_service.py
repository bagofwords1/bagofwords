"""Unit tests for file_embedding_service helpers (no model download).

Covers the source-agnostic metadata extraction, embed-text construction, and
staleness logic — the parts that decide WHAT gets (re)embedded.
"""
from types import SimpleNamespace

import app.services.file_embedding_service as fes


def _row(name, metadata_json, embedding=None, embedding_model=None, embedding_hash=None):
    return SimpleNamespace(
        name=name,
        metadata_json=metadata_json,
        embedding=embedding,
        embedding_model=embedding_model,
        embedding_hash=embedding_hash,
    )


def test_file_meta_finds_nested_blob_any_source_key():
    for key in ("network_dir", "s3", "graph", "google_drive"):
        meta = {key: {"keywords": ["a", "b"], "content_hash": "h1"}}
        assert fes._file_meta(meta) == {"keywords": ["a", "b"], "content_hash": "h1"}


def test_file_meta_none_for_plain_db_table():
    # A regular DB table's metadata has no keywords/content_hash blob.
    assert fes._file_meta({"schema": "public"}) is None
    assert fes._file_meta(None) is None
    assert fes._file_meta({}) is None


def test_build_embed_text_uses_name_and_keywords():
    row = _row("reports/2024/q3_revenue.pdf",
               {"network_dir": {"keywords": ["revenue", "finance", "quarterly"]}})
    text = fes.build_embed_text(row)
    assert "q3 revenue.pdf" in text  # slashes/underscores flattened
    assert "revenue, finance, quarterly" in text


def test_build_embed_text_empty_when_no_signal():
    assert fes.build_embed_text(_row("", {"network_dir": {"keywords": []}})) == ""


def test_is_stale_no_vector():
    row = _row("f", {"network_dir": {"content_hash": "h1"}})
    assert fes.is_stale(row, "fastembed:m") is True


def test_is_stale_model_mismatch():
    row = _row("f", {"network_dir": {"content_hash": "h1"}},
               embedding=[0.1], embedding_model="other", embedding_hash="h1")
    assert fes.is_stale(row, "fastembed:m") is True


def test_is_stale_content_changed():
    row = _row("f", {"network_dir": {"content_hash": "h2"}},
               embedding=[0.1], embedding_model="fastembed:m", embedding_hash="h1")
    assert fes.is_stale(row, "fastembed:m") is True


def test_not_stale_when_fresh():
    row = _row("f", {"network_dir": {"content_hash": "h1"}},
               embedding=[0.1], embedding_model="fastembed:m", embedding_hash="h1")
    assert fes.is_stale(row, "fastembed:m") is False


def test_not_stale_hashless_file_once_embedded():
    # metadata-only index mode: no content_hash — embed once, then stable.
    row = _row("f", {"network_dir": {"keywords": ["x"]}},
               embedding=[0.1], embedding_model="fastembed:m", embedding_hash=None)
    assert fes.is_stale(row, "fastembed:m") is False
