# Feedback Loop — the agent loop re-pays for context it already sent

Agent runs bill far more input tokens than the conversation contains, because
the prompt prefix the loop is built to cache is not actually cacheable. Three
separate causes were validated against real Claude Haiku 4.5 runs: the planner's
static context mutates between iterations, the coder and visualization prompts
carry no cache breakpoint at all, and the prefix that does cache expires between
user turns. On a 3-turn scenario the measured cost was $0.215; the same scenario
after the fixes costs $0.140 in the realistic-gap regime.

## Root cause (validated)

**1. `schemas_excerpt` is overwritten with a different renderer after every tool
dispatch.** `backend/app/ai/agent_v2.py:6398` reassigned it from a bare
`view.static.schemas.render()`, bypassing `_render_schemas_with_roster()`
(`agent_v2.py:1220`), which is what the pre-loop render at `agent_v2.py:4240`
and the focus-change render at `agent_v2.py:4487` both use.

The two renderers emit different vocabulary for the same data:
`render_combined()` emits `<data_source name=… total_tables=…>`/`<description>`
(`backend/app/ai/context/sections/tables_schema_section.py:1215`), while the
section's default `render()` uses its `tag_name` and emits
`<agent name=…>`/`<context>` (`tables_schema_section.py:303`). The block lives in
`messages[0]` — the run-stable prefix `PromptBuilderV3._build_static_context()`
exists to make cacheable (`backend/app/ai/agents/planner/prompt_builder_v3.py:682`)
— so the tag flip invalidated the message cache on every turn. `static` is primed
once per run and does not change on a warm refresh, so the reassignment had no
purpose beyond the damage.

Secondary, same line: `render()` skips the roster/focus filtering, so from loop 1
onward every attached agent's full schema shipped regardless of
`report.focused_data_source_ids`.

**2. The coder and visualization prompts cannot be cached at all.**
`backend/app/ai/agents/coder/coder.py:970` and
`backend/app/ai/tools/implementations/create_data.py:1043` call
`inference_stream_v2` with a single user message, no `system` and no `tools`.
`inference_stream_v2` only attaches `cache_control` to `system`, to the last
tool, and to `msgs[-2]` when `len(msgs) > 2`
(`backend/app/ai/llm/clients/anthropic_client.py`), so none of those three
applied and no marker was ever attached.

The content was highly repetitive but ordered so it could not help: 83–97% of
each coder prompt was byte-identical to the previous one, while the shared
*prefix* was only 357–2,841 chars, because `<user_prompt>` sat at offset ~2,754
ahead of ~25k chars of stable schemas/resources/rules.

**3. The prefix that does cache expires between turns.** `cache_control` was
`{"type": "ephemeral"}` — the 5-minute TTL, measured from the start of the
request. A person reading an answer before typing the next question routinely
exceeds that, so the ~39k-token tools+system prefix was re-written at the 1.25×
write rate on the first call of most turns instead of being read at 0.1×.

Also found and fixed: `ALLOWED_VIZ_TYPES` is a set literal
(`create_data.py:68`), so `list()` ordering varied per process — prompt
nondeterminism inside what is now a cached prefix.

## Loop A — deterministic reproduction (no external services)

`tests/unit/test_anthropic_cache_ttl.py` asserts the cache markers the client
puts on the wire, with the provider stubbed at the network boundary.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  uv run pytest tests/unit/test_anthropic_cache_ttl.py -q
```

With cause 3 reverted (`cache_control` back to a literal `{"type":
"ephemeral"}`):

```text
FAILED tests/unit/test_anthropic_cache_ttl.py::test_prefix_survives_a_gap_between_turns
E   AssertionError: assert {'type': 'ephemeral'} == {'ttl': '1h', 'type': 'ephemeral'}
FAILED tests/unit/test_anthropic_cache_ttl.py::test_long_ttl_blocks_precede_short_ttl_blocks
2 failed, 9 passed
```

After the fix: `11 passed`.

Causes 1 and 2 are prefix-stability properties that only manifest across real
planner iterations, so they are covered by Loop B rather than a unit assertion.
The coder prompt assertions in `tests/unit/test_codegen_short_code.py` and
`tests/unit/test_relative_date_prompts.py` were made channel-agnostic (they now
record `system` alongside `messages[0].content`) so they keep testing "the model
is told X" rather than which field carries it.

## Loop B — live confirmation (real Anthropic credentials)

Requires `ANTHROPIC_KEY`. Boot the sandbox per the skill, point the org at
Claude Haiku 4.5, install the `chinook` demo agent, then drive the same three
prompts through `POST /api/reports/{id}/completions` and read
`llm_usage_records` (which already stores `cache_read_tokens` /
`cache_creation_tokens` per call, grouped by `scope`).

Cause 1 — planner `messages[0]` across the six planner calls of one run:

```text
before                                   after
call  tag             prefix-vs-prev     call  tag             prefix-vs-prev
0     <data_source>   -                  0     <data_source>   -
1     <agent>         19% (1563)         1     <data_source>   100% (7267)
2     <data_source>   21% (1563)         2     <data_source>   100% (7267)
3     <agent>         19% (1563)         3     <data_source>   100% (7267)
```

Cause 2 — the coder's `system` half, same SHA on every call:

```text
before: call0..call5   rd=0      wr=0        (no marker ever attached)
after:  call0 wr=5475  call1 wr=5475
        call2..call5   rd=5475   wr=0
```

Cause 3 — 400s gaps between turns, 5m TTL vs 1h TTL:

```text
5m TTL                                   1h TTL
t+412  planner  rd=0      wr=43189       t+411  planner  rd=38902  wr=4615
t+828  planner  rd=0      wr=44023       t+828  planner  rd=38902  wr=5515
cache_creation total: 102,308            cache_creation total: 14,933
```

Isolated API check that the TTL itself is honored (no beta header needed), a
15.4k-token prefix re-read after 420s:

```text
write:  cache_creation_input_tokens 15412, cache_creation.ephemeral_1h 15412
+420s:  cache_read_input_tokens     15412, cache_creation 0
```

## The fix

- `backend/app/ai/agent_v2.py:6398` — drop the reassignment; the pre-loop
  render already holds the correct value and focus changes are handled in the
  loop head.
- `backend/app/ai/agents/coder/coder.py` — split `generate_code`'s prompt into a
  run-invariant `system_text` (role, org instructions, sandbox/ML/time rules,
  guidelines) and the per-call user half, which keeps its original block order.
- `backend/app/ai/tools/implementations/create_data.py` — same split for the
  visualization prompt; `sorted(ALLOWED_VIZ_TYPES)` for prefix determinism.
- `backend/app/ai/llm/clients/anthropic_client.py` — `_prefix_cache_control()`
  puts the 1-hour TTL on tools + system only; the moving message breakpoint
  keeps the 5-minute default. `BOW_PROMPT_CACHE_TTL=5m` rolls it back.

Measured, per `llm_usage_records`:

| scenario | before | after |
|---|---|---|
| fixes 1+2, back-to-back turns | $0.21548 | $0.18853 (−12.5%) |
| fix 3, 400s gaps between turns | $0.22269 | $0.13978 (−37.2%) |
| coder fresh tokens per call | 8,354 | 2,932 (−64.9%) |
| cache_creation, gap scenario | 102,308 | 14,933 (−85.4%) |

## What this proves / regression notes

The loop demonstrates that all three causes are prefix-stability problems, not
volume problems: the same content, reordered and marked, bills a fraction as
much. Output quality is unchanged — across four runs the agent produced the same
analyses in the same order with every completion `success`, the generated SQL
for the first step was byte-identical before and after, and the prompt templates
are preserved exactly (245→245 invariant coder lines, 166→166 visualization
lines; only run-varying data differs).

Bounds worth knowing before extending this:

- The visualization split caches nothing on Claude Haiku 4.5 — at ~2,700 tokens
  the prompt is below Haiku's 4,096-token minimum cacheable prefix (verified
  `rd=0/wr=0`). It is free either way and pays off on Sonnet/Opus.
- The 1-hour TTL costs 2× to write. Break-even is ~2 planner calls per hour per
  permission cohort; below that it costs more. The prefix is shared across
  reports (measured: 3 distinct reports read the same 38,902-token entry), so
  the bar is org-wide traffic, not turns in one conversation.
- Prompt-cache entries are server-side and content-keyed, so they outlive both
  the process and the report. A "before" run leaves entries an "after" run
  inherits *with the old TTL* — the first A/B here was contaminated that way and
  had to be re-run. Space TTL comparisons out or vary the prefix.
- Not addressed: `report.follow_ups`, `coder.py:1125`, `coder.py:1274`,
  `create_artifact.py:486`, `add_parameter.py:112` and
  `edit_artifact_legacy.py:763` still call the LLM with no `system`/`tools` and
  so cache nothing. `bedrock_client.py` sets no `cache_control` at all.
- The full `tests/unit` suite exceeds 40 minutes in a cloud sandbox and was not
  run to completion; the 40 files covering the changed paths pass (369 tests).
