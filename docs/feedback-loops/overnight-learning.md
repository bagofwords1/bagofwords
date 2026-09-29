# Feedback Loop — Overnight learning: agents consolidate what they were taught; users' memory and follow-ups kept current

Two nightly jobs (lab). The agent dream has its own switch, `enable_agent_dreaming` (on by default); the user dream runs whenever user memory is on:

- **Agent dream** (per agent, 03:00–05:00 org-local). Pending AI instruction
  suggestions pile up: the same correction arrives from several people as
  separate drafts, and one-offs sit forever. At night each agent looks at its
  pending suggestions once, groups the ones that say the same thing, and —
  only when **≥3 distinct users on ≥2 distinct days** said it — stages **one**
  consolidated suggestion, hands it to the agent's Self-Learning policy, and
  closes the absorbed drafts as *merged*. Unreviewed AI suggestions older than
  the org's `ai_suggestion_expiry_days` (default 30) are closed as *expired*.
  Neither counts as a reviewer's rejection.
- **User dream** (per user, 01:00–03:00). Reads only that user's own human
  turns since last night and does two things, both through systems that
  already exist: keeps their **memory** current (through the normal memory
  rules) and plans up to two **check-ins** (through the normal check-in
  limits, working window and opt-out). It creates no new user-facing objects:
  results show up where memory and check-ins already do.

Design: `docs/design/overnight-learning.md` (§0 lists where the build differs
from the plan). Evidence: `media/pr/claude-overnight-learning/`.

## What was built

| Layer | Where |
|---|---|
| Settings | `enable_agent_dreaming`, `ai_suggestion_expiry_days` in `app/schemas/organization_settings_schema.py`; per-agent `nightly_learning` in `app/schemas/agent_automation_schema.py`. The user dream has no switch of its own: it runs when `enable_user_memory` is on, and its check-ins follow `enable_agent_checkins` and each user's check-in opt-out |
| Storage | `dream_runs`; watermarks `memberships.user_dreamed_at`, `data_sources.agent_dreamed_at`; `agent_checkins.origin/dream_run_id` — `alembic/versions/dream01_overnight_learning.py` (+ `mrgckmem01` merging the check-ins and memory heads) |
| Runtime | `app/services/dreams/runtime.py` — hourly leader-only `overnight_sweep` (`main.py`), org-local windows, once per unit per night (`dream_runs` + `claim_scheduled_run`), 2M-token nightly org budget, watermark only on done/skipped, switches re-checked before any write |
| Agent dream | `app/services/dreams/agent_dream.py`, prompt/parse `app/ai/agents/dreams/agent_prompts.py` |
| User dream | `app/services/dreams/user_dream.py`, prompt/parse `app/ai/agents/dreams/user_prompts.py`; follow-ups via `CheckinService.plan_from_dream` |
| Scoping fix | `AgentReliabilityService._resolve_suggestion_agents` — only changed rows decide which agents a suggestion affects |
| UI | "Learn overnight" (`AgentAutomationSettings.vue`), Merged/Expired badges (`instructions/BuildExplorerModal.vue`), moon icon for memory noted overnight (`profile/UserMemoryPanel.vue`) and for the nightly inbox notice, all 10 `locales/*.json`. No new user-side UI |
| Debug trigger | `tools/agent/run_dream.py --kind agent|user --target <id> [--force]` or `--sweep` |

## Loop A — deterministic (stubbed model, fixed or real clock)

The model boundary is the one structured call per dream; tests inject a
scripted proposal built from the prompt the dream really produced (so every
key it cites — `d3`, `r1`, `m2` — is one the prompt contained). Services,
builds, the Self-Learning hand-off, memory rules, check-in limits and the DB
run for real.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_overnight_common.py tests/e2e/test_agent_dream.py tests/e2e/test_user_dream.py -q
```

Observed on the final commit: **27 + 17 + 13 passed** on SQLite, and the same
on Postgres 16 (`--db=external` against a local server, no Docker:
`TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bow_test`).

What they pin down (by contract, not by incident):

| Area | Tests |
|---|---|
| Gates | 3 users × 2 days promotes one scoped build; 2 users → no model call; a group the model proposes below the gate is **held**; expired drafts still count |
| Backlog | absorbed drafts → `[merged]` (no per-hunk verdicts, so not a human rejection); stale AI drafts → `[expired]` per org setting (`0` = never); human drafts never expire; partly-absorbed builds stay open |
| Scope | only unused **AI** instructions can be archived; feedback edits only on instructions with ≥2 downvotes; a suggestion with several rows on one agent affects only that agent |
| Runtime | window + once per night; failure leaves the watermark, a retry succeeds; org budget stops further units; switches (org, per-agent) gate before and during the model call |
| User dream inputs | only the user's own **human** turns (scheduled/check-in/other users' turns never reach the prompt); nothing new → skipped, no model call; an upcoming memory event wakes it without new turns; watermark moves |
| User dream outputs | writes only memory and planned check-ins; memory created with `source=dream`; entries the **user** wrote are never edited/forgotten; rules and secrets refused by the memory rules; check-ins only with check-ins on and the user not opted out, only on the user's own reports and known keys, at most 2 a night; needs `enable_user_memory` too; switching off mid-run writes nothing |

Spot-checked by mutation — each of these tests fails with its guarded line
removed: the human-turn filter, the user-authored refusal, the 2-a-night
limit, the user-memory requirement.

Regression: check-ins, memory, reliability and instruction-sharing suites —
**336 passed**; `alembic heads` → `dream01` only; migration up/down/up
round-trips on SQLite and Postgres; locale key sets unchanged against `main` apart from the
new keys (pre-existing drift in non-`en` catalogs is untouched).

## Loop B — live stack (real UI, real services; model scripted)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo --invite dana@example.com --invite sam@example.com
```

Seeded through the API (reports, org settings) plus back-dated history the API
can't produce: Dana's human turns (a board meeting in two days), and four AI
suggestions on the demo agent — the same VAT correction from Dana, Sam and the
admin on three days, plus an audit one-off. Both dreams then ran through the
real `DreamRuntime` with a scripted model reply.

Observed:

- **Agent dream** `done`: one nightly suggestion "Revenue is net of VAT" with
  provenance *"3 users over 3 days (3 suggestions, builds #10, #11, #12)"*;
  builds #10–#12 closed as merged; the audit one-off left pending; one inbox
  notice to the agent's manager. The pending list went from 4 to 2.
- **User dream** `done`, prompt with five sections (sessions, memory, upcoming,
  recent follow-ups, scheduled tasks). It wrote a dated memory entry
  (`source=dream`, shown in the Memory tab with the moon icon) and a planned
  check-in (`origin=dream`, due the next working morning). On a second run
  the same night, the next check-in was **rejected by the ordinary check-in
  limit** (`pending_exists_user`): the dream gets no special allowance.

Found and fixed during this loop:

1. The manager notice read "…merged Waiting for your review." and linked to a
   route that ignores its query → one sentence, links to the agent page (+ test).
2. `seed_org.py --invite` sent a payload the members endpoint rejects (422) →
   fixed.
3. After a backend code edit the dev auto-reloader hung and the backend stopped
   answering (the sweep was not running; it only works 01:00–05:00) → restarted
   the backend. Worth knowing for long agent sessions.

An earlier iteration of the user dream also kept "open threads", habit offers
and a home-page "Since you were here" card. They were removed to keep the
feature to what already exists (memory + check-ins); see the design doc §0.

### Loop B with a real model — pending

The live run on OpenAI `gpt-6-luna` (the org's small default) did not happen
in this session: the key was not available to the sandbox as an environment
variable. To run it:

```bash
# key in the environment only — never in files, commits or logs
export OPENAI_API_KEY=...            # set in the cloud environment's settings
# add the provider in Settings → Models with gpt-6-luna as the small default, then:
cd backend && TESTING=true TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/run_dream.py --kind agent --target <data_source_id> --force
cd backend && TESTING=true TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/run_dream.py --kind user --target <user_id> --force
```

What to check: the agent dream groups the three VAT drafts and leaves the
audit one alone (code holds anything below the gate either way); the user
dream proposes the board meeting as a dated memory (updating rather than
duplicating an existing entry), writes a self-contained check-in note, and
plans it during working hours; `dream_runs.tokens`
is filled from the usage records.

## Metrics to watch after rollout

- `dream_runs` by status/reason per night; tokens per org vs the 2M budget.
- Pending AI suggestions per agent (should fall), merged vs expired counts.
- Nightly suggestions approved vs rejected by reviewers.
- Dream-planned check-ins: sent vs ran quietly vs skipped by the judge.
