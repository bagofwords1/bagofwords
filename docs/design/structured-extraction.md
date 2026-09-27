# Structured Extraction: typed, grounded fields as the output of an agent run

Status: **brainstorm / proposal**. Nothing is implemented. The code references below describe
today's code that this design reuses.

## 1. The pitch

Teams already use BOW agents to *read* data: SQL sources, SharePoint/Drive/S3/Documentum
files, email. Everything the agent returns today is either a table (`create_data` → Step) or
free text (`assistant_message`). There is no way to say:

> "For every contract in this SharePoint folder, give me `counterparty`, `annual_value`,
> `currency`, `renewal_date`, `auto_renew` (yes/no). Use our definitions of *annual value*.
> Save it as a table, show me where each value came from, and re-run it when new contracts
> land."

That is **Structured Extraction**. The user defines an **output schema** (a list of fields). The
agent does its normal work: it reasons, reads documents, runs queries and applies the agent's
instructions. It ends by emitting **one validated record per subject** instead of prose. The
records land in a governed, queryable table, with evidence for every field.

**The core idea:** the analysis stays agentic, and only the output is strict.

- This is different from the IDP products (Reducto, Extend, Azure CU, Bedrock DA). They run a
  fixed parse → extract pipeline over one document.
- Our record can combine evidence from a document and a SQL query ("revenue stated in the board
  deck vs revenue in the ERP"). The agent's instructions also define terms such as "revenue",
  which the IDP products don't have.
- No IDP product does both of these things, and they are our existing strengths.

## 2. What the market does (condensed)

Surveyed: Reducto, Extend, LlamaExtract, Unstructured, Unstract, Sensible, Instabase, Rossum,
V7 Go, Hebbia Matrix, Box Extract, SharePoint autofill columns, Azure Content Understanding,
Google DocAI custom extractor, AWS Bedrock Data Automation, Databricks `ai_extract` / Agent
Bricks, Snowflake `AI_EXTRACT`, BigQuery `AI.GENERATE_TABLE`, Airtable/Notion AI fields and
Clay. The common patterns are:

| Pattern | Who | Take it? |
|---|---|---|
| The schema is JSON Schema, and **the field description is the prompt** | nearly everyone | **Yes.** This is the core UX. |
| Natural language or sample files suggest a schema | SharePoint, Databricks, LlamaExtract, Box, Hebbia | **Yes**, in v2 ("suggest fields from 10 files"). |
| Each field has a *method*: extract (verbatim), classify (enum), or derive/generate (inferred) | Azure CU, Bedrock DA, Google, Instabase | **Yes.** Method decides whether a value can be grounded and how it is checked. |
| Opt-in wrapping of each value as `{value, citations[{page,bbox,text}], confidence}` | Reducto, Databricks, LlamaExtract, Azure | **Yes**, with evidence carried as a quote, not a bounding box, in v1. |
| Confidence routes low scores to a review queue and passes the rest through automatically | Rossum, Box, Instabase, Unstract | **Yes**, driven by categorical status plus rules, not raw scores (see §5.4). |
| Deterministic validation rules on top of LLM output | Sensible (JsonLogic), Rossum business rules, Instabase | **Yes.** They are cheap and catch arithmetic and date-order errors. |
| Ground-truth eval set, then an optimizer agent rewrites field descriptions, then a new version | Extend Composer, Bedrock DA optimizer, Databricks "Fix with Genie" | **Yes.** It maps onto our evals and self-improving loop. |
| Reviewer corrections become eval cases automatically | Extend | **Yes.** It is cheap once review exists. |
| Grid UX: rows are documents, columns are fields; click a cell to see the source | Hebbia, V7, Airtable, Clay | **Yes.** This is the run-results view. |
| Output is stored on the file (Box metadata, SharePoint columns) or as a warehouse table | Box, SharePoint / Snowflake, Databricks | Table first. Writing to SharePoint columns comes later. |
| Test a prompt on N sample files before saving; preview the cost; re-run only stale or errored rows | SharePoint, Airtable | **Yes.** These are small touches that users love. |

Pitfalls the market has already hit. We should design around them from the start:

1. **Strict JSON mode and native citations don't mix.** Anthropic returns a 400 if you combine
   them. So evidence must be a *schema field* (`quote`, `page`), which we verify ourselves.
2. **Answer-before-reason key order hurts accuracy**, and strict formats hurt reasoning
   ("Let Me Speak Freely?", EMNLP'24). The fix is "reason freely, *then* format", which is
   exactly what an agent loop that ends in a schema-bound tool call does.
3. **Self-reported confidence and logprobs are poorly calibrated** (AUROC ≈ 0.7 on invoices).
   Never show a bare "0.97". Use categorical signals (Sensible: found, not found, ambiguous,
   multiple candidates), quote verification and rule checks instead.
4. **Strict schemas are a subset of JSON Schema.** OpenAI requires every field to be `required`,
   so optional fields become nullable. Neither OpenAI nor Anthropic supports
   `minimum`/`maxLength`/`pattern`. So compile to the provider's subset and **validate the full
   schema ourselves afterwards**.
5. **One LLM call per field re-reads the document every time** (Airtable's cost trap). Extract
   all fields in one pass, and group them by where they sit in the document.
6. **Schema edits orphan eval history** (Bedrock DA). Give every field a stable ID, separate from
   its display name.
7. **Auto-rerun cascades** (Airtable). Triggers must be idempotent on
   `(schema_version, file, etag)`.
8. **Don't learn from auto-approved output** (Rossum). Only human-corrected records become eval
   cases.

## 3. What we have to build on

| Need | Existing piece | Ref |
|---|---|---|
| Native tool calling with an arbitrary JSON Schema | PlannerV3 sends `ToolSpec(name, description, input_schema)` built from a catalog of `ToolDescriptor`s | `ai/agents/planner/prompt_builder_v3.py:33-48`, `planner_v3.py:174` |
| Validate arguments and retry with field-level errors | `ToolRunner.run` validates with `input_model`, then returns `Validation failed: a.b: msg` and retries up to `max_validation_failures` | `ai/runner/tool_runner.py:111-200` |
| Read documents with page numbers | `read_file` (`page_range`, `as_images`), `extract_pdf_pages_text()` for per-page text, vision fallback for scans | `ai/tools/implementations/read_file.py:218`, `data_sources/clients/_document_text.py:239` |
| Enumerate a folder | Connector `LIST_FILES` / `SEARCH_FILES` on SharePoint (Online and on-prem), Drive, S3, Documentum, network dir | `data_sources/clients/base.py:10-60` |
| Tabular result that can be charted and put on a dashboard | `Step.data` grid `{rows, columns, info}` | `models/step.py:38` |
| Queryable, refreshable custom table | `ConnectionTable(kind='bow')` materialized to DuckDB, queried through `::fast` | `models/connection_table.py:42-65`, `docs/design/query-acceleration.md` |
| Per-tool persistence and audit | `ToolExecution.result_json` (encrypted) | `models/tool_execution.py:24` |
| Saved run spec with parameters | `Prompt` / `PromptRun` spawn a new report seeded with agents | `models/prompt.py:7` |
| Schedules, webhooks, headless turns | `ScheduledPrompt`, spawn-mode `Webhook`, `run_machine_turn` | `models/scheduled_prompt.py`, `docs/design/agent-triggers.md`, `services/machine_turn.py` |
| Evals, including per-field assertions | `FieldRule(TargetRef("tool:<name>", field))` + `NumberCmp` / `TextEquals`; judge; self-improving loop | `schemas/test_expectations.py:67-172` |
| A form rendered from a JSON Schema | `ConnectForm.vue` | `frontend/components/datasources/ConnectForm.vue` |

Gaps:

- **No provider-native structured output anywhere.** No `response_format`, no forced
  `tool_choice`, no `strict`.
- `output_model` is never validated at runtime.
- `Step.data` is capped at about 1000 rows and stores values as strings.
- There is no "new file in folder" trigger.
- Nothing can be written back to external systems, apart from `write_file` on a network dir.

**The key insight:** we do *not* need a new LLM call path for v1. PlannerV3 already does native
`tool_use`, and the tool catalog is just a list of `{name, description, schema}`. A
**run-scoped, dynamically generated tool** whose `input_schema` *is* the user's schema is
provider-native structured output. `ToolRunner` already turns schema violations into
field-level retry messages. Adding forced `tool_choice` or `strict: true` per provider is a
later hardening step, not a prerequisite.

## 4. Concepts

```
submit tool (the sink at the end of today's agent loop; see §7)
 └─ fields[]: {id, name, type, description, method, required, enum?, items?, rules?}
    (written by the agent from the ask + instructions, or copied from a skill)
 └─ pinned on the resulting Step, like Step.code, and reused on scheduled reruns

 subjects:  "this conversation"        → 1 record   (chat / analysis step)
            "each file in <folder>"    → N records  (batch)
            "each row of <query>"      → N records  (e.g. per customer: pull docs + data)

record = one Step row per subject per run
 └─ values: {field_id: {value, status, evidence[], note}}
    status ∈ found | not_found | ambiguous | inferred | invalid
    evidence: {kind: file|query|web, ref, page?, quote?, step_id?, verified: bool}
 └─ review_state ∈ auto_approved | needs_review | approved | corrected | rejected
```

### 4.1 Field definition (what the user edits)

```yaml
- id: f_annual_value          # stable, never shown
  name: annual_value          # column name, can be renamed
  type: number                # string|number|integer|boolean|date|enum|array<object>
  unit: currency              # optional semantic hint: currency|percent|date|email...
  method: derive              # extract | classify | derive
  required: false             # false ⇒ nullable, "not_found" is a valid answer
  description: >
    Total contract value per 12 months, excluding VAT. If the contract states a
    multi-year total, divide by the term in years. Use the agent's "ARR" definition.
  rules:                      # deterministic post-checks (v1: a small expression set)
    - "annual_value >= 0"
```

- **extract**: the value appears in the source. It needs a `quote`, which is verified by string
  match against the parsed page text.
- **classify**: an enum or boolean. The quote is optional, and the reasoning note is required.
- **derive**: computed or inferred. It needs a `note` (how it was derived) plus the evidence
  inputs. It can cite a **query step** (`step_id`) as well as a document; this is the BOW-only
  capability.

### 4.2 What the model is asked to emit (compiled schema)

From the user's fields we compile one tool, `submit`, whose input is:

```json
{
  "subject_ref": "sharepoint://.../MSA-Acme-2025.pdf",
  "fields": {
    "annual_value": {
      "value": 120000,
      "status": "found",
      "evidence": [{"kind": "file", "ref": "<file_id>", "page": 4,
                    "quote": "an annual fee of USD 120,000"}],
      "note": null
    },
    "renewal_date": { "value": null, "status": "not_found", "evidence": [], "note": "No renewal clause; contract is fixed-term." }
  }
}
```

The compiler targets the strict subset used by OpenAI and Anthropic: every key is `required`,
optional values are nullable, `additionalProperties: false` is set, and min/max/pattern are
dropped from the compiled schema. The full user schema, including rules, is validated
**server-side** inside the tool. A failure returns `Validation failed: fields.annual_value.value: must be >= 0`,
and the existing retry loop does the rest.

The wrapper is intentionally uniform: `value`, `status`, `evidence`, `note`.

- Reasoning happens *before* the tool call, in the agent loop. The `note` field only carries a
  short justification, so we avoid the answer-before-reason trap.
- `status` gives us Sensible-style categorical signals for free.

## 5. How it works

### 5.1 Mode A: extraction as the output of an analysis (chat, prompts)

1. The user attaches a schema to a turn ("Extract: Contract fields v3"), or the schema is part of
   a saved `Prompt`.
2. The agent loop gets an extra catalog entry, `submit`, plus a short prompt block: "Your
   final output is one or more `submit` calls. Reason and gather evidence first. Use
   `not_found` rather than guessing."
3. The agent uses its normal tools (`search_files`, `read_file`, `create_data`, `run_query`) and
   applies the agent's instructions.
4. `submit` runs:
   1. Validate against the full schema.
   2. Verify quotes against the cached page text of the cited file, using a fuzzy match that is
      tolerant of whitespace, RTL text and hyphenation.
   3. Run the field rules.
   4. Compute `review_state`.
   5. Upsert the record as a row of the collection's Step.
   6. Return a compact observation, for example "4/5 fields found, 1 quote unverified on
      renewal_date". The agent may fix the record once.
5. The completion ends with a record card in chat: fields in a grid, and clicking a value opens
   the file viewer at the cited page (`read_file` already exposes viewer page numbers).

This is the part that answers "structured output as the result of an analysis step". It needs
no batch machinery and already delivers value in chat.

### 5.2 Mode B: batch over a document set (the automation)

```
subjects = list_files(folder, filter)             # connector LIST_FILES / SEARCH_FILES
for each subject (bounded concurrency, idempotent on (schema_ver, ref, etag)):
    fast path: one LLM call = [field block + instructions + parsed doc text/pages]
               → submit (forced)            # ~1 call per doc, cheap
    if any required field ∉ {found} or a quote/rule fails:
        escalate: full agent turn scoped to that doc (can read more pages,
                  cross-check a query, open related files)
    persist record
```

- The two-tier path (a cheap single call, escalating to an agent turn) is the cost answer. In
  most real document sets, most documents are easy.
- The fast path is where we add the new LLM capability: `LLM.inference(..., tools=[submit], tool_choice={"name": "submit"})`,
  with forced tool choice per provider. This is a small addition to `clients/*`. Use `strict`
  where the provider supports it.
- For long documents (over roughly 60 pages), use Sensible-style grouping. Pages are selected by
  keyword and grep hits per field group, not by sending the whole text. We have no embeddings,
  and we don't need them for v1.
- Array fields (line items, one row per obligation) use LlamaExtract's `per_table_row` idea: a
  top-level array field produces N child rows.

### 5.3 Where results go

1. **`extraction_records` table in our DB.** This is the source of truth. It stores values, status,
   evidence, review state, provenance (`report_id`, `completion_id`, `tool_execution_id`, schema
   version, model) and file `etag`.
2. **A queryable table per job** is exposed as `ConnectionTable(kind='extraction')` on the
   `::fast` DuckDB path:
   - One typed column per field, plus `_status_<field>`, `_source_ref` and `_reviewed`.
   - Agents, dashboards and `create_data` can then `SELECT ... FROM contracts_extracted JOIN erp.customers`.
   - **This is the flywheel: documents become a table, and the table feeds analytics.** It also
     sidesteps the Step row cap and the string-only values.
3. **Exports:** CSV/Excel (`write_csv`), a webhook POST of each record to external systems, and
   in v3 a write-back to SharePoint library columns through Graph.

### 5.4 Trust: review, rules, confidence

- `review_state` is decided by policy, not by a number. A record is `needs_review` if any
  required field is not `found`, any `extract` quote fails verification, any rule fails, or the
  document parse is poor (`doc_text_looks_garbled` / vision-only).
- The policy is configurable per job: "auto-approve when clean" or "always review".
- The review queue is a grid of records needing review. The reviewer sees the value, a
  highlighted quote and the file page, and can accept or correct.
- **Corrections become eval cases automatically** (`TestCase` with `FieldRule` on
  `tool:submit`), as Extend does. Auto-approved records are **never** used this way.
- A numeric confidence score, via self-consistency (sampling twice and comparing, as Box and
  Unstract do), is an opt-in "high-assurance" toggle per job. It doubles the cost, so it is not
  the default.

### 5.5 Quality loop (the "self-improving" story we already sell)

- An eval set is 10–30 documents with ground-truth records, bootstrapped by "run once, then
  correct" (the auto-label pattern from Azure, Bedrock DA and Google).
- Metrics: accuracy per field, "records fully correct", and not-found precision (did it say
  `not_found` when the value was actually missing?).
- A failing eval triggers the existing self-improving loop, which drafts **field description or
  instruction** edits, re-runs the evals, and promotes the change as a new schema version after
  approval.
- Jobs are **pinned to a schema version**, so production never changes silently.

### 5.6 Governance: the part the IDP vendors don't have to think about

- **Access leakage:** a batch job runs under *someone's* credentials, often a service account.
  The extracted table can then expose values from documents a viewer couldn't open.
  - v1 rule: each record carries `source_ref`, and table access requires access to the job's
    connection.
  - Later: row-level filtering by the source document's ACL, reusing the RLS machinery.
- **PII:** fields may be tagged `pii: true`. Those values are encrypted at rest (the tables
  already use `EncryptedJSON`) and redacted in logs, and the existing PII redaction in `llm/pii/`
  applies.
- **Audit:** every record links to its `ToolExecution`, model and schema version.

## 6. Why the agent (not a plain extractor)?

Three things a pure IDP pipeline can't do and BOW can:

1. **Definitions live in instructions.** "Revenue = net of returns, in USD at month-end FX" is
   written once, in the instructions everyone already uses and reviews, and every extractor and
   chat obeys it.
2. **Cross-source fields.** `variance_vs_erp = doc.revenue - erp.revenue` is a derived field
   whose evidence is a quote *and* a query step. Reconciliation (invoices vs POs, contracts vs
   billing) is where the money is.
3. **Escalation.** A hard document gets a real investigator: it can open the amendment, check
   the email thread and query the CRM, instead of returning `null`.

The cost of an agent is latency and money. That is why the batch path is two-tier (§5.2).

## 7. Scoping decision (final): keep extraction as today, add one `submit` tool

The agent **already extracts**. It reads SharePoint files with `read_file`, runs SQL with
`create_data`, and reasons with the agent's instructions. The only missing piece is a
**structured sink** at the end. So we don't build an extraction runner at all:

```
today:     agent loop (search_files, read_file, create_data, …) → free-text answer
proposed:  agent loop (unchanged)                                → submit(record) → typed rows
```

`submit` is one new tool. Nothing else in the loop changes.

- **Input:** `{collection, records: [{fields: {name: {value, status, evidence[], note}}}]}`,
  with the §4.2 wrapper for each field.
- **Where the schema comes from, in priority order:**
  1. The fields are given by a **skill**, a **saved Prompt** or the user's message. The tool is
     then rendered with the **concrete schema as its `input_schema`** (a run-scoped catalog
     entry, §3). The provider validates it natively and `ToolRunner` retries on errors.
  2. Otherwise the **first `submit` into a collection declares the fields**, and later submits
     must match. This is the chat "just extract these for me" case.
- **What it does:**
  1. Validate the full schema server-side.
  2. Verify quotes against the cached page text of the cited file.
  3. Run field rules.
  4. Upsert the rows into a tracked **Step** for the collection, with the schema pinned. Step,
     dashboard, `load_step`, Entity and evals all come for free.
- **Automation needs no new machinery.** A `ScheduledPrompt`, spawn-mode webhook or saved
  Prompt re-runs the same ask. The pinned schema keeps the columns stable, and `submit` upserts
  by `subject_ref` (and `etag`), so reruns update rows instead of duplicating them.
- **This is exactly "structured output as the output of an analysis step".** The reasoning
  happens freely in the loop, and only the last call is strict, which is the pattern the
  research favours (§2, pitfall 2).

**What it doesn't solve: scale.** One agent loop is capped at `step_limit` (default 100, max
500; `ai/agent_v2.py:4502`). Each document costs at least a `read_file` and a `submit`
(parallel calls help), and the context grows with every document read. In practice that means
**tens of documents per run, not thousands**. That covers chat, "this deal's documents" and a
weekly folder with a handful of new files.

**Add only when a customer hits that ceiling (P1):**

- A fan-out for batches: one small scoped turn per document, run as the §5.2 fast path, all
  calling the same `submit` into the same collection.
- `submit` stays the single sink either way, so this is purely an execution optimization.

**Rejected, in order of how heavy they are:**

- An org-level `ExtractionSchema` entity: a new noun, built before there's demand.
- A dedicated `extract_data` runner tool: it duplicates what the loop already does, and is only
  justified for batch scale, which is the fan-out above.
- A report-owned schema: it can't be reused.

### 7.1 Where the schema lives: a per-agent "Collections" group in the Knowledge Explorer

Decision: the schema is defined **per agent**, as a new tree group in
`frontend/components/KnowledgeExplorer.vue`. It sits next to Tables, Tools, Files,
Instructions, Queries and Evals (the agent subtree starts around `:212`).

- **Name: "Collections", not "Lists".** "Lists" collides with SharePoint Lists, which is
  already a connector (`graph_list_client.py`). A collection is a schema plus the rows that
  accumulate in it.
- **Editor:** a field table with name, type, description (the prompt), required, enum/items,
  an optional key field for upserts, and an "evidence required" toggle. Editing requires
  manage-agent permission, like the other agent groups.

**Runtime:** for each collection on the report's agents, register a native tool
`submit_<collection_slug>` whose `input_schema` is the compiled collection schema (§4.2
wrapper). This follows the exact precedent of native MCP tools:

- `build_native_mcp_tools` (`ai/tools/mcp_tool_registry.py:193`) appends per-report
  `ToolDescriptor`s to the planner catalog, with routing (`ai/agent_v2.py:7596-7612`).
- Server-side validation reuses `validate_arguments` (`ai/tools/mcp_schema.py:150`,
  Draft 2020-12, path-qualified errors), feeding the existing `ToolRunner` retry loop.
- The catalog-bloat guard mirrors `native_tools_enabled(tool_count)`. Past a threshold,
  collections are listed in a `<collections>` prompt block and the agent calls a generic
  `submit(collection, records)` that validates against the named schema.

**Rows:** rows belong to the **collection**, not to a report, and accumulate across chats and
scheduled runs.

- Each row carries provenance (`report_id`, `tool_execution_id`, schema version) and is
  upserted on the key field.
- The collection is exposed back to the agent as a queryable table under the agent's Tables
  (the `::fast` DuckDB path). The agent can then answer "average contract value by region"
  over what it has extracted, or join it with SQL sources.
- Viewing rows requires access to the agent.

**Schema changes:**

- Additive edits (new optional field, description tweak) are free.
- Breaking edits (type change, removed field, new required field) bump the version. Old rows
  keep their version and show as "stale" until re-extracted.

This supersedes the "schema pinned on a Step" idea above. The Step remains how a collection
is charted or put on a dashboard, through the queryable table.

## 8. Phasing (final)

**P0: `submit` (about 1–2 weeks)**

- A Collections group in the Knowledge Explorer, with a field editor and a rows viewer.
- A per-collection native `submit_<slug>` tool (following the MCP native-tool precedent),
  validated with `validate_arguments`.
- Quote verification and rules, then an upsert into the collection's rows.
- The collection exposed as a queryable table.
- A record-grid tool card whose cells open the cited file page, plus CSV export.
- A prompt block: "when a schema is active, finish by calling `submit`; use `not_found` rather
  than guess".
- Eval `FieldRule` on `tool:submit`.
- **No LLM-layer changes.** The planner already uses native `tool_use`.

**P1: trust and scale**

- A review state, a review queue, and corrections becoming eval cases.
- Etag-aware "only new or changed files" on scheduled reruns.
- A batch fan-out plus forced `tool_choice` per provider for the per-document fast path.
- `ConnectionTable(kind='extraction')` when a collection outgrows a Step (about 1000 rows).

**P2:** a "new file in folder" trigger, suggesting fields from sample files, an optimizer loop
over skill versions, write-back to SharePoint columns, and bounding-box highlights.

## 9. Open questions

1. ~~Home of the schema~~: decided in §7: extraction stays in today's agent loop, a new `submit` tool is the sink, the schema is pinned on the Step, and standard field lists live in skills or Prompts.
2. **Is it a Prompt?** An extraction job looks very close to `Prompt` + schema + subjects.
   Extending `Prompt` avoids a parallel automation surface.
3. **Where does `submit` end the run?** In Mode A, should calling it end the turn
   (`analysis_complete`), or may the agent continue and add prose? The recommendation: allow
   prose after it, and treat the record as the canonical output.
4. **Record granularity for multi-record chats**, for example "extract all 12 invoices mentioned
   in this thread". Is it one tool call per record, or an array? The recommendation: one call
   per record, because it is simpler to validate and retry.
5. **Pricing and limits:** documents per run, pages per document, and a cost preview computed from
   the first N documents before a full run.
