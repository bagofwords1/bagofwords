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

**Partially reproduced.** In host-report mode, the conversation part of the
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
