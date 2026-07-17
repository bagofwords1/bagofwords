# Feedback Loop — "the longer the report, the longer time-to-first-token takes — and it's not (only) the LLM"

On a report with many prompts/completions (create_data, create_artifact,
edit_artifact, …), the time from submitting a prompt to the first streamed
token grows with every turn. The claim under test: the growth comes from how
`agent_v2` / `completion_service` build and ship context, not just from the
model itself.

## Root cause (validated)

Measured on a live stack (Anthropic Haiku, 30-turn report, Music Store demo
data). Per-decision metrics come from `plan_decisions.metrics_json`; phase
timings from the existing `[stream:]` / `[agent:]` / `[context_hub:]` log
markers.

1. **The whole report context was an UNCACHED user message on every planner
   call** — the dominant, unbounded growth term.
   `prompt_builder_v3.py:_build_user_message` packed instructions, schemas,
   files, resources, history, artifact state and observations into one user
   message; the Anthropic client (`anthropic_client.py:294`) placed prompt-cache
   breakpoints only on the (static) system prompt and the last tool.
   Evidence: `cache_read_tokens` was pinned at 23,757 (system+tools) for all
   38 pre-fix decisions while uncached `prompt_tokens` grew turn over turn
   (loop-0: 2,690 → 6,372 over 16 turns; within-turn iterations reached
   21,519). Provider TTFT tracked uncached tokens ≈ linearly: ~0.7s at 3k,
   ~2.4s at 21k. A production-size report context (50–150k tokens) extrapolates
   to many seconds of TTFT per planner call, every iteration, every turn.

2. **`LoadablesResolver.list_for_discovery` hydrated up to 25 FULL `Step` rows
   (including `data` — all result rows) per planner iteration**
   (`loadables.py:_report_default_steps`, `select(Step)`) just to render
   id/title/columns/row-count for `<available_steps>`. Cost grows with the
   data volume of recent steps.

3. **`AgentV2._get_active_artifact` cascade-loaded every visualization's
   query, its whole step list and each step's `data` JSON** — the
   `Visualization.query` / `Query.steps` / `Query.default_step` relationships
   are `lazy="selectin"`, so summarizing N artifact visualizations hydrated
   every result-row blob behind the artifact, once per turn, growing with
   each artifact edit (`agent_v2.py:_get_active_artifact`).

4. **`MessageContextBuilder.build` (per planner iteration via `refresh_warm`)
   ran one blocks query per system completion plus a FULL `ToolExecution`
   entity load per block** — `result_json` for artifact tools carries the
   whole generated code (10–26KB each in the sandbox; grows with artifact
   size) and for create_data the full result rows. Bounded by the 20-message
   window, but the per-message payloads grow with content size.
   (`message_context_builder.py:build`, old lines ~1448–1473.)

   Measured backend pre-LLM time (`stream_start` → `loop_starting` + agent
   init) grew from ~0.9s to ~1.6–2.2s over 16 turns; `refresh_warm:messages`
   alone went 81ms → 313ms.

Not growth, but a notable fixed cost: `AgentV2.__init__` (tool registry +
context hub construction) takes ~0.5s per completion before any context work.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
BOW_DATABASE_URL="sqlite:///db/app.db" uv run pytest tests/unit/test_planner_prompt_cache_blocks.py -q
```

Pre-fix these assertions FAIL: schemas/instructions/files/resources leak into
the (uncached) user message, `PlannerInputV3.system_blocks` doesn't exist, and
the Anthropic request carries no per-block cache breakpoints. Post-fix: 4 passed.

Related existing suites that pin the DB-projection changes:
`tests/e2e/test_loadables.py` (discovery listing), unit `-k "message_context"`.

## Loop B — live confirmation (real Anthropic key)

Requires `ANTHROPIC_API_KEY` (env var only — never committed).

```bash
tools/agent/boot_stack.sh                       # or backend-only, see below
cd backend && uv run python ../tools/agent/seed_org.py --demo
# configure an Anthropic provider + default Haiku model via /api/llm/providers
# then drive a long report and record submit -> SSE-milestone latencies:
uv run python ../tools/agent/measure_ttft.py --data-source-id <ds_id> --turns 16
```

The harness POSTs `/api/reports/{id}/completions` with `stream: true` and
records per turn: HTTP headers, `completion.started`, first `block.upsert`
(context build done), first `block.delta.*` (first token surfaced), and
`completion.finished`; backend log offsets pair each turn with its
`[stream:]`/`[agent:]`/`[context_hub:]` phase lines, and
`plan_decisions.metrics_json` yields per-call `prompt_tokens` /
`cache_read_tokens` / `first_token_ms`.

**Observed pre-fix** (16 turns, one report): first-delta 1.66s → 3.5s+ and
climbing; `cache_read_tokens` constant 23,757; uncached prompt growing every
turn (numbers above).

## The fix

1. **Cache the report context** — `PromptBuilderV3.build` now splits the
   prompt into ordered system blocks, most stable first: behavior prompt
   (static per mode) → `<report_context>` (schemas, files, resources,
   data-visibility) → instructions (stable per turn). The user message keeps
   only per-turn/per-iteration content (time, prompt, mentions, entities,
   history, artifact, observations). `PlannerInputV3.system_blocks` carries
   the split; the LLM facade forwards it only to clients declaring
   `supports_system_blocks`; the Anthropic client puts a `cache_control`
   breakpoint on each block (≤3, the 4th is the last tool). Other providers
   receive the byte-equivalent joined string.
2. **Projected step discovery** — `list_for_discovery` selects
   id/title/slug/`data['columns']`/`data['info']['total_rows']`/
   `json_array_length(data['rows'])` (native JSON ops on Postgres AND SQLite),
   never the row payloads; full-entity load kept only as fallback.
3. **Projected artifact summaries** — `_summarize_artifact_visualizations`
   replaces the ORM traversal with three batched column-projected queries
   (visualizations → queries → steps metadata + row counts computed in-DB).
4. **Batched, projected history builder** — `MessageContextBuilder.build`
   loads all blocks in one query and reuses the projected tool-execution
   loader; artifact tools get a Postgres projection of light metadata keys
   (no `code`), create_data keeps its existing rows-free projection, and the
   loader now also carries `arguments_json` (the describe_tables digest read
   it but the projection never loaded it).

**Observed post-fix** (same report, now ~30 turns deep — i.e. HARDER than the
pre-fix measurement): first-delta flat at 2.0–2.4s; per-decision provider TTFT
0.8–1.1s (was 1.5–2.4s at shallower history); `cache_read_tokens` rose to
26,084 (schemas/instructions riding the cache) and loop-0 uncached
`prompt_tokens` dropped 6,372 → ~3,5k. In production the moved blocks are far
larger than this sandbox's 4KB schema, so the absolute win scales with org
size.

## What this proves / regression notes

- Submit→first-token growth on long reports is dominated by re-processing the
  report context as uncached input on every planner call, amplified by
  backend context-build costs that hydrate full result-row/artifact-code
  blobs on the pre-first-token path. Both are now bounded.
- Remaining (documented, not fixed here):
  - The per-turn user message (history, artifact, observations) is still
    uncached; within-turn iterations re-pay it. A 4th breakpoint over a
    frozen per-turn prefix is the natural next step.
  - `AgentV2.__init__` ~0.5s fixed cost per completion (registry/hub built
    from scratch each run).
  - `POST /completions/estimate` runs a full context build (30s TTL cache) —
    pre-submit UI latency on big reports.
- Pre-existing unrelated failure: `tests/unit/test_new_report_command.py::
  test_new_report_phrase_is_a_normal_prompt` fails identically with this
  change stashed (verified).
