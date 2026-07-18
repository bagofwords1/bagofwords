# Feedback Loop — "file search only finds files whose keywords match the exact query words"

`search_files` over document sources (network_dir / S3 / SharePoint / OneDrive /
Drive) matched the query against filenames + keywords extracted at index time,
scored by a simple token-hit count. If the user asked for "maternity policy" but
the handbook says "parental leave", the file scored zero and never surfaced —
the same lexical-only failure the instruction loader had, but over a corpus far
too large for the LLM to read.

This loop adds **self-hosted hybrid retrieval**: keyword BM25 fused with local
vector embeddings, OpenClaw-style. No cloud APIs — the embedding model runs
in-process on CPU. Embeddings are produced at index time and lazily on search,
and everything degrades gracefully to BM25-only when embeddings are unavailable.

---

## Design (self-hosted, hybrid, mixed write paths)

**Local embeddings only** (`app/ai/embeddings.py`). fastembed/ONNX on CPU,
config-driven model (`embeddings:` block in bow-config):
- default `BAAI/bge-small-en-v1.5` — MIT, 33M params, 384 dims, <10ms/text on
  CPU (fast enough to embed lazily at query time),
- `intfloat/multilingual-e5-small` (MIT) for multilingual corpora,
- larger models (e.g. Qwen3-Embedding-0.6B, Apache-2.0) for index-time-only
  installs that can afford the latency.

Model choice research: bge-small (MIT) is the best English size/quality/latency
point; e5-small (MIT) the multilingual pick; both permissively licensed so we
can bake them into the distributed image. EmbeddingGemma scores higher but ships
under Google's custom Gemma terms (redistribution flow-down), which is friction
for a self-hosted image — avoided.

The model is **baked into the Docker image** (`scripts/download_embedding_model.py`
+ Dockerfile step, same pattern as the tiktoken cache) so airgapped installs
never fetch at runtime. `BOW_EMBEDDINGS_ENABLED=false` opts out.

**Hybrid ranking** (`app/core/hybrid_ranking.py`), DB-agnostic (runs in-process,
identical on SQLite/Postgres):
- Okapi BM25 over each file's stored keywords + filename/path tokens (real IDF
  ranking — rare terms outweigh common ones — replacing the old hit-count),
- vector cosine against per-file embeddings,
- fusion picks strategy by embedding **coverage** of the candidate set:
  reciprocal-rank fusion while coverage is partial (a strong keyword hit isn't
  buried just for lacking a vector — unembedded docs get a neutral trailing rank,
  not exclusion), weighted `0.7·vector + 0.3·bm25` once warm (≥95% covered).

**Storage** (`connection_tables`): `embedding` (deferred JSON — normal catalog
reads don't drag the vector), `embedding_model` (mismatch ⇒ treat as
un-embedded), `embedding_hash` (the file `content_hash` the vector was computed
from ⇒ free staleness detection via the existing incremental-index machinery).

**Two write paths** (`app/services/file_embedding_service.py`):
- **Index time** — `refresh_schema` embeds new/changed files after the catalog
  upsert (bounded per run so a cold 100k index never blocks). The embed text is
  cheap and re-read-free: filename + ranked keywords already in `metadata_json`.
- **Lazy write-through** — `search_files` embeds the top BM25 candidates that
  lack a fresh vector and writes them back, so the first query on a folder warms
  it and every later query is instant.
- **Trickle for free** — the existing scheduled-reindex loop calls
  `refresh_schema` on its interval, so each tick embeds up to
  `MAX_EMBED_PER_INDEX_RUN` un-embedded files; a cold corpus converges over
  successive runs with no separate job.

`read_file`-triggered embedding is intentionally omitted: you search before you
read, so the search-lazy path already warms the exact files a read would touch.

---

## Loop A — deterministic (no model download)

```bash
cd backend
uv sync --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_EMBEDDINGS_ENABLED=false
uv run --extra dev pytest \
  tests/unit/test_hybrid_ranking.py \
  tests/unit/test_embeddings_backend.py \
  tests/unit/test_file_embedding_service.py \
  tests/unit/test_hybrid_search_files.py \
  tests/unit/test_attach_and_index.py -q
```

Observed: **all pass**. Highlights:
- `test_bm25_ranks_rare_terms_higher` — real IDF vs the old hit-count.
- `test_rrf_does_not_zero_or_bury_unembedded_docs` — the partial-coverage
  fairness property (neutral rank for missing vectors).
- `test_semantic_leg_surfaces_non_lexical_match` (fake backend) — a file whose
  keywords share NO token with the query is pulled up by the vector leg.
- `test_lazy_write_through_populates_vectors` — search embeds + persists.
- `test_no_backend_is_pure_bm25` — graceful degradation.

## Loop B — live, real model + real PDFs

Model baked/cached once (`BOW_EMBEDDINGS_CACHE_DIR=/tmp/bow-models python
scripts/download_embedding_model.py`), then a 6-PDF corpus whose bodies use
topical vocabulary but NOT the query words, indexed through the real
`NetworkDirClient` (real pypdf extraction → keywords → `embed_connection_tables`):

```
embedded 6 files at index time
rows with embedding: 6/6   model: fastembed:BAAI/bge-small-en-v1.5   dims: 384

=== hybrid semantic search ===
[OK ] 'how much profit did we make last quarter'   -> q3_financials.pdf
[OK ] 'rules about paid time off and maternity'     -> employee_handbook.pdf
[OK ] 'contract clause for ending the deal early'   -> vendor_agreement.pdf
[OK ] 'pictures of animals'                          -> office_pets_gallery.pdf
RESULT: ALL SEMANTIC QUERIES HIT
```

Every winner is a non-lexical match — "maternity" ↔ "parental leave",
"pictures of animals" ↔ "photographs of cats and dogs", "ending the deal early"
↔ "termination for cause". BM25 alone scores those ~0; the vector leg finds them.

## Notes / follow-ups

- The same `app/ai/embeddings.py` + `app/core/hybrid_ranking.py` are the shared
  foundation for a future instruction-ranking re-ranker (Phase C of the
  instruction work) — one module, many consumers.
- Per-connection `semantic_index: false` opt-out and chunk-level (within-file)
  retrieval with sqlite-vec/pgvector are deliberate future scope; v1 is
  file-level, which answers "which document is about X" and hands the
  within-file step to `read_file`/`grep_files`.
