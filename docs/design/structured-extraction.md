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
ExtractionSchema (versioned, org-scoped, attached to one or more agents)
 └─ fields[]: {id, name, type, description, method, required, enum?, items?, rules?}

ExtractionJob   = schema@version + agent(s) + subjects + trigger
 subjects:  "this conversation"        → 1 record   (chat / analysis step)
            "each file in <folder>"    → N records  (batch)
            "each row of <query>"      → N records  (e.g. per customer: pull docs + data)

ExtractionRecord (one per subject per run)
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

From the user's fields we compile one tool, `submit_record`, whose input is:

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
2. The agent loop gets an extra catalog entry, `submit_record`, plus a short prompt block: "Your
   final output is one or more `submit_record` calls. Reason and gather evidence first. Use
   `not_found` rather than guessing."
3. The agent uses its normal tools (`search_files`, `read_file`, `create_data`, `run_query`) and
   applies the agent's instructions.
4. `submit_record` runs:
   1. Validate against the full schema.
   2. Verify quotes against the cached page text of the cited file, using a fuzzy match that is
      tolerant of whitespace, RTL text and hyphenation.
   3. Run the field rules.
   4. Compute `review_state`.
   5. Persist an `ExtractionRecord`.
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
               → submit_record (forced)            # ~1 call per doc, cheap
    if any required field ∉ {found} or a quote/rule fails:
        escalate: full agent turn scoped to that doc (can read more pages,
                  cross-check a query, open related files)
    persist record
```

- The two-tier path (a cheap single call, escalating to an agent turn) is the cost answer. In
  most real document sets, most documents are easy.
- The fast path is where we add the new LLM capability: `LLM.inference(..., tools=[submit_record], tool_choice={"name": "submit_record"})`,
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
  `tool:submit_record`), as Extend does. Auto-approved records are **never** used this way.
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

## 7. Phasing

**P0: "structured output of an analysis"** (smallest useful slice, chat only)

- `ExtractionSchema` model: versioned JSON, attached to agents or supplied inline for a turn.
- A dynamic `submit_record` catalog entry, compiled from the schema, with server-side
  validation, quote verification and rules.
- `ExtractionRecord` persistence, a record card in chat, and CSV export.
- An eval `FieldRule` target on `tool:submit_record`.
- Nothing new in the LLM layer.

**P1: batch + automation**

- Forced `tool_choice` (and `strict` where available) in `openai_client`, `anthropic_client`,
  `azure_client`, `google_client` and `bedrock_client`, for the fast path.
- Jobs: folder or query subjects, the two-tier runner, concurrency and idempotency, and a
  schedule trigger (reuse the APScheduler / `ScheduledPrompt` plumbing).
- Run grid UI (rows are documents, columns are fields, click through to evidence), the review
  queue, and "test on 5 files" before saving.
- A `ConnectionTable(kind='extraction')` exposed on `::fast`.

**P2: polish and moat**

- A "new or changed file in folder" trigger that polls `LIST_FILES` with an etag cursor, plus
  webhooks.
- Suggest a schema from sample files, and derive schemas from a description in plain language.
- An optimizer loop over schema versions, and reviewer corrections feeding evals.
- A SharePoint column write-back, bounding boxes and highlight overlays, and an optional
  self-consistency confidence toggle.

## 8. Scoping decision: org-owned, agent-attached, report-run

Three separate questions are involved:

| | Answer | Why |
|---|---|---|
| **Who owns the schema** | The org. `ExtractionSchema` is its own first-class, versioned row (`organization_id`) | It is an *output contract*, not agent context. The same "Contract fields v3" is run by a legal agent reading SharePoint and by a finance agent reconciling against the ERP. Ownership by one agent would force copies that drift apart. |
| **Where it is attached** | Agents, M2M, with `scope ∈ agent / global / private`, exactly like `Prompt` (`models/prompt.py:7-20`) | This lets an agent advertise its extractors (they surface like starters). Visibility follows the existing rule: you can use it only if you can access all of its active agents. It also gives us permissions and the table-access rule of §5.6 for free, because the agents' connections gate the extracted table. |
| **Where it runs** | A report. Every run is a session, which provides provenance, the audit trail and the "why is this value X" conversation | A report is a *container for runs*, not a home for the schema. An inline, ad-hoc schema typed into a chat lives on that turn only, and can later be **promoted** to a saved schema (the fork/promote pattern). |

So the answer to "agent, report or none" is: **none owns it, agents expose it, reports execute
it.**

- Records and the extracted table belong to the **job** (schema version plus subjects plus
  trigger), not to any single report.
- A scheduled batch spawns a new report per run, the same way spawn-mode webhooks and
  `ScheduledPrompt(spawn_new_report)` do. All of those runs append to one table.

**Rejected alternatives:**

- **Agent-owned:** the same schema would be duplicated per agent. It also mixes "what the agent
  knows" (context) with "what shape the output takes" (contract).
- **Report-owned:** it can't be reused, and records would be buried in a conversation. It breaks
  the documents → table → dashboard flywheel.
- **`Instruction.structured_data`:** we'd get review and Git sync, but it would overload the
  meaning of an instruction. It would also be loaded into prompts by the instruction builder,
  which is the wrong lifecycle.

## 9. Open questions

1. ~~Home of the schema~~: decided in §8.
2. **Is it a Prompt?** An extraction job looks very close to `Prompt` + schema + subjects.
   Extending `Prompt` avoids a parallel automation surface.
3. **Where does `submit_record` end the run?** In Mode A, should calling it end the turn
   (`analysis_complete`), or may the agent continue and add prose? The recommendation: allow
   prose after it, and treat the record as the canonical output.
4. **Record granularity for multi-record chats**, for example "extract all 12 invoices mentioned
   in this thread". Is it one tool call per record, or an array? The recommendation: one call
   per record, because it is simpler to validate and retry.
5. **Pricing and limits:** documents per run, pages per document, and a cost preview computed from
   the first N documents before a full run.
