# Feedback Loop — Agent check-ins: follow-ups that stay invisible until verified

After a normal chat turn, the system quietly decides whether the conversation
deserves **one** follow-up later ("finance loads next month's invoices Monday —
tell me if Rock drops below 30%"). If so it writes a note to its future self and
arms an invisible one-shot job. When the job comes due: code guardrails run
first (facts), then a small judge model decides run/skip **with a reason**, then
— only on run — a normal machine turn runs in the same report and calls
`notify` only on explicit criteria. The user configures nothing; org admins turn
the whole feature on in AI settings (lab, **off by default**); users can opt out
for themselves in their profile. Every decision is visible to admins in the
TraceModal, including the ones that left nothing visible to the user.

Evidence (screenshots, before/after): `media/pr/ai-youthful-ptolemy-fdyk9o/`.

## What was built

| Layer | Where |
|---|---|
| Settings (3 lab `FeatureConfig`s, gated at every step) | `app/schemas/organization_settings_schema.py` (`enable_agent_checkins`, `checkins_max_per_user_per_week`, `checkins_max_runs_per_org_per_day`) |
| Table `agent_checkins` + reason codes | `app/models/agent_checkin.py`, `alembic/versions/agentcheckins01_add_agent_checkins.py` |
| Per-user opt-out (Phase 4) | `memberships.checkins_opt_out` (`agentcheckins02_…`), `GET/PUT /api/users/me/checkins` (`app/routes/user_profile.py`), profile toggle (`components/UserProfileModal.vue`) |
| Guardrails — code only | `app/services/checkin_policy.py` (clamp +2h..+14d, Mon–Fri 09–18 org-tz window + 0–90 min jitter, pending/user, pending/report, weekly & org-daily caps, access, live-run, opt-out) |
| Planner / judge / trigger prompt | `app/ai/agents/checkins/{planner,judge,prompts,_llm}.py` |
| Lifecycle (dispatch, arm/cancel, wake, fire, run, outcome) | `app/services/checkin_service.py` (`run_checkin_wake` is module-level; `claim_scheduled_run` + an atomic `planned→running` claim) |
| Post-turn hook | `app/ai/agent_v2.py` post-analysis block (`_checkins_enabled()` first → no task, no LLM call when off) |
| Settings-off hook / archive hook | `organization_settings_service.update_settings`, `report_service._cancel_checkins_for_reports` |
| `notify` guardrails in a check-in run | `app/ai/tools/implementations/notify.py` (self only, one call, setting re-checked, `source='checkin'`); `send_email` refuses and is hidden in check-in runs |
| Trace | `CheckinTraceSchema` + `ConversationTraceResponse.checkins`, turns carry `trigger_source`/`checkin_id` (`console_service._report_checkins`) |
| UI | report strip + quiet collapse (`pages/reports/[id]/index.vue`), inbox assistant-style row (`components/NotificationModal.vue`), TraceModal card + judge panel (`components/console/CheckinTraceCard.vue`, `TraceModal.vue`), AI-settings dependent fields (`pages/settings/ai_settings.vue`), `locales/{en,es,he}.json` |
| Debug path | `tools/agent/fire_checkin.py` — fire a check-in now through the real path, at its due time (default) or at real now |

Status lifecycle: `not_proposed` · `rejected:<code>` · `planned` → (`cancelled:<code>` | `skipped:judge_skip|invalid_judge_output` | `running` → `sent` | `ran_quiet` | `failed:run_failed`).

## Loop A — deterministic (no real LLM, no real clock)

Boundaries stubbed: the small-model call (`call_small_model` → scripted JSON),
the agent's LLM loop (a scripted stand-in whose "decisions" are tool calls made
through the **real** `notify` tool), the clock (`checkin_service._utcnow`), the
scheduler (recording stub, same pattern as `test_wait_tool.py`). Everything
else — routes, services, DB, completion pipeline, `run_machine_turn`,
`NotifyService`, inbox — runs real.

```bash
cd backend && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_checkin_policy.py tests/unit/test_checkin_service.py      # 63 passed
uv run pytest tests/e2e/test_agent_checkins.py --db=sqlite                              # 51 passed
# Postgres leg (no Docker in this sandbox → a local PG 16 via --db=external)
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/bow_test \
  uv run pytest tests/e2e/test_agent_checkins.py --db=external                          # 51 passed
```

Scenarios covered (e2e): setting off → 0 rows / 0 planner calls; planner
proposes → exactly 1 `planned` row + 1 `checkin:<id>` job, timeline unchanged;
declines → `not_proposed`, no job; invalid planner output → nothing; machine
turns (`wait`, `checkin`, `eval_run`) never plan; setting off while pending →
job removed, `cancelled:disabled`, later fire no-op; fire with setting off →
cancelled, no judge; judge skip → `skipped`, reason stored, timeline unchanged,
no notification; judge invalid → `skipped:invalid_judge_output`; judge run +
notify → one strip + reply, `sent`, exactly one inbox row `source='checkin'`;
run without notify → `ran_quiet`, 0 notifications, strip meta `outcome`;
`notify` rejects recipients / a second call / a mid-run setting flip;
`send_email` refused in a check-in run; weekly cap boundary (cap ∈ {1,2,3});
runs older than 7 days don't count; pending per user/report (no planner call);
org daily cap cancels at fire (cap ∈ {1,3}); member losing data-source access
→ `cancelled:access_lost`; archiving the report → `cancelled:report_deleted`;
idempotent fire (claim window) and re-delivered fire outside it (atomic claim);
live run in the report → re-armed +30 min; trace lists every row (sent,
not_proposed, skipped, cancelled) with note + judge reason, links the run turn,
and is 403 for a plain member; opt-out: preference API, never planned, opting
out cancels pending, respected at fire.

**Every test can fail** (rule 6) — mutation runs, each restored afterwards:

| Mutation | Result |
|---|---|
| drop the human-only eligibility guard | 3 failed |
| drop the settings-off hook | 2 failed |
| disable `notify` check-in guardrails | 4 failed |
| skip the access check | 1 failed |
| skip the fire-time setting check | 1 failed |
| drop the atomic `planned→running` claim | 1 failed (re-delivered fire) |
| weekly cap `>=` → `>` (off by one) | 3 failed |
| drop the `send_email` refusal | 1 failed |
| drop plan-time / fire-time opt-out | 1 failed each |
| drop the usage-loop bind (see bug 3) | 1 failed (unit) |
| drop the agent_v2 post-analysis hook / its setting gate | 2 failed / 1 failed |
| no failure path around a claimed fire | 2 failed |
| `failed` wins over `notified` | 1 failed |
| no owner check before planning | 1 failed |
| stale sweep a no-op | 2 failed |
| keep the read transaction open during the planner / judge call | 1 failed each |

Adjacent suites re-run green: console metrics, org settings, report
notifications, archive guard, conversation access, diagnosis explorer, agent
notes (68 e2e) and inbox / notify service / notify tool / wait tool / settings
cache (29 unit).

## Loop B — live (real LLM, real stack, real SMTP)

Stack: `tools/agent/boot_stack.sh` (prod build), pinned `BOW_ENCRYPTION_KEY`,
`BOW_CONFIG_PATH` with global SMTP → a local sink on :2527. Two `aiosmtpd`
relays log every accepted message to JSONL: the **org relay** (:2526, AUTH
required) and the global sink. Everything was driven through the UI with
Playwright as a real user ("Dana Analyst"): sign-up → onboarding skipped →
`/settings/models` OpenAI provider (**GPT‑6 Luna**, default + small default,
"Successfully connected") → `/settings/smtp` pointed at the org relay ("Save &
send test email" landed in `org.jsonl`) → Music Store demo agent →
`/settings/ai_settings` switch on → chats from the home prompt box.

"Finance's Monday load" is simulated by inserting January 2026 invoices (Rock
16.7% of the month) into a scratch copy of the demo DB the connection points to.
Jobs are forced due with `tools/agent/fire_checkin.py` (fires at `due_at`, the
check-in's own clock; no real-time waiting).

### What happened (every row, from `agent_checkins`)

| # | Conversation | Planner | Judge | Outcome |
|---|---|---|---|---|
| 1 | Rock share + "finance loads Monday; <30% → cut promo" | planned (due Mon 10:24) | **skip** — fired at real Saturday-now: "it is currently Saturday… no indication the Monday load has happened" | `skipped` (correct; led to the at-due debug clock) |
| 2 | same, agent itself created a Monday scheduled task | planned (**false positive: duplicate**) | **skip** — "a recurring 'Monday Rock Revenue Alert' … would duplicate" (after the fix) | `skipped` |
| 3 | same | planned | run | `ran_quiet` — **bug: agent used `send_email`**, an email went out but the row said quiet |
| 4 | same, January loaded | planned (due Mon 10:33) | run — "Monday load is due; step last ran Saturday" | **`sent`**: `run_query` → `notify`; inbox row + email via org SMTP with deep link; user opened it |
| 5–9 | 5 one-off lookups (track count, top artists, avg invoice, top support agent, customers per country) | **not_proposed** ×5, each with a specific reason | — | nothing visible |
| 10 | "USA #1 by customers; CRM syncs overnight" (agent created a daily task) | **not_proposed** — "the daily scheduled alert already checks…" (duplicate rule, live) | — | — |
| 11 | Rock scenario, data **unchanged** | planned | run | **`ran_quiet`** — reply collapsed under "Checked back: nothing new"; 0 emails, 0 notifications |
| 12 | USA share scenario | planned | — | admin turned the switch **off in the UI** → job gone from `apscheduler_jobs`, `cancelled:disabled` |
| 13 | Rock scenario with the switch off | **no call** | — | 0 rows, 0 planner usage records |
| 14 | switch back on; Dana turns **"Agent follow-ups" off in her profile**, then the Rock scenario | **no call** | — | 0 rows, 0 planner usage records (`GET /users/me/checkins` → `{"enabled": false, "available": true}`) |

The sent email as captured by the org relay (`org.jsonl`):

```
relay=org  rcpt=['dana@loopb.example.com']
subject: Rock's January revenue share fell below the 30% cutoff
The newest invoice month is January 2026, and Rock's revenue share is 16.7%,
down from 76.9% in December 2025. This is below your 30% cutoff; review the
Rock promo budget for a reduction.
— Open this in Bag of words:
http://localhost:3000/reports/8c3bb028-…
```

### Metrics

| Metric | Formula | Value | Note |
|---|---|---|---|
| Plan rate | planned / eligible turns | 6 / 12 (50%) | the loop deliberately oversampled follow-up-worthy chats; **one-off lookups 0 / 5** |
| Judge run rate | run / judged | 3 / 5 | both skips were correct (not yet due; duplicate) |
| Quiet rate | ran_quiet / runs | 2 / 3 → **1 / 2 after the send_email fix** | the quiet run was genuinely quiet (data unchanged) |
| Notify rate | sent / runs | 1 / 3 → 1 / 2 after the fix | |
| Open rate | opened / sent | 1 / 1 | `report_views.last_viewed_at` 22:35:09 > `sent_at` 22:34:30 (clicked the inbox deep link) |
| Cost — planner | per call | ~764 tokens, ~$0.00015 | 12 calls, $0.0018 total |
| Cost — judge | per call | ~973 tokens, ~$0.00018 | recorded only after bug 3 was fixed |
| Cost — run | per run | 92k–193k tokens, $0.006–$0.014, 43–84 s | a normal agent turn |

### False positives / negatives and bugs found — each fixed and re-verified

1. **Planner FP — duplicate of a schedule the agent just created (#2).** The
   main agent had already called `create_scheduled_task` in that turn; the
   planner still proposed. *Fix:* planner and judge prompts treat an existing
   scheduled task / pending wait in the report as "already covered"; the planner
   context now lists scheduled tasks **and pending waits**.
   *Verified live:* #2's judge skipped with that reason; #10's planner declined
   with "the daily scheduled alert already checks…".
2. **Bug — `send_email` bypassed every check-in guardrail (#3).** The run
   emailed the user through `send_email`: no self-only/one-call/setting checks,
   no `source='checkin'` inbox row, and the row was recorded `ran_quiet`.
   *Fix:* `send_email` (and schedule editing) hidden from check-in runs' tool
   catalog, and `send_email` refuses at runtime (`CHECKIN_USE_NOTIFY`); e2e
   test + mutation. *Verified live:* #4 used `notify`.
3. **Bug — judge cost silently dropped.** `LLM.inference` runs in a worker
   thread and schedules its usage write onto a loop captured by an earlier
   *async* LLM call; a scheduler fire right after a restart (or the debug
   script) has none, so the judge's usage was never recorded and the trace card
   showed no judge cost. *Fix:* `bind_usage_loop()` (`app/ai/llm/llm.py`)
   called by the check-in small-model call; unit test + mutation. *Verified
   live:* #11 judge usage recorded (973 tokens, $0.00018) and shown on the card.
4. **Wording — "Dana was already notified, so I did not send a duplicate
   alert"** in #4's reply, although that run sent it. *Fix:* trigger prompt:
   say plainly "I've sent you a notification" if (and only if) this run called
   notify. *Verified live:* #11's reply addresses Dana and doesn't mention
   notifications.
5. **Debug clock (#1).** Forcing a Monday check-in at real Saturday-now made the
   judge (correctly) skip. `fire_checkin.py` now fires at the check-in's
   `due_at` by default (`--now` keeps the old behaviour).
6. **UI — dependent settings overhung the parent column by 24 px.** Fixed
   (padding instead of margin); re-shot.

## Review fixes (second pass)

A review of the first version found six issues; each is fixed, covered by a
test that fails without the fix, and the live ones were re-checked on the stack.

1. **A check-in could stay `running` forever.** `fire()` claims the row
   (`planned → running`) before the guardrails and the judge, none of which
   were guarded, and a restart mid-run left the row as it was. Now everything
   after the claim runs inside a failure path that records
   `failed:fire_error` (or `sent:fire_error` if the user was already
   notified), and an hourly leader-only job (`checkin_stale_sweep` →
   `sweep_stale_running`) fails rows still `running` after 3 h as
   `stale_running` and closes their open strip.
2. **`notify` succeeded, then the turn errored → `failed`.** A notified run is
   now always `sent` (with `status_reason=run_failed` when the turn errored),
   so it counts toward the caps and the trace no longer claims "nothing was
   sent".
3. **The quiet collapse needed a reload** when the report was open during the
   run (the outcome is stamped after the page's end-of-run refresh). The strip
   now derives the outcome from the reply itself until the stamp lands (a
   successful `notify` call ⇒ sent, else quiet). Live: a check-in fired while
   the report was open collapsed to "Checked back: nothing new" with no reload
   (a `window` marker survived) — `80_live_quiet_collapsed_no_reload.png`.
4. **The planner/judge LLM calls held a pooled DB connection.** The read
   transaction is committed before each call (`expire_on_commit=False`).
   Measured with pool checkout/checkin events: 1 connection held during the
   planner call before, 0 after; now a permanent test for both calls.
5. **A turn by someone other than the report owner paid for a planner call
   that could never run.** Planning now requires the asker to own the report
   (the same owner-only gate the fire-time access check applies).
6. **7 of the 10 locale catalogs lacked the new keys.** ar, de, fr, it, pt,
   ru and sv now carry all of them (placeholders checked against `en`).

Also: the e2e suite now runs the **real `AgentV2` loop** (only the planner's
LLM stream and sync `LLM.inference` stubbed) to exercise the post-analysis hook,
its setting gate and the ids it dispatches, including a machine turn that the
hook dispatches and eligibility then rejects.

## Final prompts

Source of truth: `backend/app/ai/agents/checkins/prompts.py`.

- **Planner** (`PLANNER_SYSTEM` + `render_planner_prompt`): default
  `propose=false`; propose only for a stated future event, an awaited refresh,
  a threshold the user cares about, or a question blocked on missing data; not
  for one-off lookups, answered questions, curiosity, trivially re-runnable
  things, **or anything a scheduled task / pending wait in the report already
  covers**; the note must be self-contained (what, how, which result is worth
  notifying); `due_in_hours` follows from the note. Input: now (org tz), user,
  report title, ≤3 human prompts, the final answer (truncated), data steps with
  last-run times, scheduled tasks and pending waits.
- **Judge** (`JUDGE_SYSTEM` + `render_judge_prompt`): loose on purpose; run
  when the note's question is plausibly answerable/changed; skip when resolved
  since, nothing it depends on can have changed, history shows these get
  ignored, **or a scheduled task already re-checks it**; "unknown" refresh is
  weighed, not a no; a concrete reason is required for both decisions, plus a
  focus on run. Input: note, plan reason, planned-ago and due time, last answer
  excerpt, new human turns since, step/schedule refresh times (data source
  refresh "unknown"), the user's last check-ins (decision, outcome, notified,
  opened).
- **Trigger** (`render_checkin_prompt`): as specified — note / why / why now /
  focus; notify only on threshold met, blocked question answerable, or ≥10%
  material change (`MATERIAL_CHANGE_PCT`); never on unchanged data, sub-bar
  changes, own errors, already-seen findings or unrelated findings; recipients
  empty, subject ≤120 chars, body = what changed + the one number with its
  previous value + one next step; always end with a ≤5-line reply addressed to
  the user that mentions a notification only if this run sent one; no
  scheduled tasks.

## Decisions taken

- `ran_quiet` is exposed through the strip's `prompt.meta` (`outcome`,
  `run_completion_id`, `notify_subject`), stamped after the run — no
  completions-v2 schema change.
- A limits pre-check denial at plan time records a `rejected:<code>` row (no LLM
  call) so caps are visible in the trace; invalid planner output records nothing.
- Idempotency is two layers: `claim_scheduled_run` (30 s bucket) and an atomic
  `UPDATE … WHERE status='planned'` claim, so a re-delivery in a later bucket is
  still a no-op.
- Check-in runs hide `create_scheduled_task`, `edit_scheduled_task`, `wait` and
  `send_email`.
- In-app delivery always happens; the external nudge follows `NotifyService`'s
  order (Teams → Slack → Google Chat → email). With no chat platform, the org
  SMTP carried it. Check-in inbox rows render as a message from the org's AI
  analyst name with a "Follow-up" badge and an "Open report" deep link.
- The 10% material-change threshold stays a module constant; Loop B gave no
  signal it needs to be per-org yet.

## Re-running Loop B

```bash
tools/agent/boot_stack.sh                  # with BOW_ENCRYPTION_KEY / BOW_CONFIG_PATH pinned
# sign up + configure LLM/SMTP/AI settings in the UI (or seed_org.py --demo)
# chat something that warrants a follow-up, then:
cd backend && TESTING=true TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/fire_checkin.py --latest [--report <id>]
```
