# File connector search & indexing — plan

Status: proposal (not implemented)

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
- `search_files` **loads the whole catalog into Python** and scores token
  overlap (`search_files.py:49-91,277`). It doesn't rank by relevance and returns
  no snippets. The agent gets file names only.
- Google Drive, Graph (SharePoint Online/OneDrive) and Documentum build
  **metadata only**, so their content is searched only through a live provider
  fallback. That fallback runs when the index finds nothing or `deep=true`
  (`search_files.py:321`). Graph uses the weak `/search(q=)` endpoint
  (`graph_drive_client.py:1291`).
- Catalogs are **capped at 10k files** (`network_dir_client.py:194`), but
  enterprise customers have 50k+.
- Per-user SharePoint on-prem / Documentum connections **skip the index** and
  always go live (`search_files.py:280`). Per-user catalogs
  (`user_connection_tables`) are not read by `search_files`.
- Bugs:
  - OneNote writes keywords under `"onenote"`, which is missing from
    `_FILE_METADATA_KEYS` (`search_files.py:43`).
  - S3 doesn't reuse `prior_catalog`, so it re-extracts every file on every run
    (`s3_client.py:707`).

## Goals

- Find the right files for an agent's question, including exact IDs, phrases
  and words deep in the body.
- Scale to **50k+ files per connection** and **per-user access** without storage
  multiplying by the number of users.
- Avoid storing readable copies of customer documents by default.
- Work on both Postgres and SQLite (both are supported deployments).
- Let admins choose the cost/quality trade-off per connection.

## Decision: indexing mode is a per-connection choice (a ladder)

`index_mode` already exists on most file connector configs
(`app/schemas/data_sources/configs.py`, values `none | metadata | content`,
defined in `_file_source_common.py:22-25`). Google Drive doesn't have it.
We turn it into a clear ladder; each mode includes everything below it.

| Mode (form label) | Stored | Search capability | Rough size @ 50k files |
|---|---|---|---|
| **Live** (`none`) | nothing | name match + live grep | 0 |
| **File list** (`metadata`) | name, path, size, dates | name/path search (see open question on source search) | tiny |
| **Keywords** (`keywords`; today's `content`) | ≤50 keywords per file | basic topic match | a few MB |
| **Full text** (`fulltext`, new) | searchable terms + positions per **section**, no readable text | exact words, phrases, IDs; ranked; section location | a few hundred MB to ~1 GB |
| **Semantic** (`semantic`, new, later) | full text + chunk embeddings | also matches by meaning | +~1–4 GB vectors; needs embedding provider |

Rules:

- **Migration:** existing `content` connections map to `keywords`, so nothing
  changes on upgrade.
- **Defaults:**
  - network drive / S3 → Full text;
  - Drive / OneDrive / SharePoint Online → File list;
  - SharePoint on-prem → Full text with a service account, File list with
    per-user auth.
- **The form shows,** for each mode, one line on what's stored and what search
  can do. After the first scan, it shows an estimate (index size, build time).
- **`search_files` stays one tool** and adapts to the connection's mode. There
  are no hidden fallbacks between modes.

## Full text mode — design

**Sections, not RAG chunks.** A file is split into sections: one PDF page, one
sheet, one slide, or ~3k chars of other text. Per section we store:

- `file_id`, `section_no`, location (page / sheet / char range);
- a full-text vector of the section's terms;
- `content_hash`.

We don't store readable text by default. A per-connector option may store it
where live reads are slow or rate-limited (OneNote already stores text).

**Engine (no new service):**

- **Postgres:** `tsvector` column + GIN index. Use the `simple` config, since
  there is no Hebrew stemmer; optionally add `pg_trgm` for partial matches.
- **SQLite:** FTS5 contentless table (`content=''`), `unicode61` tokenizer.
- Ranking is BM25 / `ts_rank` at section level. Files rank by their best
  section, with a small boost for filename/path matches (keep today's boost).
- Numbers are kept as terms.

**Coverage:**

- Index all extracted text, not a prefix sample.
- Keep a sanity cap for pathological files (giant logs/exports) and flag those
  files as `partially_indexed` so the agent knows.
- Sections also keep each entry under Postgres's 1 MB `tsvector` limit.

**Incremental updates:**

- Re-index a file only when it changes: size+mtime / etag / content hash. Reuse
  the existing `prior_catalog` path and add it to S3.
- Use provider change feeds where they exist (Graph `delta`, Drive
  `changes.list`) so frequent syncs stay cheap.
- Remove sections when a file is deleted at the source.

**Per-user access:**

- Content is indexed **once per file**, keyed by file id + content hash, no
  matter how many users can see it.
- Per user we store only the **visible file ids**. We already build this list
  at sign-in (per-user catalog sync).
- Search = shared section index filtered by the caller's visible file ids.
- Snippets and `read_file` always fetch live with the **user's own
  credentials**. So a stale ACL list can at worst reveal a file name, never
  content.
- Open question: who reads content the first time — a service account, or the
  first user who can see the file (later users reuse it via content hash)?

**What the agent gets back from `search_files`:**

- Ranked files (id, name, path, modified, url) plus their **top 2–3 sections**:
  location, and a snippet fetched live from the source for the top N files.
- A `partially_indexed` / `indexed_at` note where relevant.
- The agent then drills in with `grep_files` / `read_file` on the few files that
  look right. This is the "search, then read the exact lines" loop used by
  OpenClaw, Hermes and OpenCode.

## Semantic mode — later, opt-in

Only build this if the test set shows that searches by meaning are missed.

- Chunk-level embeddings (~400 tokens, ~15–20% overlap), stored in pgvector on
  Postgres and sqlite-vec / in-process cosine on SQLite.
- **Hybrid score** as in OpenClaw: `0.7·cosine + 0.3·keyword`. Keyword scores
  are rescaled per query to the vector range. Then MMR for diversity and an
  optional recency boost on `modified_at`.
- Embeddings are cached by chunk hash, so unchanged text is never re-embedded.
  Falls back to Full text when no embedding provider is configured.
- Same per-user model: embed once per file, filter by visible file ids.

## Work plan

1. **Quick fixes**
   - Keep digits in body keywords (`_keywords.py`).
   - Add `"onenote"` to `_FILE_METADATA_KEYS`.
   - Use `prior_catalog` in S3 `get_schemas`.
   - Raise / remove the 10k `max_catalog_objects` cap.
2. **Test set.** 30–50 real queries with expected files, run against a large
   connection, with a script that reports recall@10. Measure today's search as
   the baseline.
3. **Mode ladder in config + form.**
   - Add `keywords` / `fulltext` (and reserve `semantic`) and migrate
     `content` → `keywords`.
   - Add `index_mode` to Google Drive.
   - Write the per-mode descriptions and defaults.
4. **Full text index.**
   - Section table + Postgres/SQLite FTS, incremental updates, deletes.
   - Change `search_files` to query in SQL and return ranked files + sections.
5. **Per-user filtering.** Shared content index + per-user visible file ids, so
   per-user SharePoint on-prem / Documentum can use the index.
6. **Measure** keywords vs full text on the test set; decide on Semantic.

## Open questions

- **Source search in File list mode.** Should `search_files` call the
  provider's own content search for Drive / M365 (and SharePoint on-prem when
  the farm has search)?
  - If not, their content is only searchable through Full text, which means
    downloading every file and hitting their rate limits.
  - Research notes are below. Graph `/search/query` and SharePoint on-prem
    `/_api/search/query` fit agentic use well. Drive returns no snippets.
- Who reads content first for per-user connections (service account vs first
  user)?
- Is storing readable section text acceptable as an opt-in, and for which
  connectors?

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

## Appendix: research notes

Sources were read from code (shallow clones) and official docs in Nov 2026.

**OpenClaw** (`openclaw/openclaw`, `extensions/memory-core`):

- Line-based markdown-aware chunks of ~400 tokens with 80 overlap.
- SQLite FTS5 + optional sqlite-vec; a Postgres variant uses pgvector +
  `pg_trgm`.
- Hybrid score `0.7·vec + 0.3·BM25` (BM25 rescaled per query), MMR (λ 0.7),
  30-day temporal decay; top 6 results, 700-char snippets.
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

**Provider search APIs** (for the open question):

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
