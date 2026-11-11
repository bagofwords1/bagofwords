# Feedback Loop — "the agent can't find a file by what's inside it"

On a network drive or S3 connection with tens of thousands of files, the agent
finds files by name and by broad topic, but not by a specific word, ID or
phrase inside them — and when it misses, it falls back to a live scan that
runs into its time limit. The claim validated here: a full-text index tier
(`index_mode=fulltext`) makes every word of every readable file findable,
ranked, with the page or sheet it is on, at 50,000 files.

Design and status: `docs/design/file-search-indexing-plan.md`.

## Root cause (validated on `main` @ 86150bc6e)

- The keyword tier keeps **at most 50 keywords per file**, chosen by frequency
  from the first 200k characters (PDFs: first 200 pages)
  (`backend/app/data_sources/clients/_keywords.py:40`,
  `network_dir_client.py:602`). A word used once, or anything past the sample,
  is not in the index.
- Body tokens containing **digits are dropped** on purpose
  (`_keywords.py:17-21`, pinned by `tests/unit/test_keywords_numeric.py`), so
  IDs inside files are never indexed.
- `search_files` loads **every catalog row of the connection** and scores it
  in Python (`search_files.py:277`): 3.6–4.6 s per search at 50k files.
- A zero-hit search falls through to a **live scan** bounded at ~25 s
  (`search_files.py:321`), which cannot cover 50k files.

Found on the way, all on `main` (fixed in the same change):

- Walking a share re-ran the root-policy check twice per file
  (`network_dir_client.py` `_root()` → `check_network_dir_root`): 41 s to list
  50k files; 5 s after.
- The network-drive connection test listed the whole share inside the save
  request, twice: 86 s to create a connection on the 50k share; 0.2 s after.
- A reload whose crawl outlasts Postgres' `idle_in_transaction_session_timeout`
  (5 minutes on the request engine, `settings/database.py:461`) fails at the
  end with `connection is closed`: the caller's session waits idle in a
  transaction while each connection is crawled in its own session
  (`data_source_service.py` `refresh_data_source_schema`).

## Loop A — deterministic benchmark (no external services)

A generated file share with planted answers, indexed and searched through the
real stack: `DataSourceService.refresh_data_source_schema` →
`NetworkDirClient.get_schemas` → the `search_files` tool.

```bash
cd backend
export BOW_DATABASE_URL=postgresql://bow:bow@localhost:5432/bench   # or sqlite:///db/bench.db
uv run python scripts/file_search_bench.py --corpus /data/corpus_50k \
    --files 50000 --seed 11 --mode content  --label before --reindex --out before.json
uv run python scripts/file_search_bench.py --corpus /data/corpus_50k \
    --mode fulltext --label after --reindex --out after.json     # fresh database
uv run python scripts/file_search_bench.py --compare before.json after.json
```

The corpus (`scripts/file_search_corpus.py`): 50,000 files, 184 MB — text,
Markdown, CSV, DOCX, PDF, XLSX, Hebrew DOCX — and 135 queries, 15 of each kind:

| Kind | What is planted |
|---|---|
| `name` | distinctive words in the file's name (control group) |
| `topic` | one file about a subject among 25 that mention it once |
| `rare_word` | a word used once, mid-document |
| `sheet` | a code on the third sheet of a workbook |
| `id_in_body` | an ID like `SKU-88331` that exists only inside a file |
| `question` | a natural-language question; its distinctive words are in one file |
| `phrase` | an exact phrase; six other files hold the same words apart |
| `deep` | a sentence after 260k characters of text / on page 231 of a 240-page PDF |
| `hebrew` | a Hebrew word searched bare; the file has it with a particle attached |

### Observed — before (`main`, keyword tier, Postgres 16)

```
== before (keywords, main) · postgresql · 50000 files · index build 173.8s ==
kind           n   top1   top5  top10    mrr    p50ms    p95ms  live
overall      135    33%    33%    38%   0.34     4634    29638    62
deep          15     0%     0%     0%   0.00    28750    30211    15
hebrew        15     0%     0%     0%   0.00    28647    28878    15
id_in_body    15     0%     0%     0%   0.00     4626    29677     5
name          15   100%   100%   100%   1.00     4465     4933     0
phrase        15     0%     0%     0%   0.00    28812    29230    12
question      15     0%     0%     0%   0.00     3632     4244     0
rare_word     15   100%   100%   100%   1.00    29298    30014    15
sheet         15     0%     0%    47%   0.07     3635     3905     0
topic         15   100%   100%   100%   1.00     3710     4170     0
```

`live` = searches that ended in a live scan. `rare_word` reaches 100% only
through those scans, at ~29 s each.

### Observed — after (this branch, full-text tier, Postgres 16)

```
== after (full text) · postgresql · 50000 files · index build 315.7s ==
kind           n   top1   top5  top10    mrr    p50ms    p95ms  live
overall      135   100%   100%   100%   1.00       83      343     0
deep          15   100%   100%   100%   1.00       79       88     0
hebrew        15   100%   100%   100%   1.00       80       98     0
id_in_body    15   100%   100%   100%   1.00       75       89     0
name          15   100%   100%   100%   1.00      115      149     0
phrase        15   100%   100%   100%   1.00       94      117     0
question      15   100%   100%   100%   1.00      343      371     0
rare_word     15   100%   100%   100%   1.00       82       98     0
sheet         15   100%   100%   100%   1.00       81       89     0
topic         15   100%   100%   100%   1.00       86      117     0
```

![benchmark](../../media/pr/file-fulltext-search/benchmark.png)

| | Before | After |
|---|---|---|
| Right file in the top 10 | 38% (51 / 135) | 100% (135 / 135) |
| Right file first | 33% | 100% |
| Median search time | 4.6 s | 83 ms |
| 95th percentile | 29.6 s | 0.34 s |
| Searches that fell back to a live scan | 62 | 0 |
| Hit points at the right page / sheet (where known) | — | 23 / 23 |
| First index build | 174 s | 316 s |
| Re-index, nothing changed | 84 s | 43 s |
| Index size in Postgres | 77 MB (catalog) | 77 MB + 297 MB (243 MB sections, 54 MB files) |

Raw results: `media/pr/file-fulltext-search/benchmark-{before,after}.json`.

SQLite (400-file corpus, same query kinds): 36 / 36 first-ranked, 8 ms median.

## Loop A′ — regression tests

```bash
cd backend
TESTING=true uv run pytest tests/e2e/test_file_fulltext_search.py -m e2e --db=sqlite     # 11 passed
TEST_DATABASE_URL=postgresql://… TESTING=true \
  uv run pytest tests/e2e/test_file_fulltext_search.py -m e2e --db=postgres              # 11 passed
TESTING=true uv run pytest tests/unit/test_fulltext_tokenizer.py tests/unit/test_s3_client.py \
  tests/unit/test_network_dir_client.py                                                   # pass
```

`tests/e2e/test_file_fulltext_search.py` drives POST /connections → the
background index run → the `search_files` tool, one test per promise: any
word incl. IDs and numbers; text past 400k characters with a usable byte
range; PDF page and workbook sheet; ranking (subject before mention, phrase
before scatter); Hebrew particles; changed / unchanged / deleted files; an
interrupted run resumes; files without text are reported; leaving the tier and
deleting the connection remove the index; the search runs in SQL without
loading the catalog.

## Loop B — live confirmation (real LLM, Claude Haiku 5.5)

Two full stacks on Postgres, same 50k-file share, same model, same three
questions typed into the chat: `main` with Indexing = "Contents (searchable)",
and this branch with Indexing = "Full text". The API key was supplied through
a private file, never committed or logged.

| Question | Before (`main`) | After (this branch) |
|---|---|---|
| "Which document contains the annex called quonhulgorv, and what does that annex govern? Give me the file name and the page number." | Not found after **2 m 46 s** — 6 tool calls (two live scans of ~29 s, two greps of 45–70 s) | **9 s** — `master_agreement_001.pdf`, page 231 of 240, "governs termination of the reseller schedule" — 2 tool calls (0.2 s + 0.3 s) |
| "Find the file that mentions SKU-88331 and tell me what it says about it." | Not found after **1 m 53 s** — 3 tool calls (two live scans timed out) | **7 s** — `procurement/orders/order_batch_001.pdf`, quotes the sentence — 2 tool calls |
| "What is the penalty for late delivery of sarnjaskrund components by Tulkpraxzor? Which file says so?" | Not found after **51 s** — 12 tool calls (read 10 unrelated contracts) | **10 s** — "two percent per week", `procurement/vendors/vendor_terms_000.txt` — 4 tool calls |

Tool calls as recorded in `tool_executions` (after):

```
Annex Quonhulgorv Lookup | search_files | 0.2s | Found 1 file(s) matching 'quonhulgorv' (full-text index, …)
Annex Quonhulgorv Lookup | read_file    | 0.3s | Read pages 231-231 of 240 from legal/archive/master_agreement_001.pdf
SKU-88331 Lookup         | search_files | 0.1s | Found 1 file(s) matching '"SKU-88331"' (full-text index, …)
SKU-88331 Lookup         | read_file    | 0.1s | Read pages 1-1 of 1 from procurement/orders/order_batch_001.pdf
```

Before (same table, `main`):

```
Annex Quonhulgorv Lookup | search_files | 29.8s | Found 0 file(s) matching 'quonhulgorv' (live scan) … reached its time limit
Annex Quonhulgorv Lookup | grep_files   | 69.5s | 0 match(es) in 0/500 file(s) … coverage incomplete
SKU-88331 Lookup         | search_files | 29.7s | Found 0 file(s) matching 'SKU-88331' (live scan) … reached its time limit
```

| | Before | After |
|---|---|---|
| Question 1 | ![q1 before](../../media/pr/file-fulltext-search/q1-before.png) | ![q1 after](../../media/pr/file-fulltext-search/q1-after.png) |
| Search card | — | ![card](../../media/pr/file-fulltext-search/q1-after-search-card.png) |
| Question 2 | ![q2 before](../../media/pr/file-fulltext-search/q2-before.png) | ![q2 after](../../media/pr/file-fulltext-search/q2-after.png) |
| Question 3 | ![q3 before](../../media/pr/file-fulltext-search/q3-before.png) | ![q3 after](../../media/pr/file-fulltext-search/q3-after.png) |
| Settings form | ![form before](../../media/pr/file-fulltext-search/form-before.png) | ![form after](../../media/pr/file-fulltext-search/form-after.png) |

In the live app the first full-text build of the 50k share took 385 s
(`connection_indexings.stats_json.fulltext`: 50,000 files, 129,177 sections,
all `indexed`).

## The fix

- `backend/app/data_sources/clients/_fulltext.py` — tokenizer, query parser,
  section splitters. `_fulltext_extract.py` — one file → sections, shared by
  the network drive and S3 clients.
- `backend/app/models/file_index.py`, `alembic/versions/fileindex01_…` — the
  two index tables; Postgres `tsvector` + GIN, SQLite FTS5.
- `backend/app/services/file_index_store.py` — write / prune / search for both
  databases, and the sink that commits sections while the client walks.
- `network_dir_client.py`, `s3_client.py` — `get_schemas(section_sink=…)`;
  S3 also reuses unchanged objects in the keyword tier.
- `connection_service.py` — opens the sink, prunes, records stats, removes the
  index on tier change and on delete; keeps the session alive during a crawl.
- `search_files.py` — answers from the index; hits carry `matches` (page /
  sheet / offset + the `read_file` arguments) and `index_status`.
- `configs.py` — Indexing options Live / File list / Keywords / Full text
  (default for new network-drive and S3 connections).
- `frontend/components/tools/SearchFilesTool.vue` — shows where the words were
  found and which index answered; `locales/*.json` — the new strings.

## What this proves / limits

- On this corpus, every query whose words are in a file finds that file first,
  in well under half a second, at 50k files, on Postgres and SQLite.
- **The corpus is generated.** Planted answers use invented words, so each has
  one right file; real shares have near-duplicates and vaguer questions. The
  numbers show the mechanism works, not what recall will be on a customer's
  share. A set of real queries is still the next step in the plan.
- **Not helped:** a question worded differently from the file (synonyms,
  paraphrase), and scanned PDFs (no OCR — they are reported as `no_text`).
  Those are the Semantic tier and an OCR option in the plan.
- **Cost:** the first build is ~1.8× slower than the keyword tier and the
  index is ~1.6× the size of the extracted text (297 MB for 185 MB here).
- **Hebrew:** particle handling is covered by 15 benchmark queries and a
  regression test; false matches on real Hebrew text were not measured.
- A `.docx` longer than 200k characters is indexed in full, but `read_file`
  still returns its first 200k characters (an existing limit of the read path).

### Pre-existing failures seen while testing (not caused by this change)

Full unit suite (`pytest tests/unit -n 8`, SQLite) in this sandbox:

| | passed | failed | errors |
|---|---|---|---|
| `main` @ 86150bc6e | 15,129 | 200 | 1 |
| this branch | 15,160 | 200 | 1 |

The failing set is **identical** on both (compared test id by test id): 167 in
the SharePoint server tests, 30 in the OBO tests, and five that need
`libodbc` or a real TLS chain. The SharePoint ones fail on
`assert session.verify is True` because the sandbox sets a custom CA bundle
through the environment. The same goes for three e2e tests that use the fake
SharePoint server (`test_connection_file_browse.py::test_contract_sharepoint_server`,
`test_email_attachments_index_search.py` ×2): they fail on `main` too.
