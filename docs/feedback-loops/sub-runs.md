# Feedback Loop — sub-runs (delegated agent runs with wave fan-in)

**Status: plan. Loops below are written to be executed as each phase of
`docs/design/sub-runs.md` lands; observed output sections are empty until
then.** Design decisions are not repeated here; this doc is the runnable
proof that they hold, on one root-cause-analysis (RCA) use case.

Claim being validated: when a user asks *why* a metric moved, the agent
launches one sub-run per candidate cause in a single response, ends its
turn, each sub-run investigates on its own and finishes with a typed record,
one fan-in turn wakes the agent with all records, and the agent names the
real cause with cited evidence, rules out the decoys, and never fabricates a
sub-run result. Tokens, steps and cost per sub-run are visible on the card
and the summary tab, and everything survives a reload.

---

## The RCA use case and its dataset

A planted incident, generated deterministically so every loop sees the same
numbers. Script `tools/agent/seed_rca_incident.py` writes
`backend/demo-datasources/rca_incident.sqlite` (committed, like
`chinook.sqlite`) and registers it as the **"Checkout Ops"** demo source
with one agent; the eval YAML references it by slug.

Tables (daily grain, 2026-08-01 → 2026-09-30, seeded RNG):

| Table | Columns | Role |
|---|---|---|
| `orders` | date, region, platform, app_version, payment_provider, status (paid/failed), amount | the metric: paid orders and checkout failure rate |
| `sessions` | date, region, platform, app_version, sessions | denominator for conversion; carries the traffic decoy |
| `deploys` | date, service, version, notes | the real cause's timeline |
| `payment_provider_status` | provider, region, date, incident (bool) | a decoy that looks causal |
| `price_changes` | date, sku_group, old_price, new_price | a decoy with no effect |
| `campaigns` | start, end, region, channel, spend | a decoy that moves sessions, not conversion |

Planted truth: from **2026-09-14** checkout failures jump from ~3 % to ~18 %
**only** for `platform = ios` **and** `app_version = 5.2.0` **and**
`payment_provider = PayFlow`; `deploys` shows `checkout-service 5.2.0`
shipped 2026-09-14 06:00 UTC. Decoys: a price change on 09-10 (no effect on
either series), an APAC campaign 09-12 → 09-20 (sessions up 40 %, conversion
flat), a `PayFlow` incident flag on 09-15 in `us-east` only (fails there for
six hours, recovers), and the ordinary weekly seasonality. Five candidate
causes, one true, each separable by one or two queries. That is what makes
the case a sub-run case: the hypotheses are independent and each needs
several steps.

Prompt used in every loop:

> Paid orders dropped about 15 % since mid-September. Why? Test the likely
> causes separately and tell me which one it is, with evidence, and which
> ones you ruled out.

---

## Harness changes the loops depend on

Two gaps in `TestRunService` / `TestEvaluationService`, both small, both
needed before any eval below can pass:

1. **Wave-aware terminal state.** A run is not finished while a
   `sub_run_waves` row for the report is `running` or `firing`, nor until the
   fan-in machine turn's system completion is terminal. Today the finalizer
   treats the first system completion ending as the end of the case.
2. **Children in the snapshot.** `build_final_snapshot` includes tool
   executions from child executions (via `parent_agent_execution_id`) tagged
   with `phase = subagent` and `sub_run_id`, so `tool.calls` rules can count
   `finish_subtask` and a `phase` rule can scope assertions to children or to
   the fan-in turn. The parent's final text for `text.*` rules is the fan-in
   turn's reply.

---

## Loop A — deterministic (stub LLM, no external services)

Proves the mechanics independent of any model: wave creation, child
bootstrap, `finish_subtask` record, write-back, fan-in machine turn,
`read_sub_run` while running, digest, settings gating, agent and model
override validation, reload hydration. Extends `tools/agent/stub_llm.py`
with a scripted `delegate` phase (`STUB_PHASES=delegate`):

- parent round 1 (no observations in prompt, chat mode): N `delegate` calls,
  one per hypothesis in `STUB_HYPOTHESES`, then text "Testing N causes."
- child rounds (system prompt carries the `subagent` marker): `inspect_data`
  → `create_data` → `finish_subtask` with a fixed record per hypothesis
  (`supported` for the one whose goal mentions "5.2.0", `refuted` otherwise);
  one hypothesis ("seasonality") deliberately never calls `finish_subtask`,
  to exercise the forced-finish turn and the synthesized `partial` record.
- fan-in round (prompt carries `<sub_run_results>`): text answer naming the
  supported hypothesis.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python ../tools/agent/seed_rca_incident.py --out demo-datasources/rca_incident.sqlite
uv run pytest tests/e2e/test_sub_runs.py -v
```

`tests/e2e/test_sub_runs.py` (to be written with phase 1–2; assert the
invariant, not the scenario — `backend/tests/AGENTS.md`):

| Test | Invariant |
|---|---|
| `test_delegate_batch_forms_one_wave` | N `delegate` calls in one decision → one `sub_run_waves` row with `remaining = N`; each receipt carries `sub_run_id`, `wave_id`, resolved `agents`, `model` |
| `test_child_is_hidden_completion_with_parent_links` | child completions have `trigger_source = sub_run`, are absent from `get_completions_v2`, and the child `AgentExecution` has both parent ids |
| `test_child_mode_switches_off_parent_only_behaviour` | child catalog ⊆ profile allow-list; no `clarify`, no artifact tools, no `delegate`; no title/follow-up/harness/check-in rows for the child |
| `test_finish_subtask_writes_record_and_ends_run` | record row + envelope `exit_reason = finish_call`; child completion terminal right after the call |
| `test_missing_finish_gets_forced_turn_then_partial` | the never-finishing child gets exactly one forced turn, then a synthesized `partial` record with `exit_reason = no_finish_call`; the wave still fires |
| `test_wave_fires_exactly_once_under_concurrent_finishes` | N children finishing concurrently → one `firing` transition, one fan-in machine turn (`trigger_source = sub_run`, visible `external` event + hidden trigger) |
| `test_deadline_times_out_running_children` | with `sub_run_deadline_minutes` tiny and one child stalled, the deadline job marks it `timed_out`, stops it, synthesizes its record and fires the fan-in |
| `test_write_back_to_delegate_tool_execution` | after a child finishes, the parent's `delegate` `result_json` holds receipt + record; `_digest_sub_run_tool` renders the one-line digest |
| `test_read_sub_run_while_running_and_after` | running → progress snapshot shape; finished → record; `include_report` returns the note body |
| `test_fan_in_prompt_carries_records_in_launch_order` | the hidden trigger lists all N records in the order launched, each paired with its goal |
| `test_children_do_not_block_report_queue` | a user completion created mid-wave runs (not queued) while children are `in_progress`; the fan-in turn queues behind it |
| `test_created_queries_are_report_scoped_without_widgets` | child `create_data` → query + step on the report, no widget auto-placed, `via_sub_run` provenance resolvable |
| `test_agent_ids_default_and_override` | no `agent_ids` → the parent's attached agents; an id the user cannot use → refused receipt; a narrowed set → child roster narrowed |
| `test_model_override_validated` | allowed `<provider>/<model>` → child runs on it and the envelope says so; disallowed → refused receipt; `sub_run_allow_model_override = off` → ignored with a note in the receipt |
| `test_settings_gate_and_caps` | `enable_sub_runs = off` hides `delegate` and the DELEGATION block; `sub_run_max_per_wave` refuses the excess; `sub_run_concurrency` queues children inside the wave |
| `test_stop_parent_stops_children` | stopping the report run marks running children `interrupted`; the wave counts them terminal |
| `test_summary_and_reload_endpoints` | `GET /reports/{id}/summary` has `sub_runs`; `GET /reports/{id}/sub-runs/{id}` returns row + envelope + child tool executions |

Observed (fill in per phase):

```
phase 1: …
phase 2: …
```

Regression leg, unchanged suites that touch the same seams:

```bash
uv run pytest tests/unit/test_prompt_builder_v3_user_profile.py tests/unit/test_tool_runner_*.py \
  tests/e2e/test_agent_notes.py tests/e2e/test_eval.py -q
```

---

## Loop B — live (real LLM; keys via env only)

Runs the RCA case through the in-product eval harness with a real model
matrix (`EVAL_LLMS`, same selection as `tests/evals/test_evals.py`).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
export ANTHROPIC_API_KEY_TEST=…   # or OPENAI_API_KEY_TEST; never echoed
EVAL_LLMS=anthropic uv run pytest tests/evals/test_evals.py -k sub_runs_rca -m evals -v
```

Suite `backend/tests/evals/suites/sanity_sub_runs_rca.yaml` (lands with
phase 2; draft):

```yaml
name: Sanity · Sub-runs RCA
description: >
  A "why did it drop" ask on the planted Checkout Ops incident must fan out
  to one sub-run per candidate cause in a single response, end the turn,
  and conclude in the fan-in turn with the planted cause, cited evidence,
  and the decoys ruled out. Pins the DELEGATION posture, the finish_subtask
  contract and the wave fan-in.

tags: [sub_runs, rca]

data_source_slugs:
  - Checkout Ops

fixtures:
  org_settings:
    enable_sub_runs: true
    sub_run_posture: proactive
    sub_run_allow_create_data: true

cases:
  - name: rca_fans_out_and_concludes
    prompt:
      content: >
        Paid orders dropped about 15% since mid-September. Why? Test the
        likely causes separately and tell me which one it is, with evidence,
        and which ones you ruled out.
    expectations:
      spec_version: 1
      order_mode: flexible
      rules:
        # fan-out: at least three hypotheses, all in one decision
        - type: tool.calls
          tool: delegate
          min_calls: 3
          max_calls: 6
          same_decision: true          # new flag, see harness change 2
        # the parent ends its turn after launching: no analysis tools in turn 1
        - type: phase
          phase: parent_turn_1
          rules:
            - { type: tool.calls, tool: create_data, max_calls: 0 }
            - { type: tool.calls, tool: inspect_data, max_calls: 0 }
        # every child finishes through the contract
        - type: phase
          phase: subagent
          rules:
            - { type: tool.calls, tool: finish_subtask, min_calls: 3 }
            - { type: tool.calls, tool: clarify, max_calls: 0 }
            - { type: tool.calls, tool: create_artifact, max_calls: 0 }
        # the fan-in turn exists and concludes
        - type: phase
          phase: fan_in
          rules:
            - type: field
              category: sub_runs
              field: outcomes
              matcher: list.contains_any
              value: [supported]
            - type: field
              category: final_answer
              field: text
              matcher: text.regex
              value: "(?i)5\\.2\\.0|checkout-service"
            - type: field
              category: final_answer
              field: text
              matcher: text.regex
              value: "(?i)ruled out|not the cause|refuted"
            - type: field
              category: final_answer
              field: text
              matcher: text.not_contains
              value: "sr_"            # no raw sub-run ids in user-facing text
        - type: judge
          rubric: >
            The answer names the iOS 5.2.0 checkout release on PayFlow as the
            cause, cites at least one query or chart per claim, explicitly
            rules out the price change, the APAC campaign and the us-east
            PayFlow blip as not explaining the drop, and does not present any
            number that no sub-run produced. Score 1-5; pass ≥ 4.

  - name: simple_ask_does_not_delegate
    prompt:
      content: How many paid orders did we have in September?
    expectations:
      spec_version: 1
      rules:
        - { type: tool.calls, tool: delegate, max_calls: 0 }
        - { type: tool.calls, tool: create_data, min_calls: 1 }

  - name: explicit_posture_waits_to_be_asked
    fixtures:
      org_settings:
        sub_run_posture: explicit
    prompt:
      content: >
        Paid orders dropped about 15% since mid-September. Why?
    expectations:
      spec_version: 1
      rules:
        - { type: tool.calls, tool: delegate, max_calls: 0 }

  - name: follow_up_reads_record_on_demand
    turns:
      - prompt:
          content: >
            Paid orders dropped about 15% since mid-September. Why? Test the
            likely causes separately.
      - prompt:
          content: What exactly did the pricing investigation check?
    expectations:
      spec_version: 1
      rules:
        - type: phase
          phase: turn_2
          rules:
            - { type: tool.calls, tool: read_sub_run, min_calls: 1 }
            - { type: tool.calls, tool: delegate, max_calls: 0 }
```

What each case pins: the proactive posture fires on a *why* and not on a
*how many*; the explicit posture stays quiet; the parent does not do the
investigation itself in turn 1; every child honours the contract and never
reaches for parent-only tools; the fan-in turn concludes with the planted
cause and the decoys; a later turn reaches for the record instead of
re-running the work.

Observed (fill in per model):

```
anthropic default: …
openai default: …
```

---

## UI evidence (ui-evidence skill; mandatory for phase 3)

Captured with `tools/agent/capture.mjs` against `tools/agent/boot_stack.sh`
+ `seed_org.py --demo` + the Checkout Ops source, into
`media/pr/sub-runs/`:

1. Turn 1: five delegate cards in one block, each with its own spinner and
   step counter, wave strip "Wave 1 · 5 sub-runs · 5 running", the parent's
   one-line message, composer free.
2. Mid-wave: a card expanded showing live step rows; a `read_sub_run` card
   from a mid-wave user question.
3. Fan-in: the `sub_run` event strip with outcome counts, then the synthesis
   reply with embedded evidence charts.
4. Summary tab: "Sub-runs" section with outcome badges, steps, tokens;
   header strip; "via sub-run" tags in Queries.
5. Trace modal: delegate row expanded to the child execution.
6. Reload of the same report: identical cards hydrated from rows (before/after
   pair).
7. Cost console: parent run with own + children + total.

Video: the full turn from prompt to fan-in reply, for the PR.

---

## Phase exit criteria

| Phase | Done when |
|---|---|
| 1 `subagent` mode, `finish_subtask`, tables, profile | Loop A tests 3–5, 8–9 pass with a hand-built child run (no `delegate`); seed script committed |
| 2 `delegate`, wave tracker, fan-in, `read_sub_run`, settings | all Loop A tests pass; harness changes 1–2 landed; Loop B `rca_fans_out_and_concludes` passes on the default model |
| 3 live cards, wave strip, summary, trace, rollup | UI evidence 1–7 captured; Loop B passes on two providers |
| 4 `create_data` in children | evidence viz ids appear in the fan-in reply's `create_doc`; `test_created_queries_are_report_scoped_without_widgets` holds with widgets off |
| 5 profiles via catalog, resume, steering | separate loop doc |

## What this will prove / notes

- Wave mechanics are provider-independent (Loop A) and the posture and
  contract hold on real models (Loop B).
- The planted dataset makes "fabricated result" detectable: any number in
  the answer that no child produced is a judge fail, and the decoys give the
  judge something concrete to check for in the ruled-out list.
- Pre-existing quirks to verify-before-blaming when they show up: the eval
  finalizer's reliance on the first system completion (harness change 1) will
  make every sub-run case look like a pass-with-no-answer until it lands.
