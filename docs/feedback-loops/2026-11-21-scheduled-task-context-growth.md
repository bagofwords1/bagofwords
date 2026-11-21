# Feedback Loop — "scheduled prompt has 150 queries and growing each day … context conflates and becomes more expensive every day"

A customer runs a daily scheduled prompt that keeps running in its **host report**
(`spawn_new_report=False`). Each run writes 2 queries and updates the report's
artifact. After ~75 days the report holds ~150 queries and the customer reports
the run getting more expensive every day.

Claim validated here: *does planner input grow with the number of past runs in
host-report mode, and is that growth what makes it expensive?* This loop
measures it against the real agent, context builders, governor and compaction,
with only the LLM stubbed.

## Result in one paragraph

**Reproduced (Loop A2).** When each run *adds* its charts to the report's
dashboard with `edit_artifact` (old ones are kept automatically), per-run cost
keeps rising with no ceiling: compaction can't shrink `<current_artifact>`. When
each run *rebuilds* the dashboard (Loop A), growth levels off. In host-report mode, the conversation part of the
prompt grows about 1.2K tokens per run. The row-window governor plus
compaction folds it every ~12 runs, so it **saws between ~16K and ~29K tokens
and levels off**. Cost per run settles at roughly **+25–40% over run 1** and
stops climbing. Report-per-run mode stays flat. The bigger finding is that
**~71K tokens of every planner call are fixed overhead**: ~55K of tool
schemas (64 tools) plus ~16K of system prompt. That overhead is paid on every
iteration in both modes and is 2–4× the history part even at the peak. The
stub cannot reproduce "more expensive *every day*" without a ceiling. The
likely real-world causes are listed under *Open questions*, along with the
customer telemetry that settles them.

## Root cause (validated)

| Fact | Evidence |
|---|---|
| Host-mode runs replay earlier runs as real assistant/tool messages before the new prompt (durable transcript) | stub dump of run 2: `assistant(create_data…)`, `tool{…created_visualization_ids…}` precede the `<scheduled_execution>` user message |
| History is bounded by the **row window**, not the token budget | backend log: `[context] governor window full: 2 row(s) past the live window, fold=compacted` (`backend/app/ai/agent_v2.py:3603`) |
| Each fold folds 24 turns (12 runs × 2 completions) into the rolling summary | `[compaction] … trigger=auto_sync status=compacted method=fallback turns=24 history_tokens=10589->1638` (`backend/app/services/context_compaction_service.py:838`) |
| The token thresholds (fold at 75% of usable, `backend/app/ai/context/context_budget.py:29-31`) never fire for this workload on a 200K model | peak planner prompt ~101K est. < `summarize_at` 135K |
| Scheduled runs get the **full tool catalog** | 64 tools, 54,971 tokens (o200k). The largest are `create_eval` 3.7K, `edit_eval` 3.2K, `create_artifact` 3.0K, `create_data` 2.3K, `edit_instruction` 2.2K. `tool_policy_service.py:203-221` only makes "ask" tools fail closed for scheduled runs. It does not trim the catalog |
| Queries pile up in the host report, but the queries section is capped | 80 runs → 160 queries in the report. `<queries>` shows 5 (`query_context_builder.py:93`), observations 8 (`context_hub.py:91`) |

Hypothesis, **not** validated: "observations conflate". Observations are capped
at 8 and are per turn. The history that grows is the replayed transcript plus
the rolling summary.

## Loop A — deterministic reproduction (stub LLM, no credentials)

Files:
- `tools/agent/scheduled_growth_stub_llm.py`: an OpenAI-compatible stub that
  replays one run (2× `create_data` → `create_artifact` with
  `replaces_artifact_id` → ~1.2K-char analysis) and logs every request's size,
  split into `baseline_tokens` (system + tool schemas) and `conversation_tokens`.
- `tools/agent/scheduled_growth_drive.py`: creates a report + scheduled
  prompt through the API, fires `/trigger` N times, waits for each run's final
  planner round and prints one row per run.

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true ENVIRONMENT=production \
  TEST_DATABASE_URL=sqlite:///db/agent.db BOW_DATABASE_URL=sqlite:///db/agent.db \
  BOW_ENCRYPTION_KEY="$(uv run python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
# pin the key for the whole session; a backend restart without it can't decrypt the stub provider
uv run alembic upgrade head
uv run python main.py > /tmp/bow-agent/backend.log 2>&1 &
DS=$(uv run python ../tools/agent/seed_org.py --sqlite-sources 1 --db-path db/agent.db \
      | python3 -c 'import json,sys;print(json.load(sys.stdin)["sqlite_sources"][0]["id"])')
STUB_SOURCE_ID=$DS STUB_LOG=/tmp/sched_growth.jsonl \
  uv run python ../tools/agent/scheduled_growth_stub_llm.py &
uv run python ../tools/agent/setup_stub_llm.py
uv run python ../tools/agent/scheduled_growth_drive.py --runs 80 --mode host  --source-id $DS --log /tmp/sched_growth.jsonl --out host.json
uv run python ../tools/agent/scheduled_growth_drive.py --runs 10 --mode spawn --source-id $DS --log /tmp/sched_growth.jsonl --out spawn.json
```

DevBox: skip the env/migrate/boot/seed lines. Use the provisioned stack, and take
`DS` from `seed_org.py --sqlite-sources 1` after `source tools/agent/devbox_env.sh`.

Observed (80 host runs, 10 spawn runs, all finished; 3 planner calls per run in
both modes; tokens are the stub's chars/4 estimate):

| Run | Host: conversation tokens (peak call) | Host: est. input tokens, all calls | Spawn: all calls |
|---:|---:|---:|---:|
| 1 | 3,764 | 233,749 | 233,762 |
| 10 | 15,313 | 277,991 | 233,832 |
| 20 | 27,478 *(peak)* | 315,871 | — |
| 21 | 15,707 *(after fold)* | 300,899 | — |
| 40 | 24,281 | 310,528 | — |
| 56 | 29,305 *(peak)* | 326,624 | — |
| 57 | 16,289 *(after fold)* | 286,446 | — |
| 80 | 29,463 *(peak)* | 327,571 | — |
| mean of last 10 | | 310,521 (**+33%** vs run 1) | 233,824 (flat) |

Sawtooth peaks: 27.5K → 28.9K → 29.1K → 29.3K → 29.5K → 29.5K (levelled off).
Troughs: 15.7K → 16.3K. Fixed baseline per planner call: 71.4K in both modes.

Estimate accuracy (o200k tokenizer on a real request): chars/4 overstates the
system prompt by ~10% and understates JSON-heavy conversation by ~25%. Tool
schemas: 54.0K estimated vs 55.0K actual. The shape holds; the absolute numbers
are ±25%.

## Loop A2 — the dashboard accumulates (reproduces "more expensive every run")

Loop A rebuilds the artifact each run with only today's 2 visualizations: the
clean path. A real agent is as likely to **edit** it, and `edit_artifact` keeps
the existing visualizations automatically ("NEW visualization ids to add …
existing ones are kept automatically", `backend/app/ai/tools/schemas/edit_artifact.py:31`).
The scheduled-run instruction "If artifact existed, edit or recreate it" doesn't
say to remove the previous run's charts. `<current_artifact>` then lists every
visualization with no cap (`backend/app/ai/agents/planner/prompt_builder.py:610-624`)
and inlines the code up to 30K chars (`prompt_builder.py:628-638`).

```bash
STUB_ARTIFACT_MODE=edit STUB_SOURCE_ID=$DS STUB_LOG=/tmp/sched_edit.jsonl \
  uv run python ../tools/agent/scheduled_growth_stub_llm.py &
uv run python ../tools/agent/scheduled_growth_drive.py --runs 80 --mode host --source-id $DS --log /tmp/sched_edit.jsonl --out edit.json
```

Observed (80 runs, all finished, 3 planner calls each). By run 3,
`<current_artifact>` already lists 6 visualizations.

| | Rebuild (Loop A) | Edit/append (Loop A2) |
|---|---|---|
| Trough after each fold, runs 21→69 | 15.7K → 16.3K (levels off) | 16.2K → 17.9K → 19.4K → 20.9K → 22.3K (**keeps rising**) |
| Peak, runs 20→80 | 27.5K → 29.5K | 25.7K → 28.2K → 29.9K → 31.4K → 32.9K → 34.3K |
| Mean est. tokens per run, last 10 | 310,521 | 326,704, still rising |

Compaction folds the conversation but not the artifact, so the floor rises
~1.5K every 12 runs. The stub's panels are tiny (~130 tokens/run). Real chart
sections are 1–2K chars each, so a real dashboard would grow about 5–10× faster
until the 30K-char code cap. After the cap, the code is omitted and each edit
needs extra `read_artifact` calls, while the visualization list keeps growing.

**Revised root cause:** in host-report mode, conversation history is bounded,
but **the artifact the run keeps adding to is not**, and it is re-sent on every
planner call.

## Loop C — custom API task, before/after the replay-digest fix

The reporter's task pulls a **custom API request** (routed through
`execute_mcp`) and rebuilds the dashboard. Its dashboard holds 2 charts, so
Loop A2 doesn't apply: `edit_artifact` gates on every payload viz being
rendered (`backend/app/ai/tools/implementations/edit_artifact.py:390` →
`viz_reference_errors`), so a dashboard can't silently keep stale charts.

```bash
FAKE_API_ROWS=1500 python3 ../tools/agent/scheduled_growth_fake_api.py &   # ~175K-char JSON
uv run python ../tools/agent/scheduled_growth_api_setup.py                 # prints data_source_id + tool
STUB_API_TOOL=<tool> STUB_SOURCE_ID=<data_source_id> STUB_LOG=/tmp/sched_api.jsonl \
  uv run python ../tools/agent/scheduled_growth_stub_llm.py &
uv run python ../tools/agent/scheduled_growth_drive.py --runs 17 --mode host --source-id <data_source_id> --log /tmp/sched_api.jsonl
```

Correction to an earlier hypothesis: earlier runs' API responses are **not**
replayed in full. Inside its own run, the response is inline at ~100K chars
(~25K tokens). On later runs it comes back as the stored projection, ~3.5K chars
(capped by `GENERIC_TOOL_CONTEXT_BUDGET_BYTES` = 16KB, `backend/app/ai/persisted_summary.py:31`).

The fix (`build_durable_transcript(digest_prior_results=True)` for scheduled
runs) replays those as summary + ids. The stub is deterministic, so run 1 is
identical in both sets.

| Run 17 | Before (original code) | After (fix) |
|---|---|---|
| History replayed at run start | 16,328 tokens (already folded once at run 15, from 17.2K) | **7,728** (no fold needed yet) |
| Growth per run | +1,159 | **+351** (3.3× flatter) |
| Est. input tokens, whole run | 199,450 | **188,327 (−5.6%)** |

The absolute saving is modest because most of each run is fixed: ~73K
tokens of system prompt + tool schemas on each of 2 planner calls, plus the
run's own ~25K-token API response. **Trimming the scheduled-run tool catalog
remains the bigger lever** (see below).

## Loop B — live confirmation (not run)

The stub fixes the run at 3 planner calls and uses the deterministic fallback
summary (`method=fallback`, `degraded=1`, because the stub doesn't return
summarizer JSON). Re-run Loop A with a real model (`setup_haiku_llm.py`,
key via env var only) to measure (a) the LLM-written summary size (up to
`SUMMARY_MAX` = 12K vs the fallback's 1.6K) and (b) whether the planner's
iteration count rises as history accumulates.

## Open questions — what explains "every day" in production

Ranked by likelihood. Each can be settled from the customer's
`llm_usage_records` (group by `agent_execution_id`, ordered by date: number
of calls per run and `prompt_tokens` per call).

1. **More planner iterations per run over time.** The built-in scheduled-run
   instructions say *"If comparing to previous runs, use read_query to
   retrieve past results"*. With more history there's more to look up. Cost then
   grows with *calls per run*, not prompt size. The stub can't show this
   (fixed 3 calls). → Check: calls per run over time.
2. **Heavier real turns + the LLM summary.** Real data previews, artifact code
   and a 12K summary raise the sawtooth band, but it still levels off. → Check:
   `prompt_tokens` per call levelling off after ~2 weeks.
3. **Large-window model.** The token thresholds scale with the window. The row
   window should still bound history, but verify. → Check the model in use.

## What this means for the proposed fix ("remember last N runs" toggle)

- **First, stop the dashboard accumulating** (Loop A2, the only unbounded growth
  found). Make the scheduled-run instruction explicit: replace the previous
  run's visualizations (`remove_visualization_ids`) rather than append. Also cap
  `<current_artifact><visualizations>` in the prompt (show the newest N plus a
  count, like the 30K code cap). An "N runs" window doesn't fix this, because
  the artifact isn't history.
- On this workload it would cut history from ~16–29K to ~4–6K tokens per call:
  **~10–25% of per-run cost** in host mode (11–24K saved out of a
  ~87–101K planner call). Worth doing, but it's not the main lever.
- **Trimming the tool catalog for scheduled runs** (no eval/instruction/
  training/list-authoring tools in an unattended run) could cut up to ~40K of the
  55K on every call, in both modes, for every scheduled task: **~40–50% of
  per-run cost**. Prompt caching softens this where the provider caches the
  prefix. Confirm the customer's provider is caching before relying on that.
- If item 1 above is confirmed, the fix is mostly in the scheduled-run
  instructions (bounded look-back), and the N-runs window helps there too.

## Regression test to add with whichever fix lands

The invariant: *planner input for a scheduled run in host mode is bounded
independent of the number of past runs.* Seed K completions under one
`scheduled_prompt_id`, build the planner input for run K+1, and assert
the history size for K=50 is ≤ that for K=5 plus a constant. If the
catalog is trimmed, also assert the scheduled-run tool set excludes
authoring tools. Follow `backend/tests/AGENTS.md`.
