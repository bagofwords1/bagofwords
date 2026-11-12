# File connector search & indexing — plan

Status: **Full text is implemented for network drives and S3** (revision 3).
Everything else in this plan is still a proposal. What was built, what was
measured, and what turned out different from the plan is in
[Implementation status](#implementation-status-revision-3) right below; the
runnable proof is `docs/feedback-loops/2026-11-11-file-fulltext-search.md`.

## Implementation status (revision 3)

**Built**

- Index tier `fulltext` for `network_dir` and `s3`, default for new
  connections. Existing connections keep their setting.
- Tables `file_index_files` / `file_index_sections` (migration `fileindex01`):
  Postgres `tsvector` + GIN, SQLite FTS5 (contentless).
- One tokenizer for both databases (`_fulltext.py`): numbers kept, light
  English stem, Hebrew particle prefixes.
- Sections located in `read_file` coordinates: PDF page, sheet, character
  offset (docx/pptx/email), byte offset (text/csv).
- `search_files` answers from the index in SQL and returns ranked files with
  where the words were found. A miss is final unless `deep=true`.
- Index runs are incremental (size+mtime) and resumable (committed every 25
  files); deleted files are pruned; leaving the tier removes the index.
- Files with no extractable text are recorded as `no_text` and reported.
- S3 keyword indexing now reuses unchanged objects (`prior_catalog`).

**Different from the plan**

- **Ranking on Postgres** weights each query word by how rare it is in the
  connection (inverse document frequency computed per query), because
  `ts_rank` alone ignores rarity. Any word may match; rarer words count more.
- **Quoted phrases and ID-like tokens are required matches.** A search for
  `PO-48213` returns only files that contain exactly that, not every file
  with another `PO-` number. Other joined words (`cost/benefit`) only boost.
- **Stored value names:** the Keywords tier keeps the value `content`; the form
  labels are Live / File list / Keywords / Full text (recommended).

**Corrections to earlier revisions of this plan**

- **OneNote is not a bug.** It has its own search tools (`search_notes`) which
  read the `"onenote"` key; `search_files` is never offered for it.
- **Drive / Microsoft 365 already always use the source's search.** Their
  catalogs hold no keywords, so `search_files` goes to the provider on every
  call, not only as a fallback. What remains open is moving Graph from
  `/search(q=)` to `/search/query`, which needs a tenant to verify against.
- **Digits in Keywords-tier body text were left alone.** It is a deliberate,
  tested choice (numeric tables would flood 50 keyword slots), and an ID that
  appears once would not make the top 50 anyway. Full text is the fix.

**Found and fixed on the way** (all three exist on `main`)

- Walking a large network drive spent half its time re-running the root
  policy check twice per file: 41 s → 5 s for 50k files.
- The network-drive connection test walked the whole share inside the save
  request (twice). It now probes 200 files.
- A manual reload whose crawl takes more than 5 minutes failed on Postgres at
  the end: the request's session sat idle in a transaction and the server
  dropped it. The session is now kept alive during the crawl.

**Not built yet:** per-user filtering, Semantic tier, Graph `/search/query`,
SharePoint on-prem / Documentum full text, size/time estimate in the form.

## Problem

File connectors (network drive, S3, SharePoint online/on-prem, OneDrive, Google
Drive, Documentum, OneNote, mail, git repos) are searched through a thin keyword
index that misses a lot:

- Each file gets **up to 50 keywords**: filename tokens plus the most frequent
  body words, taken from a prefix sample of ≤200k chars (`_keywords.py:40,126`,
  `network_dir_client.py:602`). PDFs stop at 200 pages. The text itself is not
  stored.
- Body tokens with **digits are dropped**, so IDs and invoice numbers inside
  documents are unsearchable (`_keywords.py:17-21`).
- Matching is **whole-word only**. Hebrew attaches prefixes to words
  (ב/ה/ל/ו/ש/מ/כ), so a search for "חוזה" does not match a file that says
  "בחוזה".
- `search_files` **loads the whole catalog into Python** and scores token
  overlap (`search_files.py:49-91,277`). It doesn't rank by relevance and
  returns no location inside the file. The agent gets file names only.
- Google Drive, Graph (SharePoint Online/OneDrive) and Documentum build
  **metadata only**. Their content is searched only through a live provider
  fallback that runs when the index finds nothing or `deep=true`
  (`search_files.py:321`). Graph uses the weak `/search(q=)` endpoint
  (`graph_drive_client.py:1291`).
- Catalog size is capped per connection by `max_catalog_objects`: default
  5,000 (10,000 for network drives), and at most 50,000 on the connectors that
  bound it (`configs.py:2653-3392`). Enterprise customers have 50k+ files.
- Per-user SharePoint on-prem / Documentum connections **skip the index on
  purpose** and always go live (`search_files.py:280`): the shared catalog was
  crawled as one identity and must not stand in for the caller's.
- Per-user catalogs (`user_connection_tables`) are refreshed **only when the
  user signs in**; the scheduled sweep skips them
  (`scheduled_reindex.py:132-136`). `search_files` does not read them.
- An indexing run has a **4 h hard timeout** and a 15 min idle timeout
  (`connection_indexing_service.py:203-204`), and a failed run keeps the prior
  catalog but not partial progress.
- S3 doesn't reuse `prior_catalog`, so it re-extracts every file on every run
  (`s3_client.py:707`).

## Goals

- Find the right files for an agent's question, including exact IDs, phrases
  and words deep in the body, in English and Hebrew.
- Scale to **50k+ files per connection** and **per-user access** without storage
  multiplying by the number of users.
- Store as little customer content as the chosen mode needs, and say plainly
  what each mode stores.
- Work on Postgres (production) and SQLite (local dev).
- Let admins choose the cost/quality trade-off per connection.

## Decision: indexing mode is a per-connection choice (a ladder)

`index_mode` already exists on most file connector configs
(`app/schemas/data_sources/configs.py`, values `none | metadata | content`,
defined in `_file_source_common.py:22-25`). Google Drive doesn't have it.
We turn it into a clear ladder; each mode includes everything below it.

| Form label | Stored value | Stored | Search capability | Rough size @ 50k files |
|---|---|---|---|---|
| **Live** | `none` | nothing | name match + live grep | 0 |
| **File list** | `metadata` | name, path, size, dates | names from our list; content through the source's own search where it has one | tiny |
| **Keywords** | `content` (unchanged) | ≤50 keywords per file | basic topic match | a few MB |
| **Full text** | `fulltext` (new) | every word of every **section**, with positions | exact words, phrases, IDs; ranked; location in the file | a few hundred MB to ~1 GB (estimate, to be measured) |
| **Semantic** | `semantic` (later) | full text + chunk embeddings | also matches by meaning | +~1–4 GB vectors; needs embedding provider |

Rules:

- **No rename of stored values.** `content` is compared in ~23 places across
  11 files (for example `schema_context_builder.py:1136`). It keeps its value
  and only gets the form label "Keywords". Existing connections don't change.
- **Defaults:**
  - network drive / S3 → Full text;
  - Drive / OneDrive / SharePoint Online → File list;
  - SharePoint on-prem / Documentum → Full text with a service account, File
    list with per-user auth (until the per-user phase ships).
- **The form shows,** for each mode, one line on what's stored and what search
  can do. After the first scan it shows an estimate (index size, build time).
  Semantic is not shown until it exists.
- **`search_files` stays one tool** and adapts to the connection's mode.

### What "Full text" stores — stated honestly

A full-text index holds every word and its position. The original text can be
largely rebuilt from it. So:

- The index is treated as **customer content**: same tenant isolation, deleted
  with the connection, covered by the same retention rules.
- The form says "stores a searchable index of the words in your files", not
  "stores nothing readable".
- Word positions are kept because phrase search needs them.
- What we still don't keep: the original files, their layout, images, or a
  second copy per user.

## Source search for Drive / Microsoft 365 (File list mode)

The code already treats these connectors as having native search
(`_NATIVE_SEARCH_TYPES`, `schema_context_builder.py:1043`), but
`search_files` only calls it as a fallback. Proposed (needs sign-off):

- In File list mode, `search_files` **always** runs the source's content
  search and merges it with name matches from our list.
- Graph moves from `/search(q=)` to `/search/query`: KQL filters, highlighted
  snippets, relevance ranking, results trimmed to the user.
- A per-user rate limiter (Graph allows ~10 requests/sec per user) and a clear
  "slow down" result the agent can act on.
- Drive returns ranked files with no snippets; the agent reads the top hits.

Without this, Drive and Microsoft 365 customers get no improvement unless they
switch to Full text, which means downloading every file.

## Full text mode — design

**Sections, not RAG chunks.** A file is split into sections: one PDF page, one
sheet, one slide, or ~3k chars of other text. Per section we store:

- `file_id`, `section_no`, `content_hash`;
- the location **in `read_file` coordinates**: PDF page, sheet name, or
  character offset into the extracted text, so the agent can read exactly that
  part (`file_tools.py:255-290` already supports page, `sheet` and `offset`);
- the full-text vector of the section's normalized terms.

**One tokenizer, in Python, for both databases.** We normalize text ourselves
and hand the databases pre-tokenized terms, so matching behaves the same on
Postgres and SQLite:

- lowercase, Unicode word split, numbers and ID-like tokens kept
  (`PO-48213` → `po-48213`, `po`, `48213`);
- light English stemming (reuse `ai/context/keyword_match.py:stem`);
- Hebrew: index each word and, when it starts with a one-letter prefix and at
  least 3 letters remain, also its prefix-stripped form. The same rule is
  applied to queries. To be validated on Hebrew test queries for false matches.

**Engine (no new service):**

- **Postgres:** `tsvector` column (`simple` config over our terms) + GIN index;
  ranking with `ts_rank_cd`.
- **SQLite:** FTS5 contentless table with `contentless_delete=1` (needs SQLite
  ≥ 3.43); ranking with `bm25()`.
- The two rank differently (Postgres has no built-in BM25). Quality is judged
  on Postgres; SQLite must return the same matches but not the same order.
- Sections of ~3k chars stay far below Postgres's limits (1 MB per `tsvector`,
  positions up to 16,383).

**Query semantics:**

- All words must match first; if nothing matches, retry with any word.
- Quoted text is a phrase.
- Files rank by their best section, plus a boost for filename/path matches
  (keep today's boost).

**Coverage:**

- Index all extracted text, not a prefix sample.
- Keep a sanity cap for pathological files (giant logs/exports) and flag those
  files as `partially_indexed` so the agent knows.
- Files with no extractable text (scanned PDFs, images) are flagged
  `no_text`, so "not found" isn't read as "doesn't exist".

**Building and updating the index:**

- **Resumable.** Sections are committed per file as the run goes. A run that
  times out or fails keeps what it finished; the next run skips files whose
  content hash is already indexed. A 50k-file first build will not fit in one
  4 h run on slow sources.
- Re-index a file only when it changes: size+mtime / etag / content hash. Reuse
  the existing `prior_catalog` path and add it to S3.
- Use provider change feeds where they exist (Graph `delta`, Drive
  `changes.list`) so frequent syncs stay cheap.
- Remove sections when a file is deleted at the source. A rename or move
  updates the path without re-extracting.
- Raise the `max_catalog_objects` ceiling for Full text connections and show
  "N of M files indexed" when the cap is hit.

**What the agent gets back from `search_files`:**

- Ranked files (id, name, path, modified, url), each with its **top 2–3
  sections**: location and the matched terms.
- **No live snippet fetch during search.** Downloading and extracting several
  files per search is too slow for an agent that searches many times per
  question. The agent calls `read_file` on the section it wants.
- `partially_indexed` / `no_text` / `indexed_at` notes where relevant.
- `grep_files` (network drive / S3 only) and `read_file` are the drill-down
  tools.

## Per-user access — last phase, gated on a security review

Today per-user connections bypass the shared index deliberately. Using the
index for them changes a security decision, so it ships last and only after
review. Design:

- Content is indexed **once per file** by the connection's crawl identity,
  keyed by file id + content hash. Connections with no crawl identity
  (personal OneDrive / Drive) cannot use Full text; they use source search.
- Per user we store only the **visible file ids** (the per-user catalog we
  already build).
- Search = shared section index, filtered by the caller's visible file ids.
- **Verify before returning.** For the top N results, check access live with
  the caller's own credentials (a metadata call) and drop anything denied. A
  stale list then cannot leak even a file name.
- **Refresh.** Per-user lists are refreshed on a schedule where we hold a
  refreshable credential, not only at sign-in. A list older than a set age is
  not used; search falls back to live.
- `read_file` keeps enforcing access live, as today.
- Admin opt-in per connection; default stays live search.

## Semantic mode — later, opt-in

Only build this if the test set shows that searches by meaning are missed.

- Chunk-level embeddings (~400 tokens, ~15–20% overlap), stored in pgvector on
  Postgres and sqlite-vec / in-process cosine on SQLite.
- **Hybrid score** as in OpenClaw: `0.7·cosine + 0.3·keyword`. Keyword scores
  are rescaled per query to the vector range. Then MMR for diversity and an
  optional recency boost on `modified_at`.
- Embeddings are cached by chunk hash, so unchanged text is never re-embedded.
  Falls back to Full text when no embedding provider is configured.
- Same access model as Full text.

## Work plan

1. **Test set and baseline.** 30–50 real queries with expected files (English
   and Hebrew), run against a large connection on Postgres. A script reports
   how many queries return the right file in the top 10. Measure today's
   search first.
2. **Quick fixes**
   - Use `prior_catalog` in S3 `get_schemas`.
   - Keep digits in body keywords (`_keywords.py`).
3. **Source search in File list mode** (if approved): always-on, Graph
   `/search/query`, rate limiter.
4. **Full text for shared connections.**
   - Section table, shared tokenizer, Postgres/SQLite FTS.
   - Resumable build, incremental updates, deletes.
   - `search_files` queries in SQL and returns ranked files + sections.
   - Network drive and S3 first, then SharePoint on-prem and Documentum with a
     service account.
5. **Form.** `fulltext` option, "Keywords" label for `content`, per-mode
   descriptions, defaults, size/time estimate, `index_mode` on Google Drive.
6. **Per-user filtering,** after security review.
7. **Measure** each step on the test set; decide on Semantic.

## Sandbox feedback loop

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md`; the doc lands in
`docs/feedback-loops/`.

**Loop A — deterministic, no credentials.** Seed a local folder as a
network-drive connection and drive everything through the real `search_files`
tool with pytest, on SQLite and with `--db=postgres`. Each case fails on
current code for the stated reason and passes after the change.

| Case | Seeded data | Fails today because |
|---|---|---|
| Deep match | long document, unique word on the last page | only the first 200k chars are sampled |
| ID in body | `PO-48213` appears only inside a file | body digits are dropped |
| Ranking | one file about a topic, 20 that mention it once | no relevance ranking |
| Location | term on page 7 of a PDF / on the second sheet | results carry no location |
| Phrase | exact phrase in one file, same words scattered in another | no phrase search |
| Hebrew prefix | file contains "בחוזה", query is "חוזה" | whole-word match only |
| File changed | edit a file, re-index | new word found, old word gone, unchanged files not re-read |
| File deleted | delete a file, re-index | it must disappear from results |
| Interrupted build | stop indexing halfway, run again | finished files are kept and not re-read |
| No text | scanned PDF | flagged `no_text`, not silently missing |
| Per-user (phase 6) | two users, one cannot see file X | X never appears for that user, not even its name; also after access is revoked without a re-sync |
| Scale | 50k small generated files | search must not load the whole catalog, and must stay under a time limit |

The headline number is the share of test-set queries with the right file in
the top 10, before and after.

**Loop B — optional, real LLM.** Ask the agent a question whose answer sits
deep in one file and check it finds that file within a few tool calls.

## Open questions

- Sign-off on source search in File list mode (recommended above).
- Sign-off on the per-user design and its security review.
- The Hebrew prefix rule: does it produce too many false matches? Decide from
  the Hebrew test queries.
- Real index size and build time at 50k files: measure in step 4 before
  setting defaults.

## Not doing

- **Storing all chunks with text + embeddings by default** (the Open WebUI /
  LibreChat model). Storage multiplies with per-user access, we'd hold copies of
  every document, and neither product scales to 50k files: Open WebUI rebuilds
  BM25 over every chunk on every query, and LibreChat makes one HTTP call per
  file.
- **A dedicated search engine** (Elasticsearch/OpenSearch) as a requirement. It
  adds another service for on-prem customers to run. It could be a pluggable
  backend later for the largest customers.
- **LLM file summaries ("file cards")** for now. Cost is low (~$15–30 one-off
  for 50k files with Claude Haiku 5.5, batch vs standard), but the value is
  unproven, and some customers can't send content to an LLM. Revisit after the
  test set.
- **A separate native-search tool.** Source search lives inside `search_files`.

## Changes in revision 2

- "No readable text stored" replaced by an honest statement: the full-text
  index is customer content.
- Stored value `content` is kept (label "Keywords") instead of being renamed.
- One Python tokenizer for both databases, with Hebrew prefix handling.
- Search returns section locations in `read_file` coordinates; no live snippet
  fetch.
- Index builds are resumable; scanned files are flagged.
- Per-user search moved to a last, security-gated phase with verify-before-
  return and scheduled list refresh; content is crawled by the connection's
  crawl identity.
- Source search for Drive / Microsoft 365 is a recommended decision instead of
  an open question.
- Catalog cap corrected: per-connection setting, default 5k–10k, max 50k.
- Baseline measurement moved to step 1; test cases added.

## Appendix: research notes

Sources were read from code (shallow clones) and official docs in Nov 2026.

**OpenClaw** (`openclaw/openclaw`, `extensions/memory-core`):

- Line-based markdown-aware chunks of ~400 tokens with 80 overlap.
- SQLite FTS5 + optional sqlite-vec; a Postgres variant uses pgvector +
  `pg_trgm`.
- Hybrid score `0.7·vec + 0.3·BM25` (BM25 rescaled per query), MMR (λ 0.7),
  30-day temporal decay; top 6 results, 700-char snippets.
- Keyword search requires all terms first, then retries with any term.
- Embedding cache keyed by chunk hash. Falls back to FTS-only when there's no
  embedding provider.
- Two agent tools: `memory_search` → `memory_get(path, lines)`.

**Hermes Agent** (`NousResearch/hermes-agent`):

- No RAG or embeddings in core.
- Files: ripgrep-backed `search_files` (content regex / glob, limit 50).
- Sessions: SQLite FTS5 (+ trigram for CJK) with "hydrated" context windows
  around hits.
- Semantic memory is left to plugins (Mem0, Honcho, qdrant…).

**OpenCode** (`sst/opencode`):

- No index or embeddings; live ripgrep for grep/glob.
- Search returns 100 results by default; reads are capped at 2000 lines / 50 KB.

**Open WebUI:**

- Character chunks of 1000 with 100 overlap; local `all-MiniLM-L6-v2`
  embeddings; Chroma by default (15 vector DBs supported); top_k 3.
- Hybrid search is off by default. When on, it builds BM25 in memory over
  **all chunks of the collection per query**.
- Stores the full text in SQL **and** chunk text + vectors, duplicated per
  knowledge base.
- Drive/OneDrive support is just a file picker that downloads files into its
  own pipeline. It doesn't use the provider's search.

**LibreChat + rag_api:**

- Chunks of 1500 with 100 overlap; LangChain pgvector table; OpenAI
  `text-embedding-3-small` by default.
- `file_search` sends **one `/query` per attached file** (k=5), then keeps the
  top 10.

**Glean:**

- Full crawl index (content, ACLs, people, activity) in a single-tenant
  deployment, hosted by Glean or in the customer's own cloud account.
- Hybrid lexical + per-customer-tuned embeddings + personalization graph.
- ACLs are synced at crawl time and filtered at query time; live fetch for
  freshness.
- Agentic search is run by its own trained planner model.

**Provider search APIs:**

- **Graph `/search/query`:**
  - KQL with path/filetype/date filters, highlighted snippets, relevance
    ranking.
  - Delegated calls are permission-trimmed; ~10 req/s per user (SharePoint
    search throttling).
  - App-only calls are not user-trimmed and need `region`.
- **SharePoint Server `/_api/search/query`:** KQL/FQL, `HitHighlightedSummary`,
  security-trimmed. Freshness depends on the farm's crawl schedule.
- **Drive `fullText contains`:** relevance-ordered and user-trimmed, but no
  snippets and no sorting.
- **Graph `/search(q=)`:** no filters, no snippets.
