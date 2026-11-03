# Feedback Loop — “if the files are in GB, would it work?”

Network-directory and S3 text scans should find late-file matches without loading an entire file. A scan budget must produce explicit partial coverage and a byte cursor, rather than skip the file for its size. This change covers streaming grep, network-directory live text search, and bounded text keyword sampling. It does not add large-document parsing or change other connectors' scanning engines.

## Root cause (validated)

Baseline: `94394a0aa55d45bcd252964d582ec091c7aeeace`.

- `backend/app/data_sources/clients/_grep_common.py:255` rejected candidates above `max_bytes_per_file`; line 278 decoded and split the complete accepted file.
- Baseline `network_dir_client.py:757` called `path.read_bytes()`; baseline `s3_client.py:542` called `Body.read()` without a length.
- The old cursor contained a line number, requiring a reread from the beginning. It did not bind continuation to the current file version.
- Network-directory text indexing read the whole file before slicing its text; S3 similarly downloaded the complete object before taking its keyword sample.

The deterministic reproduction observed four failures before implementation: both connectors reported `too_large`, and neither returned the cursor needed to continue a budget-limited scan.

## Environment

Use Python 3.13 and the repository's locked backend environment:

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true
export BOW_DATABASE_URL=sqlite:///db/app.db
mkdir -p db
```

The implementation was verified in an isolated Git worktree on branch `codex/large-file-streaming`. Local runs used the existing Python 3.13 virtual environment from the main checkout against this worktree's sources. No real S3 credentials are required or used. Unit tests use `--confcutdir=tests/unit` because these connector tests need no database fixtures; the end-to-end test uses the normal migration/fixture setup.

## Loop A — deterministic reproduction

To reproduce the original failure without disturbing the current checkout, run from the repository root containing the new tests:

```bash
repro_dir=$(mktemp -d)
git archive 94394a0aa55d45bcd252964d582ec091c7aeeace | tar -x -C "$repro_dir"
cp backend/tests/unit/test_large_file_streaming.py "$repro_dir/backend/tests/unit/"
cd "$repro_dir/backend"
uv sync --frozen --extra dev
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/unit/test_large_file_streaming.py --confcutdir=tests/unit \
  -k 'large_text_resumes or changed_file_cannot' -q
```

Observed baseline:

```text
AssertionError: assert not [{'file_id': 'events.log', 'reason': 'too_large'}]
AssertionError: assert first['next_cursor']
4 failed
```

The tests use small files with deliberately smaller scan budgets, so the contract is exercised quickly. They cover both a real filesystem client and the real S3 client with its external transport stubbed.

## The fix

- `backend/app/data_sources/clients/_streaming_grep.py:67`: shared scanner reads 64 KiB chunks, matches complete UTF-8 lines, retains bounded context, and uses regex timeouts. Cursors carry byte position, line number, context, scope, and file identity.
- `backend/app/data_sources/clients/network_dir_client.py:701`: resumes through `seek()`; checks device/inode/size/nanosecond modification identity before and after scanning.
- `backend/app/data_sources/clients/s3_client.py:471`: streams and closes response bodies, binds requests with ETag/version, resumes with Range, and rejects ignored range responses. A mutable S3 `null` version also checks ETag.
- `backend/app/data_sources/clients/base.py:389`: async cancellation signals the worker to stop at its next scan checkpoint.
- Network-directory content search uses the scanner and reports budget exhaustion through `SearchStopped`. S3 retains catalog search plus explicit deep grep.
- Text keyword indexing reads at most 200,000 bytes, records `content_coverage` and `sample_max_bytes`, and describes the index as sampled. Network-directory cache versioning refreshes older samples.
- Tool output includes bytes read, complete/incomplete coverage, stop reason, and skip reasons. Catalog truncation is explicit rather than presented as an exhaustive scan.

Run the regression set from the fixed checkout's `backend` directory:

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/unit/test_large_file_streaming.py \
  tests/unit/test_grep_files.py \
  tests/unit/test_network_dir_client.py \
  tests/unit/test_network_dir_incremental_index.py \
  tests/unit/test_s3_client.py \
  tests/unit/test_email_files.py \
  tests/unit/test_file_tools.py \
  --confcutdir=tests/unit -q --disable-warnings
```

Observed after implementation on 2026-11-03:

```text
240 passed, 153 warnings in 17.80s
```

The original four failing cases pass. Additional coverage includes adjacent matches across result pages, UTF-8/chunk boundaries, long Unicode context cursors, binary detection, oversized lines, regex timeout, cancellation and stream closure, changed objects, ignored S3 ranges, bounded indexing, and incomplete live search.

## Loop B — full application path, no external credentials

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/rbac/test_file_tools_run_scope.py \
  -k large_file_grep -q --disable-warnings
```

Observed:

```text
2 passed, 13 deselected, 965 warnings in 6.50s
```

Both admin and member go through real API-created agents, reports, the agent loop, tool resolver, and filesystem client. Only the LLM boundary is scripted. The planner receives `byte_budget` and `coverage incomplete` instead of an apparently exhaustive empty result.

## Loop C — actual GB-scale files and allocation measurement

```bash
PYTHONPATH=. uv run python scripts/bench_large_file_streaming.py --mb 64 --source network
PYTHONPATH=. uv run python scripts/bench_large_file_streaming.py --mb 64 --source s3
PYTHONPATH=. uv run python scripts/bench_large_file_streaming.py --mb 1024 --source network
PYTHONPATH=. uv run python scripts/bench_large_file_streaming.py --mb 1024 --source s3
```

The harness generates ordinary newline-delimited text, places the sole marker at EOF, follows cursors to completion, checks the exact marker line, and asserts peak traced scan allocations below 32 MiB. Temporary files are removed automatically. The S3 transport reads the same real disk file, honoring range offsets and conditional identity; it does not copy the object into a Python byte string.

| Source | File size | Calls | EOF hits | Peak traced scan allocation | Time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Network directory | 64 MiB + marker | 4 | 1 | 0.160 MiB | 0.28 s |
| S3 client / local transport | 64 MiB + marker | 4 | 1 | 0.160 MiB | 0.22 s |
| Network directory | 1 GiB + marker | 54 | 1 | 0.164 MiB | 7.50 s |
| S3 client / local transport | 1 GiB + marker | 54 | 1 | 0.192 MiB | 6.82 s |

Each final 1 GiB run read 1,076,346,893 bytes across pages for a 1,073,741,837-byte file: bounded read-ahead is reread at continuation boundaries. Timings vary with concurrent work and disk cache. Allocation figures use `tracemalloc` after imports/warm-up; they are not total process RSS, native-library allocations, or actual AWS transfer measurements.

## What this proves / remaining limits

- GB-sized text is searchable across bounded calls; file size alone no longer excludes it. Small-file, document, and email regression tests pass.
- Default text budget is 20,000,000 bytes per file per call, with a 100,000,000-byte aggregate read budget. Budgets are soft at line/context boundaries and can overrun by bounded read-ahead. Whole-file parsing limits remain separate.
- Lines above 1 MiB and regex timeouts produce explicit skips and incomplete coverage. This is not a claim of full grep compatibility for arbitrarily long minified JSON records. Text scanning uses UTF-8 and LF/CRLF lines.
- The changed cursor format rejects old cursors; restart those sweeps. A cursor checks its current file, not a transactionally frozen directory/bucket snapshot.
- Listing is bounded by connector catalog limits for grep. When enumeration is capped, narrow the folder/prefix/pattern; a byte cursor does not page past the catalog limit.
- Cancellation is cooperative at scan checkpoints. A blocking filesystem/HTTP read must return before its stream can close. Directory listing and document extraction still have their existing I/O characteristics.
- Email continues through bounded extraction, capped at 20 MB or the smaller connection cap. Large PDF/Office extraction, streaming dataset analysis, compressed-file seeking, and other connectors are outside this change.
- Live AWS IAM, network behavior, billing, and throttling were not tested. All S3 verification is at the SDK transport boundary; no live cloud performance claim is made.
- No frontend files or database schema changed. `git diff --check` passed. The initial broader test invocation needed `BOW_DATABASE_URL` set; after correcting that environment setup, the focused suites passed with no unresolved test failures.
