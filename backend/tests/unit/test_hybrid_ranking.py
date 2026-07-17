"""Unit tests for hybrid retrieval primitives (BM25, RRF, weighted fusion)."""
from app.core.hybrid_ranking import (
    BM25,
    tokenize,
    fuse,
    fuse_rrf,
    fuse_weighted,
    _minmax,
)


def test_tokenize_lowercases_and_drops_short_tokens():
    assert tokenize("Q3 Revenue REPORT a b") == ["q3", "revenue", "report"]


def test_bm25_ranks_rare_terms_higher():
    # 'revenue' appears in one doc, 'the' in all — rare term must dominate.
    corpus = [
        tokenize("the revenue report"),
        tokenize("the sales figures"),
        tokenize("the cost breakdown"),
    ]
    bm = BM25(corpus)
    scores = bm.scores(tokenize("the revenue"))
    assert scores[0] == max(scores)
    assert scores[0] > scores[1]


def test_bm25_length_normalization():
    # Same single match; the shorter doc should score at least as high.
    corpus = [tokenize("revenue"), tokenize("revenue " + "filler " * 40)]
    bm = BM25(corpus)
    scores = bm.scores(tokenize("revenue"))
    assert scores[0] >= scores[1]


def test_minmax_handles_flat_and_empty():
    assert _minmax([]) == []
    assert _minmax([5, 5, 5]) == [0.0, 0.0, 0.0]
    assert _minmax([0, 5, 10]) == [0.0, 0.5, 1.0]


def test_weighted_fusion_default_vector_dominates():
    bm25 = [1.0, 0.0]
    vec = [0.0, 1.0]
    out = fuse_weighted(bm25, vec)  # 0.7 vector + 0.3 bm25
    assert out[1] > out[0]
    assert abs(out[0] - 0.3) < 1e-9
    assert abs(out[1] - 0.7) < 1e-9


def test_weighted_fusion_missing_vector_uses_bm25_only():
    bm25 = [2.0, 1.0]
    vec = [None, 0.9]
    out = fuse_weighted(bm25, vec)
    # doc0 has no vector -> only its normalized bm25 (=1.0) * 0.3
    assert abs(out[0] - 0.3) < 1e-9


def test_rrf_does_not_zero_or_bury_unembedded_docs():
    # doc0 wins bm25 but has no vector. Under weighted fusion it would be capped
    # at 0.3*norm and buried; under RRF it still gets a real, non-zero score
    # from its bm25 rank and outranks a doc that is weak on both signals.
    bm25 = [3.0, 1.0, 0.1]
    vec = [None, 0.2, 0.15]
    out = fuse_rrf(bm25, vec)
    assert out[0] > 0.0
    # doc0 (bm25 #1, unembedded) beats doc2 (bm25 #3 + weak vector).
    assert out[0] > out[2]


def test_rrf_rewards_agreement_across_signals():
    # A doc ranked highly by BOTH bm25 and vector should top the fusion.
    bm25 = [1.0, 0.9, 0.1]
    vec = [0.95, 0.1, 0.2]
    out = fuse_rrf(bm25, vec)
    assert out[0] == max(out)


def test_fuse_selects_strategy_by_coverage():
    bm25 = [1.0, 0.0]
    vec = [0.0, 1.0]
    cold = fuse(bm25, vec, coverage=0.5)
    warm = fuse(bm25, vec, coverage=0.99)
    # Cold uses RRF (small reciprocal-rank magnitudes), warm uses weighted.
    assert max(cold) < 0.1
    assert max(warm) == 0.7


def test_fuse_no_vectors_is_pure_bm25_minmax():
    bm25 = [2.0, 1.0, 0.0]
    out = fuse(bm25, [None, None, None], coverage=0.0)
    assert out == [1.0, 0.5, 0.0]
