# Feedback Loop — "indexing a network_dir with thousands of PDFs re-extracts everything, every time"

A `network_dir` connection with the default `index_mode: "content"` extracts
text from every PDF/Office document on **every** index run — including the
scheduled reindex (default every 12h) of a directory where nothing changed.
On a PDF-heavy share that is minutes-to-hours of sequential pypdf work whose
output already sits, byte-identical, in the catalog rows. The run also
reported no per-file progress and could not be cancelled. This loop proves
the cost, then proves the fix: reindexing an unchanged directory becomes a
stat-only walk that reuses the stored keywords/hashes.

## Root cause (validated)

- `NetworkDirClient.get_schemas()` (`backend/app/data_sources/clients/network_dir_client.py`)
  called `_file_text()` → `extract_document_text()` (pypdf, up to 200 pages /
  200k chars per file) for **every** file on **every** run. Each catalog row
  already stored `size`, `modified_at`, and `content_hash` — none of it was
  consulted to skip unchanged files.
- The scheduled sweeper (`backend/app/services/scheduled_reindex.py`) re-runs
  `refresh_schema` per connection on its interval (default 12h,
  `Connection.DEFAULT_REINDEX_INTERVAL_MINUTES`), so the full extraction cost
  recurred forever on static archives.
- `get_schemas(progress_callback=...)` accepted the callback but never invoked
  it, so the indexing UI showed 0/0 and the runner's cancel checkpoint (the
  callback raising `IndexingCancelled`) never fired.
- `ConnectionService.refresh_schema` wrapped every exception in a generic 500
  (`connection_service.py`), which would have converted `IndexingCancelled`
  into a *failed* run instead of a *cancelled* one.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
pip install uv
uv sync --frozen --extra dev
export ENVIRONMENT=development BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db

# Benchmark: generates a corpus of REAL multi-page PDFs (matplotlib backend,
# selectable text — pypdf does real extraction work), then times three runs.
uv run python scripts/bench_network_dir_indexing.py /tmp/bench_corpus --pdfs 1000 --pages 3
```

Observed BEFORE the fix (1,000 three-page PDFs + 100 txt/csv, local SSD —
a real SMB/NFS mount and messier real-world PDFs are substantially slower):

```
run 1 (cold index):                      102.68s  files=1100
run 2 (unchanged, NO prior catalog):     104.93s   <- every scheduled reindex paid this
```

Observed AFTER the fix (same corpus, same process):

```
run 3 (unchanged, WITH prior catalog):     0.14s
speedup vs old rerun: 775.2x
catalog equivalence: OK (same files, same keywords, same hashes)
```

Regression tests (all deterministic, tmp-dir fixtures):

```bash
uv run pytest tests/unit/test_network_dir_incremental_index.py -q   # 11 tests
uv run pytest tests/e2e/test_network_dir_incremental_e2e.py -q      # full-stack reindex loop
```

The e2e test drives the real stack — `POST /connections` → background
`ConnectionIndexingService` runner → `refresh_schema` →
`NetworkDirClient.get_schemas` — and asserts that `POST /reindex` on an
unchanged directory performs **zero** content extractions (counted by
patching `_file_text`), that a changed file re-extracts exactly that file,
and that the indexing row now reports real per-file progress (3/3).

## The fix

- `NetworkDirClient.get_schemas(progress_callback=None, prior_catalog=None)`
  (`network_dir_client.py`): a file whose `size` + `modified_at` match its
  prior catalog row (and whose prior row was successfully content-indexed)
  reuses the stored keywords/`content_hash` instead of re-extracting. A
  per-file `reporter.tick()` reports progress and doubles as the cancel
  checkpoint.
- `DataSourceClient.aget_schemas` (`base.py`): forwards `prior_catalog` (like
  `progress_callback`) only to clients whose `get_schemas` accepts it —
  legacy clients are untouched.
- `ConnectionService.refresh_schema` (`connection_service.py`): loads the
  existing `ConnectionTable` rows *before* schema discovery (they already fed
  the upsert diff afterwards) and passes `{table_name: metadata_json}` as the
  prior catalog; re-raises `IndexingCancelled` so the runner finalizes the
  run as `cancelled`, not `failed`.

Skip-check correctness notes:

- `modified_at` is the full-precision UTC isoformat of `st_mtime`, and size is
  compared too, so a same-mtime-different-size edit still re-extracts
  (covered by `test_same_mtime_different_size_is_reextracted`).
- A prior row from a metadata-only run or a failed extraction has no
  `indexed: true` flag and never satisfies the skip.
- New files extract, deleted files drop out (the walk is still live).

## What this proves / regression notes

- Steady-state scheduled reindexing of an unchanged directory is now O(stat)
  instead of O(full pypdf re-extraction): 104.93s → 0.14s on the 1,100-file
  corpus, with a byte-identical catalog.
- Related suites pass: `tests/unit/test_network_dir_client.py`,
  `test_s3_client.py`, `test_schema_validation_file_fast_path.py`,
  `test_attach_and_index.py`, `test_grep_files.py` (110 tests) and
  `tests/e2e/test_connection_indexing.py` (6 tests, incl. cancel endpoint).
- Pre-existing, unrelated: `tests/e2e/test_network_dir_e2e.py`'s two tests
  fail identically with this change stashed (deep-search assertion), so they
  are not regressions from this loop.
- First-time indexing of a huge archive is unchanged (still sequential
  extraction) — by design; it happens once, in the background, and now shows
  progress and honors cancel. S3 has the same incremental opportunity
  (ETag-keyed) and can adopt the same `prior_catalog` kwarg later.
