"""Hybrid retrieval primitives: Okapi BM25, reciprocal-rank fusion, and a
weighted score blend.

Used by document-source file search (BM25 over stored per-file keywords +
name/path tokens, fused with embedding cosine scores). Deliberately
DB-agnostic — everything runs in-process over the candidate rows, so it works
identically on SQLite and Postgres.

Fusion policy (OpenClaw-style):
- Warm corpus (embedding coverage >= WARM_COVERAGE): weighted sum of per-query
  min-max-normalized scores, default 0.7 * vector + 0.3 * bm25.
- Cold/partial corpus: reciprocal-rank fusion, which compares RANKS — a file
  that is #1 on BM25 but not yet embedded still competes fairly instead of
  being buried under everything that has a vector.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

# Vector weight for the warm-corpus weighted blend (bm25 gets 1 - this).
VECTOR_WEIGHT = 0.7
# Embedding coverage ratio above which the weighted blend replaces RRF.
WARM_COVERAGE = 0.95
# Standard RRF dampening constant.
RRF_K = 60

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


class BM25:
    """Okapi BM25 over small in-memory corpora (hundreds..thousands of docs).

    Documents are token lists (e.g. a file's stored keywords + name/path
    tokens). Build per query over the candidate corpus — construction is
    O(total tokens) and cheap at catalog scale.
    """

    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_freqs: list[dict[str, int]] = []
        self.doc_lens: list[int] = []
        df: dict[str, int] = {}
        for doc in corpus:
            freqs: dict[str, int] = {}
            for tok in doc:
                freqs[tok] = freqs.get(tok, 0) + 1
            self.doc_freqs.append(freqs)
            self.doc_lens.append(len(doc))
            for tok in freqs:
                df[tok] = df.get(tok, 0) + 1
        self.n_docs = len(self.doc_freqs)
        self.avg_len = (sum(self.doc_lens) / self.n_docs) if self.n_docs else 0.0
        # BM25+-style floor: keep idf positive for very common terms.
        self.idf: dict[str, float] = {
            tok: max(0.05, math.log(1 + (self.n_docs - n + 0.5) / (n + 0.5)))
            for tok, n in df.items()
        }

    def score(self, query_tokens: Iterable[str], index: int) -> float:
        freqs = self.doc_freqs[index]
        dl = self.doc_lens[index] or 1
        norm = self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
        s = 0.0
        for tok in query_tokens:
            f = freqs.get(tok, 0)
            if not f:
                continue
            s += self.idf.get(tok, 0.0) * (f * (self.k1 + 1)) / (f + norm)
        return s

    def scores(self, query_tokens: Sequence[str]) -> list[float]:
        return [self.score(query_tokens, i) for i in range(self.n_docs)]


def _minmax(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def fuse_weighted(
    bm25_scores: Sequence[float],
    vector_scores: Sequence[float | None],
    vector_weight: float = VECTOR_WEIGHT,
) -> list[float]:
    """Weighted blend of min-max-normalized scores. Docs without a vector
    (None) contribute only their BM25 component."""
    bm_n = _minmax(bm25_scores)
    present = [v for v in vector_scores if v is not None]
    vec_n_present = _minmax(present)
    it = iter(vec_n_present)
    vec_n = [next(it) if v is not None else None for v in vector_scores]
    out = []
    for b, v in zip(bm_n, vec_n, strict=False):
        if v is None:
            out.append((1 - vector_weight) * b)
        else:
            out.append(vector_weight * v + (1 - vector_weight) * b)
    return out


def fuse_rrf(
    bm25_scores: Sequence[float],
    vector_scores: Sequence[float | None],
    k: int = RRF_K,
) -> list[float]:
    """Reciprocal-rank fusion under partial vector coverage.

    Every doc contributes both a bm25 term and a vector term. Docs without a
    vector are assigned a NEUTRAL trailing rank (just past the last embedded
    doc) rather than being dropped from the vector list — dropping them would
    give embedded docs an extra reciprocal-rank term and structurally bury a
    strong bm25 hit merely for lacking a vector. Neutral placement makes
    "unembedded" cost nothing beyond not benefiting from semantic agreement.
    """
    n = len(bm25_scores)

    def ranks(scores: Sequence[tuple[int, float]]) -> dict[int, int]:
        ordered = sorted(scores, key=lambda x: x[1], reverse=True)
        return {idx: rank for rank, (idx, _) in enumerate(ordered, start=1)}

    bm_ranks = ranks([(i, s) for i, s in enumerate(bm25_scores)])
    vec_ranks = ranks([(i, s) for i, s in enumerate(vector_scores) if s is not None])
    neutral_vec_rank = len(vec_ranks) + 1  # trailing rank for unembedded docs

    out = []
    for i in range(n):
        vr = vec_ranks.get(i, neutral_vec_rank)
        out.append(1.0 / (k + bm_ranks[i]) + 1.0 / (k + vr))
    return out


def fuse(
    bm25_scores: Sequence[float],
    vector_scores: Sequence[float | None],
    coverage: float,
    vector_weight: float = VECTOR_WEIGHT,
) -> list[float]:
    """Pick the fusion strategy by embedding coverage of the candidate set."""
    if not any(v is not None for v in vector_scores):
        return _minmax(bm25_scores)
    if coverage >= WARM_COVERAGE:
        return fuse_weighted(bm25_scores, vector_scores, vector_weight)
    return fuse_rrf(bm25_scores, vector_scores)
