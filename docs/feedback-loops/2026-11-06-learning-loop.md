# Feedback Loop — "learn from a target over every item, then write instructions"

A trainer gives an agent a target Y and a set of items: labels for 300
transactions, which of 40 support tickets are urgent. The agent starts with zero
instructions. The claim: in training mode it runs a read → check → learn →
repeat loop over **all** items and writes instructions that let it reproduce Y
by itself. A second claim: during long runs the planner sees its own token use,
cache share and repeated identical calls.

## What was built

- **Skill `learning-loop`** ("Learn rules from labelled examples",
  `backend/app/ai/skills/library/learning-loop.md`). It is on by default in
  training mode and listed right after "Train an agent". It sets out one loop
  and leaves the details to the agent:
  - **Score every pass.** Encode the current rules as the prediction, compare
    it with Y over all items, and count the matches.
  - **Keep a `Plan` note** with the loop's steps as checkboxes, so the existing
    completion gate keeps the run going when the model tries to stop early
    (`app/ai/run_control.py`).
  - **Write rules that decide.** A cutoff is part of the rule. Observed rates
    go in `evidence`.
  - **Explain misses with derived fields** (hour, length, a header field), not
    with exact wording.
  - **Write without asking.** The draft build is the approval step.
  - **Fan out only where items must be read** (files). Sub-agents report; only
    the main agent writes instructions.
  - **Stop only when there is no next check left to run.**
- **Anti-overfit wording** (`prompt_blocks.NO_OVERFIT_BLOCK` and the generality
  check prompt in `instruction_quality.py`): a decision cutoff learned from the
  user's labelled examples is part of a rule, while observed rates and counts
  still are not. Before this, the agent left cutoffs out ("raise the risk when
  the amount is high"), and such a rule cannot be applied to a single item.
- **Training prompt** (`prompt_builder_v3.py`):
  - When the user supplies the correct answers to learn from, read the
    matching skill before reading the items.
  - The proactive "delegate one sub-agent per cause" reminder no longer rides
    in training turns. It turned rule checks over one table into fan-outs, and
    the research-only children came back `blocked`.
- **`<run_efficiency>` line** (`app/ai/agents/planner/run_efficiency.py`, wired
  in `agent_v2.py`):
  - Shows input tokens so far, the share served from cache, output tokens, and
    identical repeated tool calls (same tool and arguments; polling tools are
    exempt).
  - Lives in the per-turn head (`_build_turn_head`), below the cached prefix.
  - Appears after 4 steps, after 200k input tokens, or at the first repeat.
  - Anthropic counts input without the cached part, so the totals are adjusted
    to mean the same as other providers.
  - Off unless `BOW_RUN_EFFICIENCY=1`.

## Loop A — deterministic (no LLM)

```bash
cd backend && export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest tests/unit/test_run_efficiency.py tests/unit/test_sub_agent_prompts.py \
  tests/unit/test_no_overfit_prompt_block.py tests/unit/test_skill_catalog_library.py -q
uv run pytest tests/e2e/test_skill_catalog.py tests/e2e/test_skills_catalog.py \
  tests/e2e/test_instruction_overfit.py -m e2e --db=sqlite -q
```

The tests check that:
- the line is silent on short runs and shows on long or repeating ones;
- only identical calls count, and argument order does not matter;
- the cache share is the same on Anthropic and OpenAI for the same request;
- the line rides in the newest user turn and leaves the system prompt and the
  cached first turn byte-identical (verified failing with the render line
  removed);
- training turns get no delegation reminder;
- the training prompt points labelled learning at the skills;
- the skill parses and sits in the curated default list.

## Loop B — live (Haiku 5.5, real stack, mock data)

The backend runs alone with `BOW_RUN_EFFICIENCY=1`, `BOW_LLM_TRACE_FILE` and
`BOW_LLM_TRACE_FULL=1`, seeded with `tools/agent/seed_org.py`. The key comes
from the `ANTHROPIC_KEY` env var only. Each run starts from a fresh database, so
the org has zero instructions.

```bash
cd backend
ANTHROPIC_KEY=... uv run python ../docs/feedback-loops/2026-11-06-learning-loop.live.py        # rows
ANTHROPIC_KEY=... uv run python ../docs/feedback-loops/2026-11-06-learning-loop-files.live.py  # files
```

- **Rows** (`…learning-loop.live.py`):
  - 300 card transactions with a `fraud_labels` table. The hidden rules are
    "cross-border and amount ≥ 800" and "gift cards between 00:00 and 05:00",
    plus 4 noisy labels and a decoy `channel` column.
  - The script scores 120 unseen transactions twice before learning and twice
    after the run's build is published.
- **Files** (`…learning-loop-files.live.py`):
  - 40 ticket `.txt` files plus `labels.csv` in the agent's file library. The
    hidden rule is "data loss, or an Enterprise-plan outage blocking several
    users". Decoys: angry tone, single-user issues, outages on lower plans.
  - The script scores 15 unseen tickets before and after.

### What each round exposed, and the fix

| Round | Observed (Haiku 5.5) | Fix |
|---|---|---|
| 1 | Test script stopped while sub-agents were still running | Script waits for open sub-agent runs |
| 2 | Rules left out their cutoffs ("raise the risk") because the anti-overfit text treated a learned cutoff as an observed number | Learned decision cutoffs are part of a rule (prompt and generality check) |
| 3 | Profiled each factor, never scored a rule set, then asked "should I write these?" | Skill: score the rule set as one prediction query; write without asking (the draft build is the review) |
| 3 | Sub-agent came back `blocked`: children cannot run `create_data` by default | Skill: score rows yourself; no delegation reminder in training turns |
| 4–5 | Stopped with a named next step ("check whether the false positives share a field") | Skill: if you can name the next check, run it; check both kinds of miss; move the cutoff |
| 5 | Never tried the hour inside the timestamp | Skill: try derived fields on the misses |
| files 6b–8b | Read files, then ended the turn with "I'll score next"; in some runs never opened the skill | `Plan` note as the first action (the completion gate then forces continuation); training prompt points to the skills; skill renamed to what users say |
| files 10b | Tried to read all tickets with one `grep_files`, but it printed 162 of 280 lines | Skill: `grep_files` finds, `read_file` in batches reads |
| files 11b–12b | Ran the whole loop but keyed the rule on wording, not the `Plan:` header | Skill: when similar items have different Y, look at the other fields |

### Results on unseen items (2 samples each side)

| Run | Before learning (P / R) | After learning (P / R) | Instructions written |
|---|---|---|---|
| rows, round 2 | 0.47 / 0.43 | 1.00 / 0.67 | 3, vague (no cutoffs) |
| rows, round 4 | 0.68 / 0.62 · 0.78 / 0.33 | 1.00 / 0.67 · 1.00 / 0.67 | 3: one decides, two vague |
| rows, round 6 | 0.50 / 0.24 · 0.59 / 0.62 | 0.83 / 0.71 · 0.83 / 0.71 | 2, both decide |
| rows, round 7 | 0.56 / 0.71 · 0.35 / 0.86 | 0.57 / 0.76 · 0.62 / 0.76 | 1 combined (gift-card clause too broad) + 1 exclusion |
| rows, round 13 (final skill) | 1.00 / 0.67 · 0.43 / 0.86 | 0.93 / 0.67 · 0.93 / 0.67 | 1 combined rule, both clauses decide |
| files, round 11 | 0.75 / 1.00 · 0.75 / 1.00 | 0.71 / 0.83 · 0.71 / 0.83 | 1, decides (keyed on wording) |
| files, round 12 | 0.75 / 1.00 · 0.75 / 1.00 | 0.67 / 0.67 · 0.71 / 0.83 | 1, decides (keyed on wording) |

Round 6 is the best rows run, a full loop:
- six scored passes;
- a `Plan` ledger;
- misses pulled and compared, cutoffs tuned;
- training-set recall from 0.56 to 0.78 at 0.76 precision;
- two instructions that a reader can apply to one row.

`<run_efficiency>` appeared live, for example:

```
<run_efficiency>step 7 · input so far 739,467 tokens (90% from cache) · output 3,494. Diagnostic only: …
```

It appeared in 4 to 21 planner requests per run, and the agent never repeated
the numbers to the user. No live run made an identical repeated call, so the
repeat warning is covered by unit tests only.

## What this proves / limits

- The loop runs end to end from zero instructions, over every item, in both
  shapes (rows and files), and writes rules that decide.
- On rows, learning raised precision on unseen data over the uninstructed agent
  in every completed run.
- Haiku 5.5 does not reliably find the subtler pattern in either test (the hour
  inside a timestamp; the plan field behind same-wording tickets). In the
  ticket test the uninstructed model was already strong (recall 1.0), and the
  wording-keyed rule made it slightly worse on unseen tickets. A check on items
  held out from learning would catch that before the build is submitted. That
  was deliberately left out of the skill to keep it simple.
- Run-to-run variance on Haiku is high (see the baselines). Single runs are
  anecdotes.
- `MAX_COMPLETION_REJECTIONS = 2` bounds how often the `Plan` gate can push the
  run on. Long file reads can exhaust it.
- Pre-existing, unrelated: about 199 unit tests fail in this sandbox on the
  proxy CA bundle and on the missing `libodbc` library. The same set fails with
  the change stashed.
