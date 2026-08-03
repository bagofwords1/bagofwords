# Agent tool results — native transcript, compaction, cross-LLM parity

**Status: design. No implementation yet.**

Today the planner emits **native tool calls** but never sends **native tool
results**. Every planner iteration is a fresh single-turn request whose one user
message is rebuilt from scratch, with prior results hand-serialized into
`<past_observations>` / `<last_observation>` JSON.

This doc specifies the move to a growing, provider-native transcript: a typed
result contract, an append-only part list, one transport seam, and a decay
ladder for compaction — plus the cross-cutting requirements (PII, cost, quota,
token counting, provider-opaque state) and the sandbox loop that validates it
per provider.

---

## 1. The defect

`prompt_builder_v3.py:72-80` builds exactly one message:

```python
msg = Message(role="user", content=user_content)
return PlannerInputV3(system=..., messages=[{"role": msg.role, ...}], tools=[...])
```

`planner_v3.py:171-180` reifies that single message and calls
`inference_stream_v2`. Results re-enter as text (`prompt_builder_v3.py:686-689`):

```python
parts.append(f"  <past_observations>{json.dumps(compacted)}</past_observations>")
parts.append(f"  <last_observation>{last_obs}</last_observation>")
```

**All nine callers** of `inference_stream_v2` pass a one-element message list —
planner, coder ×3, `create_data`, `create_artifact` ×2, `edit_artifact`. There
is no multi-turn exchange anywhere in the codebase.

### What it costs

| # | Consequence | Evidence |
|---|---|---|
| 1 | Prompt caching is near-worthless — breakpoints sit on `system` + last tool, so schemas / instructions / files / resources / history / observations are re-prefilled every iteration | `anthropic_client.py:301-319` |
| 2 | Even the stable head can't cache: `HH:MM:SS` is the first line of the user message | `clock.py:123-128`, `prompt_builder_v3.py:573` |
| 3 | Reasoning continuity is dropped — `signature` is received and discarded | `planner_v3.py:292-297`, `types.py:207-211` |
| 4 | Two independent result renderers, both keyed on tool name | `observation_context_builder.py:36-190` + `_OBS_KEEP_KEYS` (`prompt_builder.py:21-33`) vs. `message_context_builder.py` (~2200 lines, 10 `_digest_*` fns) |
| 5 | No result contract — every tool invents its own budget | `tool_runner.py:204`; `read_file.py:60` (4 000), `read_query.py:33` (50 000), `data_preview.py:28` (1 000) |
| 6 | Per-iteration context rebuild is ~quadratic over a run — full `message_builder.build()` with an N+1 per completion *and* per block, every loop | `agent_v2.py:3937-3982`, `message_context_builder.py:1789+` |
| 7 | Workarounds that exist only because there is no turn structure | vision TTL (`agent_v2.py:191, 1407`), `_carry_substantive_observation` (`:3341`), `_aggregate_batch_observation` (`:3374`) |
| 8 | Budget trimming corrupts XML mid-tag | `context_hub.py:134-143` |

---

## 2. What already exists (the leverage)

The expensive half is built. This is mostly a **caller-side** change.

| Piece | Where | State |
|---|---|---|
| `ToolUseBlock`, `ToolResultBlock` | `types.py:66-80` | defined, unused |
| `Message.content: Union[str, list[dict]]` | `types.py:83-91` | defined, always a string in practice |
| `text` / `tool_use` / `tool_result` / `image` translation, **all six clients** | `anthropic_client.py:198-225`, `bedrock_client.py:257-268`, `google_client.py`, `openai_client.py:233-245`, `azure_client.py:134-145`, `openai_responses_client.py:183-194` | implemented, **unfed** |
| Multi-turn replay tests | `tests/unit/test_google_message_translation.py`, `tests/integrations/llm_clients.py:499` | landed on main — regression net exists before we build |
| Provider-opaque signature carrier | `types.py:120-132` (`ToolUseCompleteEvent.signature`) | landed on main |
| PII redaction of `tool_result` content | `llm.py:296-363` (`_apply_pii_v2`, keys `text` + `content`) | **already correct** |
| Usage / cost / quota accounting incl. cache tokens | `llm.py:620-700`, `:842-882`, `LLMUsageRecord` | works; needs new metrics only |
| Structured rolling compaction | `context_compaction_service.py` | good — but runs on completions, not the transcript |
| Per-tool digests | `message_context_builder.py:49-703` | good — wrong location |
| Cheap auxiliary model | `self.small_model`, in `runtime_ctx` | plumbed, unused for compaction |
| Planner input dump | `planner_v3.py:90-115` (`BOW_PLANNER_DUMP_FILE`) | seed for I/O capture |
| Persisted execution rows | `ToolExecution`, `PlanDecision`, `CompletionBlock` | richer than needed; not replayable |

**Read this table as the thesis: the hard parts are done and the simple one was
skipped.** A growing list feeding the transport layer is what's missing.

---

## 2b. Findings from the full builder scan

A sweep of `context_hub` and every builder/section turned up four things the
rest of this doc had to be corrected for.

### 2b.1 "Static context" is not static — it mutates mid-run

This is the most consequential finding: it invalidates the naive version of
Phase 3.

The schema block — the largest context section — renders **live usage
statistics** per table: `<usage count/success/failure>`, `<success_rate>`,
`<last_used_at>` (`tables_schema_section.py:368-382`, populated at
`schema_context_builder.py:528-560` from `TableStats`).

Those stats are written **during the run**. `emit_table_usage` /
`emit_table_usage_from_tables_by_source` record a `TableUsageEvent` and upsert
`TableStats` on every step creation (`project_manager.py:171, 251`) — i.e. every
`create_data`.

And the schema block is re-rendered **per tool execution** with
`build(with_stats=True)` (`agent_v2.py:4958-4961`).

**Net: every time the agent queries a table, the biggest "static" section
changes, and it is rebuilt before the next planner call.** A cached
static-context turn would never hit.

The same applies to instructions, and worse: `_batch_load_usage_counts` feeds
the **sort key** (`instruction_context_builder.py:477-478, 928`), so a usage
change **reorders** the block rather than editing a value — rewriting it end to
end.

**Consequences for the design:**

- Phase 3 must split the schema/instruction sections into a **stable identity
  part** (names, columns, types, descriptions — cacheable) and a **volatile
  signal part** (usage, success rate, recency, ordering) rendered in the
  per-turn tail.
- Ranking may read usage stats freely; **rendering** them into the cached prefix
  is what costs. Prefer a stable sort key (e.g. name) in the cached block with
  ranking applied to selection, not to output order.
- Cheapest interim option: snapshot stats **once per run** and render from the
  snapshot, so the block is stable within a run even if it differs between runs.
- This also removes the per-tool `build(with_stats=True)` rebuild, which is
  pure waste today (§1, row 6).

### 2b.2 Six result representations, not two

Earlier drafts said two. The real count:

| # | Representation | Where |
|---|---|---|
| 1 | in-run observations | `observation_context_builder.py` |
| 2 | cross-run tool digests | `message_context_builder.py` `_digest_*` |
| 3 | **re-parsed digests** | `messages_section.py:66-92` `_minify_message` |
| 4 | query observations | `query_context_builder.py:288` `_build_query_observation` |
| 5 | widget observations | `widget_context_builder.py:138` `_build_observation_data` |
| 6 | loadable steps | `agent_v2._build_available_steps_context` |

A single `create_data` result can be present in five of these at once —
`<past_observations>`, a `Tool: create_data …` digest line, `<queries>`,
`<available_steps>`, and (historically) `<widgets>`. Each has its own truncation
rule and its own notion of what is worth keeping.

The transcript does not replace all six — 4 and 6 are legitimately *indexes of
reusable artifacts*, not turn history. But they should read from the same
persisted part, not re-derive from `Step` / `Query` rows independently.

### 2b.3 The regex round-trip proves the missing contract

`messages_section.py:6-14` defines `_ID_PATTERNS` and scrapes `viz_id:`,
`artifact_id:`, `query_id:` back **out of the digest prose** with regex, so
older messages keep their referenceable ids after minification.

Structured data → formatted string → regex → structured data. That layer exists
only because the result has no typed shape. With `ToolResultPart` carrying ids
as fields, `_minify_message` and `_ID_PATTERNS` delete outright.

### 2b.4 Windowing policy is defined in seven places

`_RECENT_OBS_FULL = 5` (`prompt_builder.py:18`), `_OBS_KEEP_KEYS` (`:21`),
`_RECENT_FULL = 7` (`messages_section.py:16`), the observation builder's
per-tool stripping, `compaction_budgets()`, `trim_context_to_budget()`, and
per-tool caps (`read_file.py:60`, `read_query.py:33`, `data_preview.py:28`).
Seven independent answers to "what survives". The ladder (§3.4) is one answer.

### 2b.5 `WidgetContextBuilder` is dead — delete it

`widget_context_builder.py:158-159` hardcodes `allow_llm_see_data = True` with
a "settings check can be added later" comment, then gates preview rows on it.

**This is not a live data leak.** Its output reaches only
`ContextSnapshot.widgets_context`, consumed by token-estimation metrics and
`_render_for_prompt` — and `context_hub.render()` has no callers. The warm-cache
entry is explicitly `None  # Deprecated` (`context_hub.py:828`), the v3 prompt
builder has no widgets block, and `_build_slim_context_snapshot` reads `view`
(where widgets are None), so it does not reach persisted snapshots either.

It is dead weight carrying a latent data-visibility bypass that would become
real if anyone re-wired it. Delete in Phase 5 rather than fix.

---

## 2c. Measured in the sandbox (Phase 0 landed)

Two providers, 10-turn conversations over two CSVs, real API keys. Traces via
`BOW_LLM_TRACE_FILE` (`app/ai/llm/trace.py`).

### 2c.1 The core claim is now measured, not inferred

Across **31 Anthropic calls and 36 OpenAI calls**, every single request had
`message_count == 1` and content kind `str`. Zero `tool_result` blocks, on both
the Anthropic client and the OpenAI Responses client.

Anthropic also showed the cache working exactly where §1 predicted and nowhere
else: `cache_creation = 31,358` on call 1, then `cache_read = 31,358` on every
subsequent call — the system+tools prefix, byte-stable, and nothing below it.

### 2c.2 What Phase 0 actually bought

**message_builder N+1 — real, deterministic.** Benchmarked over 20 repeats
against a real report (query count is not perturbed by agent nondeterminism):

| | queries/build | ms/build |
|---|---|---|
| before | 73.0 | 89.8 |
| after | 48.0 | 61.0 |
| | **−34%** | **−32%** |

Output is byte-identical (md5-verified against the pre-change renderer on the
same report). In one 10-turn run the builder ran 95 times, so this is ~2,400
fewer queries per conversation, and it grows with conversation length. On
sqlite the time saving is modest; on Postgres the query-count reduction is the
number that matters.

**Time-block move — structurally real, no measurable payoff yet.** Common
prefix between two consecutive planner messages one second apart:

| | stable prefix |
|---|---|
| before | 32 bytes (0.6%) |
| after | 5,199 bytes (89.7%) |

But an A/B on OpenAI (the provider that caches prefixes automatically) showed
**no improvement**: 80.6% → 77.0% cache hit, inside run-to-run noise. Two
reasons, both worth carrying forward:

1. **The binding constraint moved rather than disappeared.** With realistic
   state the prefix now ends precisely at `</past_observations>` — observations
   still break it before the volatile tail is reached. The clock was never the
   whole problem.
2. In this sandbox the system prompt (13,076 chars) dwarfs the user message
   (~5,800 chars), so the newly-stable region is small next to what already
   cached. A production org with large schemas inverts that ratio.

**Conclusion: the time-block move is a prerequisite, not a win.** It pays off
only once observations move into the transcript (Phase 2) and a breakpoint is
placed at the boundary (Phase 3). Do not expect it to show up on its own.

**Wall-clock deltas from the E2E runs are not attributable.** The Anthropic
before/after showed −32.5% wall clock, but turn 1 alone went 75.2s → 7.9s
(first-ever run indexing the data source), which accounts for the entire delta,
and the after-run did more work (36 calls vs 31). Agent nondeterminism makes
end-to-end wall clock useless as a Phase 0 metric — use the deterministic
benchmarks above.

### 2c.3 Metric definitions differ per provider — normalise before comparing

Anthropic reports `input_tokens` **exclusive** of cache; OpenAI reports it
**inclusive**. Total input is `input + cache_read + cache_creation` on
Anthropic but just `input` on OpenAI. Comparing raw `prompt_tokens` across
providers is meaningless; a naive cache ratio came out at 445%.

### 2c.4 Bug found by the loop (fixed separately)

A 10-turn run failed the *same* turn on both providers — the one touching the
CSV with a `name` column. `payload_name` did `(getattr(payload, "name", "") or
"")`; pandas resolves attribute access against the frame's columns, so that
returned a Series and raised. `read_file` failed outright for any spreadsheet
with a `name` column (19 occurrences in the baseline log, 0 after the fix).

This is the argument for the loop: two providers agreeing on a failure turn is
a much louder signal than either run alone.

---

## 2d. Phases 1–2 landed (opt-in) — measured

`BOW_PLANNER_TRANSCRIPT=1` (or `PlannerInput.use_transcript`). Off by default.

### 2d.1 The shape actually changed

| | single-message | transcript |
|---|---|---|
| `message_count` per planner call | always **1** | **2 / 4 / 6 / 8** |
| content kinds | `str` only | `str` + `blocks` |
| `tool_use` blocks | 0 | 23 |
| `tool_result` blocks | **0** | **23** |

Perfectly paired, on both Anthropic and OpenAI.

### 2d.2 A/B, same model, same 10 prompts, both post-Phase-0

| metric | Anthropic | OpenAI |
|---|---|---|
| **billed-fresh input tokens** | **−23.0%** | **−20.5%** |
| cache hit ratio | 77.5% → 81.1% | 77.0% → **85.9%** |
| total input tokens | −8.3% | −20.5% |
| output tokens | −16.6% | −21.9% |
| wall clock | −6.5% | −15.2% |
| answer grade | 9/10 (unchanged) | 6/10 vs 8/10 |

Billed-fresh input is the number that matters: cache reads are a fraction of
the price. The mechanism is the one §1 predicted — prior steps now sit in
cacheable prefix turns instead of being re-serialized into a fresh user message
every iteration.

**Caveats, stated plainly.** Agent nondeterminism moves call counts run to run
(36 vs 35, 36 vs 28), so treat single-digit deltas as directional. The OpenAI
grade drop is not attributable to the transcript: the failures are the same
customers.csv-vs-sales.csv routing error seen on every run, plus regex-grader
imprecision (turn 10 is graded FAIL on an answer that is substantively right).
Quality needs a real eval suite before any claim is made either way.

### 2d.3 Elision must not be mistakable for failure

The first live transcript probe exposed a design bug the unit tests did not.
Tier `DROPPED` returned an empty body — discarding the digest it had already
computed — and all three providers read the gap as a failed call:

| | before | after |
|---|---|---|
| Anthropic | *"No row count returned (file may not exist or error occurred)"* | "sales.csv: 200 rows, customers.csv: 60 rows" |
| OpenAI | *"couldn't get a row count for sales.csv"* | same, correct |
| Azure | *"sales.csv: unavailable"* | same, correct |

`DROPPED` now falls back digest → breadcrumb naming the tool and its outcome.
**A decayed result must always say that it succeeded and was elided.**

Related: for a result that *has* a digest, `DROPPED` and `DIGEST` now render
identically, so the DROPPED pass saves nothing there. The real lever is
tier 1 + good per-tool digests; `DROPPED` earns its keep only for results with
no digest at all.

### 2d.4 Provider matrix status

| provider | client | simple | tool call | **replay** | 10-turn | id format |
|---|---|---|---|---|---|---|
| anthropic | Anthropic | ✅ | ✅ | ✅ | 8/10 | `toolu_…` (provider) |
| openai | OpenAIResponses | ✅ | ✅ | ✅ | 7/10 | `call_…` (provider) |
| azure (`use_responses_api`) | OpenAIResponses | ✅ | ✅ | ✅ | probe | `call_…` (provider) |
| bedrock | Bedrock | ✅ | ✅ | ✅ | 8/10 | `tooluse_…` (provider) |
| google | Google | ✅ | ✅ | ✅ | 8/10 | `call_…` (**client-minted**) |

**All five verified live**, each replaying under its own id format — which is
the point of carrying provider ids rather than minting them.

Google is the exception that proves the rule: the Gemini API issues no tool-call
ids, so the client mints them (and must keep the id→name map per request, or a
recycled `call_0` mislabels an earlier response — see
`test_google_message_translation.py`). What Gemini *does* issue is a
`thought_signature`, captured at 552 bytes per call and carried on the
`ToolCallPart`.

A 10-turn Gemini run replayed **74 tool_use / 74 tool_result pairs with zero
`INVALID_ARGUMENT`** — the failure mode this design was most exposed to.
Honest caveat: strict `thought_signature` enforcement is a Gemini **3**
behavior, and `gemini-3-pro-preview` is retired for this key, so what is proven
is that signatures are captured and carried and that 2.5 replay is clean. The
strict path is argued, not measured.

**Bedrock took two fixes, neither of them code.** An access-key pair was
rejected by STS itself (`InvalidClientTokenId`); a bearer API key
(`auth_mode: api_key`) works, and its embedded credential scope names the real
region — `eu-west-1`, not the region we had configured. Then the bare model id
returns `ValidationException: Invocation of model ID … with on-demand
throughput isn't supported`; newer Anthropic models on Bedrock must be invoked
through a **regional inference profile**
(`eu.anthropic.claude-haiku-4-5-20251001-v1:0`). Both are worth surfacing in
the provider setup UI — an admin pasting a plain model id hits a wall with no
hint about inference profiles.

Bedrock reports `cache_read = 0`: prompt caching there needs explicit
`cachePoint` markers, which is Phase 3 work, not a regression.

Google needed a working key: the first one authenticated (429, not 401) but had
zero quota on every reachable model. `gemini-3-pro-preview` is retired
outright; `gemini-2.5-flash` and `gemini-2.5-pro` both work.

---

## 2e. Compaction and overflow, measured

Two mechanisms are easy to conflate. They are independent and were verified
separately.

- The **decay ladder** (§3.4) shrinks the in-run transcript — tool results
  inside one turn.
- **Cross-turn compaction** (`context_compaction_service`) folds settled
  *completions* into a rolling `<history_summary>` and advances a watermark.

### 2e.1 Neither had ever fired in the sandbox

Across every log from every provider run: zero `Auto-compacted`, zero context
overflows, and `report_context_states` had **no rows**. A 10-turn run produces
~20 completions against a trigger of >40 completions / >12.5k tokens, so
nothing ever crossed a threshold. The same is true of the decay ladder — every
tool self-caps its output (largest observed transcript 6,321 tokens against an
8,000 budget; largest single tool result 4,482 chars), so the ladder is a
safety net, not a steady-state path. Both were forced deliberately to test
them.

Forcing compaction by API alone was not enough: `POST
/api/reports/{id}/context/compact` returned `nothing_to_compact`, because
`PROTECT_LAST_MIN = 12` plus `PROTECT_FIRST_N` plus the tail-token floor
covered all 20 completions. It only becomes reachable by shrinking the declared
`context_window_tokens` (200,000 → 8,000 → conversation budget 1,000, trigger
500, tail 100).

### 2e.2 Compaction fires, and then keeps firing on its own

With the window shrunk, one forced pass compacted 6 turns / ~434 tokens and
wrote a `report_context_states` row. From there **auto-compaction took over
unprompted** — four more passes, one after each subsequent turn
(`+2 turns/284 tok`, `+2/241`, `+2/169`, `+2/153`), advancing the watermark
from completion 7 to completion 15 of 28. So the trigger works; it simply never
gets near its threshold at realistic conversation lengths.

The summary is structured (`goal`, `progress`, `key_decisions`, `entities`,
`critical_context`, `opening_request`, `next_steps`, `constraints_preferences`)
and was factually correct against the source CSV.

### 2e.3 The summary survives the Phase 3 prompt move

Phase 3 moved `messages_context` out of the static prefix and into the turn
head, so the obvious regression is a summary that silently stops reaching the
model. It does reach it: the rendered planner payload carries
`<history_summary>` and the raw `<conversation>` block drops from 14 turns to
8–9.

### 2e.4 Recall grading — does the model actually use the summary?

Four questions were asked *after* compaction, each about a fact whose raw turn
was behind the watermark. For each, the payload was split into the
`<history_summary>` and `<conversation>` regions and each fact located, so
"answered from the summary" is a measurement rather than an assumption.

| ask | fact present in summary | present in raw turns | answer | ground truth | verdict |
|---|---|---|---|---|---|
| first thing I asked? | ✅ | ✅ (turn 1 is protected) | verbatim quote | ✅ | pass (weak — both sources) |
| total revenue earlier? | ✅ | ❌ | $265,370.23 | $265,370.23 | **pass (summary-only)** |
| rows/columns of sales.csv? | ✅ | ❌ | 200 rows, 6 columns, all six names | 200 × 6 | **pass (summary-only)** |
| summarise the session | ✅ | partial | 4 regions + 4 products + avg units, all exact | exact | pass |

Two of the four are strict tests — the number existed *only* in the summary at
answer time — and both passed. The session summary's regional and product
figures were checked against the payload too, and every one was present in
context; none were reconstructed from memory.

One caveat worth stating plainly: compaction is faithful to the transcript, not
to the data. The summary carries `17 customers in enterprise tier` because that
is what the agent computed earlier; ground truth is 19. That is the known
pre-existing bug where customer questions get answered off `sales.csv`, and
compaction propagated it exactly as it should.

### 2e.5 The overflow path, exercised end to end

`_handle_context_overflow` had never run — the transcript-decay wiring added to
it was untested. Driving it needs a real `context_length` rejection, which the
existing fault injector can produce (`BOW_AGENT_LOOP_FAULT_KIND=context` raises
an Anthropic-shaped 400 that `classify()` maps to `context_length`).

Injected mid-run at `loop_index=1`, the full remediation chain ran:

1. factor shrink `1.00 → 0.76`, parsed from the provider's own
   `250000 tokens > 200000 maximum` (not the blind 0.85 decay);
2. `trimming context to shrunk window 6080 (factor 0.76)` on the retry;
3. forced synchronous compaction (2 turns, ~190 tokens);
4. retry succeeded — the answer was `$1,326.85`, exactly `265,370.23 / 200`.

The transcript-decay branch is reached but is a no-op here: at loop index 1 the
transcript is far below even the shrunken budget, so `digested` and `dropped`
are both 0. Confirmed reachable and harmless; the decaying behavior itself is
covered by the ladder tests, not by this path.

---

## 3. Target architecture

### 3.1 The part

```
TextPart:        text
ThinkingPart:    text, signature, provider_name
ToolCallPart:    id, tool_name, args, signature, provider_name
ToolResultPart:  call_id, tool_name, outcome, content, digest, metadata, tokens, timestamp
```

- **`content`** — model-visible, budgeted, full while fresh.
- **`digest`** — the tool's own compact form, computed **once at execution
  time**. This is what today's `_digest_*` functions produce; they move next to
  their tools.
- **`metadata`** — UI / media / audit only. **Never** sent to the model, and
  **never** fed to the compaction summarizer.
- **`outcome`** — `success | failed | denied | interrupted`. A field, not a
  heuristic; replaces `_observation_failed()` sniffing (`agent_v2.py:3337`).
  `denied` binds to `ToolConfirmation.STATUS_DENIED`; `interrupted` to the
  sigkill path (`planner_v3.py:211`), which today drops in-flight calls silently.
- **`tokens`** — local estimate of `content` size, recorded at write time. See
  §4.3 for why this is *not* the billing number.

### 3.2 The transcript

Append-only, `id` / `parent_id`. Parent linkage matches the existing fork
feature and lets compaction record lineage rather than mutate history.

Prompt assembly becomes:

```
system                    ← static, cached
tools                     ← static, cached
user[static context]      ← instructions, schemas, files, resources, MCP index — cached
user[the ask]
assistant[tool_use…] / user[tool_result…]    ← grows; each settled turn cacheable
user[per-turn head]       ← time, steering, routing hint, notes nudge
```

Volatile content moves to the **tail**. Backed by `ToolExecution` +
`PlanDecision`, which already carry the fields.

### 3.3 One transport seam

`to_model_messages(parts, model) -> (system, list[Message], tools)` is the only
place parts become provider messages. It owns:

- truncation to the model's budget, with a visible truncation notice
- media hoisting where a provider can't take images in a tool result
- dict-vs-string result encoding (Gemini requires a dict)
- native error channels vs. JSON-framed `{"error": …}` per provider
- signature replay, **gated on `provider_name`**

It feeds the six `_translate_messages` implementations that already exist. No
new client methods.

### 3.4 The decay ladder

Compaction is not one operation. It is four tiers:

| Tier | Representation | Cost |
|---|---|---|
| 0 | full `content` | free — current + recent turns |
| 1 | `digest` | **free** — field swap, no LLM |
| 2 | folded into a compaction part | one `small_model` call, amortized |
| 3 | dropped, referenceable by id | free |

Tier 1 is why this gets cheaper, not more expensive. Tier 3 is safe because
`read_query` / `read_artifact` / `read_file` exist precisely so the agent can
re-fetch — the id survives, so dropping is not lossy.

**Invariants**

- Never split a call from its result when choosing a decay boundary; shift the
  boundary to the assistant message instead.
- Never drop a call — synthesize an `interrupted` result rather than orphan it.
- Summarize `content` / `digest` only; `metadata` never reaches the summarizer.
- Drop thinking parts on decay — signatures are provider-bound and short-lived.
- `context_length` error → shrink the trim budget, decay the transcript,
  compact, retry. This already exists in `_handle_context_overflow`; it is
  excluded from *model-fallback* eligibility (`fallback.py`), which is not the
  same as being terminal. Verified end to end — see §2e.5.

---

## 4. Cross-cutting requirements

### 4.1 Cross-LLM: provider-opaque state

Not a Gemini quirk — three providers, three mechanisms, one carrier.

| Provider | Carrier | If dropped |
|---|---|---|
| Google | `thought_signature` on every function call | **400 INVALID_ARGUMENT** |
| Anthropic | thinking-block `signature` | quality degradation |
| OpenAI Responses | reasoning items | **unknown — must be measured, not assumed** |

`ToolUseCompleteEvent.signature` (landed on main) is the general carrier.
`provider_name` must ride with it: `FallbackController` swaps providers
**mid-run** on rate-limit / quota / overload, and a signature from one provider
is meaningless — or fatal — to another. Rule: **replay a signature only to the
provider that issued it; otherwise drop the part.**

### 4.2 Translator bugs to fix first

Four silent data-loss bugs, all in the block path, all blocking the transcript:

1. **Text dropped beside tool results** — `if tool_results: … elif tool_calls:`
   emits only the `tool` messages; `text_blocks` vanish
   (`openai_client.py:238`, `azure_client.py:139`,
   `openai_responses_client.py:187`). This is exactly the shape §3.2 produces
   (tool results + per-turn head in one user turn). It arrives on Anthropic and
   disappears on OpenAI/Azure — silently.
2. **Only `text_blocks[0]`** used for assistant content; further text dropped.
3. **No `image` block handling** in `openai_client.py` / `azure_client.py` —
   falls to the `else` branch and renders as an empty string.
4. **Responses: images gated on string content** —
   `openai_responses_client.py:171` attaches images only when the last user
   message is a plain string. The moment it becomes a tool-result list, every
   image silently stops reaching the model.

Provider routing means both Azure and OpenAI can land on *either* client
(`llm.py:127-158`), and an org can flip between them — every fix covers both
paths.

### 4.3 Tokens, cost, quota

Two different numbers. Do not conflate them.

**Billing / quota — provider-reported, authoritative.** `UsageEvent` carries
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`;
`pop_last_usage()` is the fallback when a client emits no event
(`llm.py:620-680`). This already flows into `_schedule_usage_record` →
`LLMUsageRecord` and `_record_usage_limit_async`. **Unchanged by this work.**

**Budget decisions — local estimate, per part.** The decay ladder needs to know
what each part costs *before* the call. The provider cannot tell us that.
`ToolResultPart.tokens` is written at execution time and summed locally. This
replaces `_estimate_total()`'s `json.dumps` of the whole observation list on
every call (`context_hub.py:175-193`) with arithmetic.

**What changes, and what to watch:**

- Cache tokens become the headline metric. `_quota_total_tokens`
  (`llm.py:842-853`) already adds cache tokens to the quota total **for
  Anthropic only** — verify that's still correct once Bedrock and Gemini caching
  are enabled in Phase 4, since their billing models differ.
- `_calc_input_cost` already prices cache read/write separately. Expect **total
  input tokens to rise** (a transcript grows where a blob was stripped) while
  **cost falls** (cache reads are a fraction of fresh input). Track cost, not
  token count — a token-count regression alarm will fire spuriously.
- New per-run metrics: cache hit ratio, uncached input tokens, TTFT
  (`llm.py:626-629` already records it), pre-LLM rebuild wall-time.

### 4.4 PII

`_apply_pii_v2` (`llm.py:296-363`) **already redacts `tool_result` content** —
it walks every block and redacts both `text` and `content` keys, and block-mode
rules raise `PiiPromptBlockedError`. This is correct today and stays correct
for a transcript, because redaction happens at the LLM boundary, after
`to_model_messages`.

Four things to get right:

1. **Redaction is send-time, not store-time.** `ToolExecution.result_json` and
   the persisted `digest` hold raw values. That is the intended audit behavior —
   but it means **the digest must never be rendered to a surface that isn't
   PII-gated**, and the UI path needs the same treatment `display.py` already
   applies.
2. **`metadata` is never sent, so it is never redacted.** Anything routed from
   `metadata` into `content` later must pass through the redactor — enforce by
   construction (metadata is not an input to any model-visible field).
3. **The compaction summarizer is an LLM call.** It must go through the same
   facade so `_apply_pii_v2` applies. A summary built from unredacted content
   would launder PII into a persisted part that is replayed for the rest of the
   run.
4. **Cost of re-redaction.** Today one message is scanned per iteration. With a
   transcript, the whole history is re-scanned every call. Redact once at part
   creation and mark parts clean, or the redactor becomes a per-iteration
   O(history) cost — measure before choosing.

---

## 5. Phases

Each phase is independently shippable except where noted.

### Phase 0 — latency, no architecture

- Move the `<time>` block off token zero → tail of the user message
  (`clock.py`, `prompt_builder_v3.py:573`).
- Batch `message_builder.build()`'s N+1: one query for blocks across
  completions, one for their `ToolExecution` rows.

No dependency on anything below. Measurable alone.

### Phase 1 — result contract (no behavior change)

- Define `ToolResultPart` (§3.1); `outcome` enum wired to `ToolConfirmation`
  and the sigkill path.
- Move `_digest_*` functions next to their tools; each tool declares its digest
  and token budget in `ToolMetadata` (which already has `output_schema` and
  `observation_policy` to build on).
- Persist `digest`, `tokens`, `signature`, `provider_name` on `ToolExecution`.
- Fix the four translator bugs (§4.2).
- **Both existing renderers keep working**, reading from the new shape.

Gate: rendered output unchanged. This is a pure refactor and de-risks everything
after it. Landable tool-by-tool.

### Phase 2 — transcript + decay ladder (ship together)

- Typed parts, append-only, `id`/`parent_id`.
- `to_model_messages()` seam.
- Ladder tiers 0-3, pair-preserving boundaries, `small_model` for tier 2,
  reusing `context_compaction_service`'s summary structure.
- Redaction-cost decision from §4.4(4).
- **Behind a flag**, A/B against the current path.

Gate: **do not ship the transcript without the ladder.** Long runs would regress
on cost and hit context limits.

### Phase 3 — caching

**Prerequisite (from §2b.1): make the cached prefix actually stable.** Without
this the rest of the phase is a no-op.

- Split schemas and instructions into a **stable identity block** (cacheable)
  and a **volatile signal block** (usage, success rate, recency) rendered in the
  per-turn tail. Stable sort order in the cached block; ranking applies to
  selection, not output order.
- Or, as the cheap interim: snapshot usage stats once per run and render from
  the snapshot.
- Drop the per-tool `build(with_stats=True)` re-render (`agent_v2.py:4958`),
  which is waste once the block is snapshot-stable.
- Static context as its own cached turn; breakpoint on the settled prefix.
- Per-provider cache hints beyond Anthropic (Bedrock `cachePoint`, Gemini
  cached content). `enable_cache` already exists on the Anthropic client
  signature but the facade never passes it.
- `context_length` → compact and retry. **Done and verified** (§2e.5).

Gate: assert byte-stability of the cached prefix across iterations of a real
run before enabling breakpoints (§6.5).

### Phase 4 — delete the workarounds

- Vision TTL, `_carry_substantive_observation`, `_aggregate_batch_observation`.
- `trim_context_to_budget`'s XML tail-slicing → budget arithmetic over part
  sizes.
- Replay thinking parts, gated on `provider_name`.

### Phase 5 — collapse the redundant renderers

With the transcript persisted, `message_context_builder`'s completion/block
traversal, N+1 loads, if/elif dispatch and the duplicate `_OBS_KEEP_KEYS`
policy all go. **The digests themselves stay** — they moved to their tools in
Phase 1.

Also in scope, per §2b:

- Delete `messages_section._ID_PATTERNS` / `_minify_message` — the regex
  round-trip is unnecessary once ids are fields (§2b.3).
- Point `query_context_builder` and `_build_available_steps_context` at the
  persisted parts instead of re-deriving from `Query` / `Step` rows (§2b.2).
- Delete `WidgetContextBuilder` and its `ContextSnapshot.widgets_context`
  plumbing (§2b.5).
- Collapse the seven windowing policies into the ladder (§2b.4).

---

## 6. Verification

Per `.claude/skills/sandbox-feedback-loop`, extended for multi-provider.

### 6.1 Sandbox

Boot per the skill (backend :8000 with `BOW_DATABASE_URL='sqlite:///db/app.db'`,
frontend :3000, seed via `/users/sign-up`, skip onboarding, configure models in
`/settings/models`).

**Deviation:** the skill configures one provider through the UI. We need six.
Seed providers via the API instead (JWT from the `auth.token` cookie +
`X-Organization-Id`) — the UI flow is fiddly per provider and the model
checkboxes carry no accessible labels. Keep the UI path only for a single
smoke-test provider.

Per provider, enable one cheap model. Verify in DB that `llm_models` holds
exactly the expected rows.

### 6.2 The provider matrix

The point of this work is cross-LLM parity, so the matrix is the deliverable:

| Provider | Client path | Must prove |
|---|---|---|
| anthropic | `anthropic_client` | thinking signature replay; cache breakpoints |
| google | `google_client` | `thought_signature` replay (400 if wrong); dict-encoded results; recycled call ids |
| openai (default) | `openai_responses_client` | `function_call_output` round-trip; reasoning-item replay; images beside tool results |
| openai (base_url) | `openai_client` | chat-completions round-trip |
| azure (default) | `azure_client` | chat-completions round-trip |
| azure (`use_responses_api`) | `openai_responses_client` | same as openai Responses |
| bedrock | `bedrock_client` | Converse round-trip; image-before-tool_result ordering |
| custom | `openai_client` | compatible-endpoint round-trip |

Each runs the same eval set. Extend `tests/integrations/llm_clients.py:499`
(`tool_result_round_trip`) to a **multi-turn, multi-tool, with-reasoning** case
per provider — that is the single highest-signal test for this work, and it
already exists in skeleton.

### 6.3 Evals per model

Use the existing infrastructure: `TestSuite` / `TestCase` /
`TestRun` / `TestResult`, with `additional_turns_json` for multi-turn cases.
`TestResult` already carries `agent_execution_id` and `result_json`, so tool-call
sequences are recoverable per run.

Suite shape — a handful of cases, each targeting a failure this design predicts:

- single tool → answer (baseline)
- parallel batch in one turn (proves N tool_results in one user turn)
- long run whose transcript exceeds the budget (proves the in-run ladder)
- a conversation past the compaction watermark, then asked to recall a fact
  that survives only in the summary (proves cross-turn compaction — §2e.4;
  note this needs a shrunken declared window, since real thresholds are never
  reached at realistic lengths)
- a failing tool then recovery (proves `outcome=failed` + error channels)
- a denied confirmation (proves `outcome=denied`)
- an interrupted run (proves synthesized `interrupted`, no orphan call)
- an image-bearing tool result (proves media hoisting on all four client paths)

Run the suite per provider. Compare tool-call sequence and final answer between
the flag-off and flag-on paths — divergence in the sequence is the signal, not
just a pass/fail.

### 6.4 Capturing input/output per model run

`BOW_PLANNER_DUMP_FILE` (`planner_v3.py:90-115`) already appends
`{ts, system, messages, tools}` as jsonl. Extend it to a full I/O record:

- **input**: system, messages (post-`to_model_messages`, **post-PII** so the
  artifact is safe to keep), tools, provider, model, cache breakpoints
- **output**: normalized `LLMStreamEvent` sequence, `stop_reason`, `UsageEvent`
- **derived**: prompt/completion/cache tokens, cost, TTFT, wall time

Two sinks, both useful:

- **Files** — one jsonl per run under the scratchpad, keyed by
  `agent_execution_id`. Cheap, diffable, ideal for the build-verify loop and for
  eyeballing what actually went over the wire per provider.
- **DB** — `TestResult.result_json` for eval runs, so the matrix is queryable
  and regressions are comparable across runs.

Plus the skill's HTTP layer: log every `/api/` response via
`page.on('response')` into jsonl, and read `httpx` lines in the backend log to
confirm real provider calls and status codes.

### 6.5 Highest-signal check

The skill's §5 pattern — `import main` to register mappers, then run a builder
directly against real rows — is the equivalent of unit-testing the seam:

```
build the transcript for a real report_id
→ to_model_messages(parts, model) for EACH provider
→ assert every provider receives the same semantic content
```

That single check catches all four §4.2 bugs, the signature gating, and any
media-hoisting divergence, without booting the UI.

### 6.6 Loop

Small numbered Playwright scripts (`01_signup.js`, `02_providers.js`, …) with
`storageState` carrying the session. On selector failure: screenshot, read it,
fix, rerun. Per the skill — do not guess selectors.

---

## 7. Risks and rollback

| Risk | Mitigation |
|---|---|
| Transcript ships without the ladder → cost/context regression | Phases 2's gate; flag-off rollback |
| Signature replay fatal on Gemini | Provider-gated replay; the round-trip test is the guard |
| OpenAI reasoning-item behavior unknown | Measure in Phase 1 before designing for it — do not assume |
| PII re-scan becomes O(history) per call | Measure; redact-once-and-mark if hot |
| Token-count alarms fire on expected growth | Alarm on **cost** and cache-hit ratio, not raw input tokens |
| Prompt-cache invalidation from a stray volatile field | **Known live: live `TableStats` in the schema block and usage-ranked instruction ordering (§2b.1).** Assert byte-stability of the cached prefix across iterations in the sandbox |
| Splitting stable/volatile schema signal changes ranking behavior | Ranking reads stats as today; only *rendering* moves. Cover with an eval case whose answer depends on table ranking |

Rollback is the flag: Phase 1 is behavior-neutral, Phase 2 is the only
switchable change, and Phases 3-5 are strictly subtractive once 2 is proven.

---

## 8. Open questions

1. Does OpenAI Responses degrade or hard-error without reasoning-item replay?
   **Measure in Phase 1.**
2. Should `digest` be stored redacted, raw, or both? Depends on which surfaces
   render it (§4.4(1)).
3. Do Bedrock and Gemini cache tokens belong in `_quota_total_tokens`? Their
   billing differs from Anthropic's.
4. Is `parent_id` lineage worth it in Phase 2, or deferrable until the fork
   feature needs it?
5. Per-tool token budgets — declared as absolute counts, or as a share of the
   model window?
6. Do live usage stats in the schema block earn their tokens at all? They exist
   to bias the model toward proven tables — if ranking alone achieves that,
   rendering `<usage>` / `<success_rate>` / `<last_used_at>` could simply be
   dropped, which is cheaper than splitting stable from volatile (§2b.1).
7. Are `<queries>` / `<available_steps>` still earning their place once the
   transcript carries the same results, or are they redundant with it (§2b.2)?
