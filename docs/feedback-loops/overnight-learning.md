# Feedback Loop — Overnight learning: agents consolidate what they were taught, users find things ready

Two nightly jobs, both **off by default** (lab):

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
  turns since last night and proposes: memory updates (through the normal
  memory rules), open threads, up to two follow-ups (through the check-in
  limits), and at most one "have this ready every Monday?" offer for an ask
  code saw recur on ≥3 weeks. The user sees the result as a small **Since you
  were here** card on the home page.

Design: `docs/design/overnight-learning.md` (§0 lists where the build differs
from the plan). Evidence: `media/pr/claude-overnight-learning/`.

## What was built

| Layer | Where |
|---|---|
| Settings | `enable_agent_dreaming`, `enable_user_dreaming`, `ai_suggestion_expiry_days` in `app/schemas/organization_settings_schema.py`; per-agent `nightly_learning` in `app/schemas/agent_automation_schema.py`; per-user `memberships.overnight_prep` |
| Storage | `dream_runs`, `user_open_threads`, `habit_offers`; watermarks `memberships.user_dreamed_at`, `data_sources.agent_dreamed_at`; `agent_checkins.origin/dream_run_id/briefing_feedback` — `alembic/versions/dream01_overnight_learning.py` (+ `mrgckmem01` merging the check-ins and memory heads) |
| Runtime | `app/services/dreams/runtime.py` — hourly leader-only `overnight_sweep` (`main.py`), org-local windows, once per unit per night (`dream_runs` + `claim_scheduled_run`), 2M-token nightly org budget, watermark only on done/skipped, switches re-checked before any write |
| Agent dream | `app/services/dreams/agent_dream.py`, prompt/parse `app/ai/agents/dreams/agent_prompts.py` |
| User dream | `app/services/dreams/user_dream.py`, prompt/parse `app/ai/agents/dreams/user_prompts.py`; follow-ups via `CheckinService.plan_from_dream` |
| Briefing + habits + log | `app/services/dreams/briefing.py`, routes `app/routes/overnight.py` (`/api/users/me/briefing…`, `/habit_offers/{id}/accept|decline`, `/overnight`, `/overnight/log`) |
| Scoping fix | `AgentReliabilityService._resolve_suggestion_agents` — only changed rows decide which agents a suggestion affects |
| UI | `components/BriefingCard.vue` (home), overnight toggle + "What I did overnight" (`UserProfileModal.vue`), "Learn overnight" (`AgentAutomationSettings.vue`), Merged/Expired badges (`instructions/BuildExplorerModal.vue`), moon icon for dream memory / inbox, all 10 `locales/*.json` |
| Debug trigger | `tools/agent/run_dream.py --kind agent|user --target <id> [--force]` or `--sweep` |

## Loop A — deterministic (stubbed model, fixed or real clock)

The model boundary is the one structured call per dream; tests inject a
scripted proposal built from the prompt the dream really produced (so every
key it cites — `d3`, `r1`, `m2`, `h1` — is one the prompt contained). Services,
builds, the Self-Learning hand-off, memory rules, check-in limits and the DB
run for real.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_overnight_common.py tests/e2e/test_agent_dream.py tests/e2e/test_user_dream.py -q
```

Observed on the final commit: **37 + 17 + 17 passed** on SQLite, and the same
71 on Postgres 16 (`--db=external` against a local server, no Docker:
`TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bow_test`).

What they pin down (by contract, not by incident):

| Area | Tests |
|---|---|
| Gates | 3 users × 2 days promotes one scoped build; 2 users → no model call; a group the model proposes below the gate is **held**; expired drafts still count |
| Backlog | absorbed drafts → `[merged]` (no per-hunk verdicts, so not a human rejection); stale AI drafts → `[expired]` per org setting (`0` = never); human drafts never expire; partly-absorbed builds stay open |
| Scope | only unused **AI** instructions can be archived; feedback edits only on instructions with ≥2 downvotes; a suggestion with several rows on one agent affects only that agent |
| Runtime | window + once per night; failure leaves the watermark, a retry succeeds; org budget stops further units; switches (org, per-agent, per-user) gate before and during |
| User dream inputs | only the user's own **human** turns (scheduled/check-in/other users' turns never reach the prompt); nothing new → skipped, no model call; watermark moves |
| User dream outputs | memory created with `source=dream`; entries the **user** wrote are never edited/forgotten; rules and secrets refused by the memory rules; follow-ups only with check-ins on, not opted out, own report, known key; threads replaced only when the night saw sessions |
| Habits | only for a recurring ask code detected; accept creates a normal scheduled task (`30 8 * * 1`); answered offers can't be re-answered (`habit_offer.not_pending`); already-scheduled and recently-declined asks are not offered again |
| Briefing | owner-only (another member gets `404 briefing.item_not_found`, sees nothing); "not useful" dismisses; returning to a report resolves its thread; **"Got it" hides items until a later night notes them again** |

Spot-checked by mutation — each of these tests fails with its guarded line
removed: the human-turn filter, the user-authored refusal, the last-seen
filter.

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
can't produce: Dana's human turns (a board meeting in two days, margin work
waiting on a cost file, "Weekly pipeline by region" on three past Mondays), and
four AI suggestions on the demo agent — the same VAT correction from Dana, Sam
and the admin on three days, plus an audit one-off. Both dreams then ran
through the real `DreamRuntime` with a scripted model reply.

Observed:

- **Agent dream** `done`: one nightly suggestion "Revenue is net of VAT" with
  provenance *"3 users over 3 days (3 suggestions, builds #10, #11, #12)"*;
  builds #10–#12 closed as merged; the audit one-off left pending; one inbox
  notice to the agent's manager. The pending list went from 4 to 2.
- **User dream** `done`: memory `Board meeting` (dated, `source=dream`), one
  open thread, one planned follow-up before the meeting (`origin=dream`), one
  habit offer. Home shows *Since you were here*; Hebrew renders RTL.
- Clicking **Yes, schedule it** created the scheduled task `30 8 * * 1`;
  **Not useful** dismissed the thread; **Got it** hid the card.

Found and fixed during this loop:

1. After **Got it**, the same event and threads came back on every visit →
   threads/events now show only when new since last seen (+ test).
2. The manager notice read "…merged Waiting for your review." and linked to a
   route that ignores its query → one sentence, links to the agent page (+ test).
3. A thread's blocker was labelled "why: …" → "waiting on …".
4. `seed_org.py --invite` sent a payload the members endpoint rejects (422) →
   fixed.
5. After a backend code edit the dev auto-reloader hung and the backend stopped
   answering (the sweep was not running; it only works 01:00–05:00) → restarted
   the backend. Worth knowing for long agent sessions.

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
dream proposes the board meeting as a dated memory, refuses nothing it
shouldn't, and plans the follow-up during working hours; `dream_runs.tokens`
is filled from the usage records.

## Metrics to watch after rollout

- `dream_runs` by status/reason per night; tokens per org vs the 2M budget.
- Pending AI suggestions per agent (should fall), merged vs expired counts.
- Nightly suggestions approved vs rejected by reviewers.
- Briefing: items shown, "not useful" rate, habit accept/decline rate.
