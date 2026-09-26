# Agent check-ins — invisible, verified follow-ups

Status: **plan** (not implemented). Build it as a sandbox feedback loop
(`.agents/skills/sandbox-feedback-loop/SKILL.md`); the loop report lands in
`docs/feedback-loops/agent-checkins.md`.

## 1. What we are building

After a normal chat session ends, the system quietly decides whether this
conversation deserves a **follow-up later** ("the month-end data lands Monday —
re-run the reconciliation", "churn was 3.8%; tell them if it crosses 4% after
the next refresh"). If yes, it arms an invisible one-shot job. When the job is
due, cheap code checks and a small judge decide whether it is still worth
running. Only if it runs does anything become visible: a normal machine turn in
the **same report**, followed by one notification.

The user never configures anything. No scheduled task appears in any list. No
tool card appears in the conversation. The org admin can turn the whole feature
on or off in AI settings.

### User-visible contract

| Moment | What the user sees |
|---|---|
| Session ends, check-in planned | **Nothing** |
| Check-in pending (hours–days) | **Nothing** (no card, no scheduled-task row, no badge) |
| Due, cancelled by re-check or judge | **Nothing, ever** |
| Due, judge says RUN | A normal machine turn in the same report: a compact "Follow-up" event strip, the agent's steps/queries/charts, and its reply. Plus one notification (in-app inbox + one external nudge) that deep-links to the report |
| Run concludes nothing changed | Collapsed one-line strip "Checked back — nothing new since {date}" in the report. **No notification** |

### Non-goals (v1)

- Per-user memory redesign and nightly consolidation ("dreaming") — separate plans.
- Recurring check-ins. A check-in is one-shot; if the run itself thinks a
  recurring task is warranted it may *suggest* `create_scheduled_task` to the user
  in its reply, never create one.
- Check-ins for other users (only the report owner / the human who drove the session).

## 2. Existing building blocks (verified)

| Need | Reuse | Where |
|---|---|---|
| Post-analysis hook | Post-loop block that runs the knowledge harness | `backend/app/ai/agent_v2.py:6796-6818` (`# === Post-analysis tasks ===`) |
| Invisible one-shot job | `WaitService.schedule_wait` pattern: APScheduler `date` trigger, module-level callable, `misfire_grace_time` | `backend/app/services/wait_service.py:104-140`, `run_wait_wake` at `:43` |
| Exactly-once fire across workers/replicas | `claim_scheduled_run(job_id)` | `backend/app/core/scheduler.py:109` |
| Machine-initiated turn in the same report | `run_machine_turn(..., trigger_source=...)` — visible `role='external'` strip + hidden `role='user'` trigger + normal `role='system'` reply | `backend/app/services/machine_turn.py:32-110` |
| Hidden trigger prompt in timeline | `get_completions_v2` filter `(webhook_id OR trigger_source) AND role='user'` | `backend/app/services/completion_service.py:1052-1058` |
| Frontend rendering of machine-turn strips | `m.role === 'external' && m.trigger_source` | `frontend/pages/reports/[id]/index.vue:177`, `:1951-2009` |
| Notification (inbox + one nudge, self always included) | `NotifyService.notify(db, sender=, organization=, report=, subject=, body=, source=)` | `backend/app/services/notify_service.py:141` |
| Notification sources | `SOURCE_*`, `SOURCES` | `backend/app/models/notification.py:11-15` |
| "Did the user come back?" | `ReportView.last_viewed_at` (per report/user) | `backend/app/models/report_view.py` |
| Org feature flag | `FeatureConfig` fields, generic rendering in AI settings page | `backend/app/schemas/organization_settings_schema.py:340-360`; `frontend/pages/settings/ai_settings.vue:26` (`regularConfigFeatures`); names/descriptions in `locales/*.json` (`"enable_agent_notes"` at `locales/en.json:2708` is the model) |
| Reading a flag in the agent | `self.organization_settings.get_config("…")` | `backend/app/ai/agent_v2.py:795` |
| Org timezone | `_org_timezone_for_report(report_id)` | `backend/app/services/scheduled_prompt_service.py:25` |
| Small model | `self.small_model` (already used by harness / judge) | `backend/app/ai/agent_v2.py` |
| Reference loop + tests for this exact scheduling style | wait tool | `docs/feedback-loops/wait-tool.md`, `backend/tests/unit/test_wait_tool.py` |

Facts checked while planning that shape the design:

- The knowledge harness runs **only when trigger conditions fire**
  (`_should_suggest_instructions`), and it persists visible blocks / emits
  `instructions.suggest.*` SSE events. So check-in planning must **not** be a
  harness tool (it would render) and must **not** depend on the harness running.
  → It is a separate, silent, post-analysis step in the same block.
- There is **no per-user timezone** and **no per-user notification preference**
  today (grep of `models/user.py`, `models/membership.py`). v1 uses the org
  timezone; a per-user opt-out is added in Phase 4.
- `run_machine_turn` always creates the visible event strip. That is fine: we
  only call it after the judge says RUN.

## 3. Pipeline

```
chat turn completes (agent_v2 post-analysis block)
  ├─ knowledge harness (unchanged)
  └─ NEW CheckinPlanner  [background task, own DB session, no SSE, no blocks]
        pre-filter (code) ──fail──▶ stop (no LLM call)
        small-model plan  ──propose=false──▶ stop
        CheckinPolicy.can_plan (code) ──deny──▶ row status=rejected (reason) 
        insert agent_checkins(status=planned) + arm date job "checkin:<id>"
                         ⋮ (hours/days)
run_checkin_wake(checkin_id)  [module-level, claim_scheduled_run]
        re-check (code) ──stale──▶ status=cancelled (reason)
        outside working hours ──▶ re-arm to next window (no status change)
        collect evidence (code)
        CheckinJudge (small model) ──skip──▶ status=skipped (reason)
        run_machine_turn(trigger_source="checkin", instruction=CHECKIN_PROMPT)
        inspect reply:
            NOTHING_NOTEWORTHY ──▶ status=ran_quiet, collapse strip, no notify
            otherwise          ──▶ status=sent, NotifyService.notify(...)
engagement (lazy, computed on read): opened_at / replied_at
```

### 3.1 Org settings (Phase 1)

Add to `OrganizationSettingsConfig` in
`backend/app/schemas/organization_settings_schema.py`, next to
`enable_agent_notes`:

```python
enable_agent_checkins: FeatureConfig = FeatureConfig(
    value=False, name="Agent check-ins",
    description="Let the agent follow up on its own after a conversation when "
                "something is worth revisiting (e.g. after a data refresh). "
                "Follow-ups run as the user, appear in the same report, and send "
                "one notification. At most a few per user per week.",
    is_lab=True, editable=True)
checkins_max_per_user_per_week: FeatureConfig = FeatureConfig(
    value=2, name="Check-ins per user per week",
    description="Maximum follow-ups a single user can receive in 7 days.",
    is_lab=True, editable=True)
checkins_max_runs_per_org_per_day: FeatureConfig = FeatureConfig(
    value=50, name="Check-in runs per day",
    description="Upper bound on follow-up runs across the organization per day (cost cap).",
    is_lab=True, editable=True)
```

- Default **off** (opt-in per org while in lab).
- Add `name`/`description` for all three keys to **every** `locales/*.json`
  catalog (identical shape is enforced — see `docs/design/i18n.md`). Hebrew must
  follow the vocabulary convention in `AGENTS.md`.
- The setting is read at **plan time and again at fire time** (turning it off
  stops pending check-ins from running; they are marked `cancelled:disabled`).
- Confirm the AI settings page renders the three toggles generically via
  `regularConfigFeatures` (no custom UI needed). If the numeric fields need an
  input type, follow how `max_instructions_in_context` renders.

### 3.2 Data model (Phase 1)

New table `agent_checkins` (model `backend/app/models/agent_checkin.py`,
Alembic migration in `backend/alembic/versions/`; must run on SQLite and
Postgres):

| Column | Type | Notes |
|---|---|---|
| `id` | str(36) pk | |
| `organization_id` | FK organizations, indexed | |
| `user_id` | FK users, indexed | the human the follow-up is for |
| `report_id` | FK reports, indexed | the same report the turn runs in |
| `source_completion_id` | FK completions, nullable | the turn that planned it |
| `kind` | str(32) | `followup` \| `metric_watch` \| `unfinished` \| `data_refresh` |
| `hypothesis` | text | what would make it worth sending; required |
| `plan_reason` | text | planner's one-line justification |
| `due_at` | datetime (UTC) | after clamping + working-hours shift |
| `job_id` | str | `checkin:<id>` |
| `status` | str(24), indexed | `planned` → `cancelled` \| `skipped` \| `running` \| `ran_quiet` \| `sent` \| `failed`; plus `rejected` for policy-denied plans (kept for tuning) |
| `status_reason` | str | machine-readable reason code (see §3.5/§3.6) |
| `judge_reason` | text, nullable | |
| `run_completion_id` | FK completions, nullable | the system reply of the check-in turn |
| `preview` | str(280), nullable | one-line summary used as notification subject/body |
| `sent_at` | datetime, nullable | |
| `created_at` / `updated_at` | | from `BaseSchema` |

Engagement is **derived**, not stored, in v1: `opened` =
`ReportView.last_viewed_at > sent_at`; `replied` = a `role='user'` completion
with `trigger_source IS NULL` in the report after `sent_at`; `ignored` = sent,
not opened within 72h.

Cleanup: on report delete, cancel pending jobs for that report (mirror
`WaitService.cancel_waits_for_report`) and mark rows `cancelled:report_deleted`.

### 3.3 Planning (Phase 2)

**Where:** `agent_v2.py` post-analysis block, after the harness `try` (line
~6818), in the non-training branch. Dispatch as a background task with its own
session (same pattern as `_bg_final_snap` right below it) so it never delays
the user's turn and never emits SSE or blocks.

**Hard pre-filter (code, no LLM)** — skip unless all true:
1. `enable_agent_checkins` is on.
2. `self.mode != "training"`, and not `completion_errored`.
3. The head completion is **human-initiated**: `trigger_source IS NULL` and
   `webhook_id IS NULL`. (Prevents check-ins spawning check-ins, and no planning
   from wait wakes, scheduled prompts, evals, webhooks, Slack bots-as-users is fine.)
4. The session did real work: ≥1 successful data step (`create_data`/query) in
   this report, or ≥2 user turns.
5. `CheckinPolicy.can_plan(user, report)` pre-check passes (cheap DB counts,
   §3.5) — avoids paying for an LLM call we would reject.

**Planner call:** `backend/app/ai/agents/checkins/planner.py`, one
structured small-model call. Input (keep under ~6k tokens):
- report title; last ≤3 user prompts; the final answer text (truncated); titles
  and last-run times of the report's data steps; any scheduled prompt / custom
  query refresh cadence attached to the report's data; today's date and org
  timezone; the user's name and `Membership.memory` if present.

Output schema (pydantic, validated; invalid → no check-in):
```json
{
  "propose": false,
  "kind": "followup|metric_watch|unfinished|data_refresh",
  "due_in_hours": 72,
  "hypothesis": "Churn crosses 4% after the Oct 1 refresh of subscriptions",
  "reason": "User said they'd re-check after month-end close"
}
```
Prompt rules (write them explicitly):
- Default is `propose=false`. Most sessions deserve no follow-up.
- Only propose when there is a **concrete, checkable hypothesis** tied to
  something that will change with time: a stated future event, a data refresh,
  a threshold the user cared about, an unfinished question blocked on data.
- Never propose for: one-off lookups, questions fully answered, curiosity,
  anything the user can trivially re-run, anything about other people's data.
- `due_in_hours` must be justified by the hypothesis (e.g. after the next refresh).

**Persist:** clamp `due_at` to `[now+2h, now+14d]`, shift into the working
window (§3.5), `CheckinPolicy.can_plan` again (authoritative), insert row
`planned`, arm job. Log one line with `checkin_id, report_id, kind, due_at`.

### 3.4 Scheduling (Phase 2)

New `backend/app/services/checkin_service.py`, modelled on `wait_service.py`:

- `arm(checkin) -> job_id`: `scheduler.add_job(func=run_checkin_wake,
  trigger="date", run_date=due_at, id=f"checkin:{id}", kwargs={"checkin_id": id},
  replace_existing=True, misfire_grace_time=6*3600)`.
- `cancel(checkin_id, reason)` and `cancel_for_report(report_id)`.
- `run_checkin_wake(checkin_id)` is a **module-level** async function (the job
  store serializes callables by import path — see `wait-tool.md`); first line is
  `if not await asyncio.to_thread(claim_scheduled_run, job_id): return`.
- Job kwargs carry only the id; everything else is re-read from the DB at fire
  time (so a setting/permission change is respected).

### 3.5 Policy and limits (Phase 1, pure code, unit-tested)

`backend/app/services/checkin_policy.py` — no LLM, all thresholds read from org
settings or module constants:

| Rule | Value | Applies at |
|---|---|---|
| Feature enabled | `enable_agent_checkins` | plan, fire |
| Pending per user | ≤ 1 `planned` | plan |
| Pending per report | ≤ 1 `planned` | plan |
| Sent per user per 7 days | ≤ `checkins_max_per_user_per_week` (2) | plan, fire |
| Runs per org per day | ≤ `checkins_max_runs_per_org_per_day` (50) | fire |
| Ignore back-off | 2 most recent `sent` both `ignored` → no plans for 14 days | plan |
| Due window | clamp to `[+2h, +14d]` | plan |
| Working window | Mon–Fri 09:00–18:00 **org timezone**; outside → move to next window start + random 0–90 min jitter | plan, fire |

Reason codes (store in `status_reason`): `disabled`, `pending_exists_user`,
`pending_exists_report`, `weekly_cap`, `org_daily_cap`, `ignore_backoff`,
`report_deleted`, `access_lost`, `user_returned`, `user_active`,
`judge_skip`, `nothing_noteworthy`, `run_failed`.

### 3.6 Fire-time re-check (Phase 3, code, before any LLM)

In `run_checkin_wake`, load the row; proceed only if `status == planned`, then:
1. Feature still enabled → else `cancelled:disabled`.
2. Report exists; user still an active org member; user can still read the
   report and its data sources (reuse the report access check the completion
   route uses) → else `cancelled:access_lost` / `report_deleted`.
3. **User returned:** `ReportView.last_viewed_at > checkin.created_at` or a
   human completion in the report after `created_at` → `cancelled:user_returned`.
4. **User active now:** any human completion by this user in the org in the last
   30 min → re-arm +2h (don't interrupt a live session; don't stream into an
   open report).
5. Caps (§3.5) → `cancelled:weekly_cap` / `org_daily_cap`.
6. Outside working window → re-arm, keep `planned`.

### 3.7 Judge (Phase 3)

`backend/app/ai/agents/checkins/judge.py`, one small-model structured call.

**Evidence (collected in code, no queries against customer data):**
- `elapsed`: time since the planning turn; whether `due_at` was tied to an event.
- `data_refreshed`: for the report's data steps / attached custom queries /
  scheduled prompts, whether any refresh completed since `created_at`
  (last-run timestamps from `custom_query_service` / `report_service` cron runs /
  `scheduled_prompts.last_run_at`). v1 may only have partial coverage — pass
  `unknown` rather than guessing.
- `values_changed`: if a refreshed step/custom query stores results, whether the
  stored result hash/row count changed since the planning turn (compare stored
  artefacts only — never re-run a query here).
- `engagement`: this user's last N check-ins by kind with opened/ignored.
- `user_memory`: `Membership.memory` text (e.g. "don't ping me about X").
- `hypothesis`, `kind`, `plan_reason`, report title, last answer excerpt.

**Output:** `{ "run": false, "reason": "...", "focus": "what the run should check" }`.

**Rules:** default `run=false`. `run=true` only if the evidence makes the
hypothesis plausibly *changed or decidable now* (e.g. data refreshed since, or
the awaited date passed). No evidence of change → skip. User memory or
engagement says they don't want this kind → skip.

### 3.8 The check-in run (Phase 3)

Call `run_machine_turn(session, report=, user=, organization=,
trigger_source="checkin", message_type="checkin_event",  # wait uses "wait_resume_event" (wait_service.py:95)
summary=<fallback text>, meta={"kind": kind, "checkin_id": id},
instruction=CHECKIN_PROMPT)`. Set the row `running` before, store
`run_completion_id` after (latest `role='system'` completion in the report).

- Runs **as the user**, full normal tool set (it may create queries, charts,
  notes — accepted by product).
- Guard: the planner pre-filter (§3.3.3) already prevents this turn from
  planning another check-in because its trigger prompt has `trigger_source`.

`CHECKIN_PROMPT` (template; keep in `backend/app/ai/agents/checkins/prompts.py`):

> You are following up, on your own initiative, on this conversation with
> {user_name}. When it ended you planned to check: "{hypothesis}"
> (reason: {plan_reason}). Focus: {judge_focus}.
> Re-run or re-inspect only what is needed to decide this. Compare against what
> this report already showed — the user has seen that.
> If nothing meaningful changed or the hypothesis is not met, reply with exactly
> `NOTHING_NOTEWORTHY` and one short sentence of what you checked.
> Otherwise reply in ≤5 short lines: what changed, the number that matters, and
> one suggested next step. Include one chart only if it makes the change obvious.
> Do not create scheduled tasks or send notifications yourself.

### 3.9 Outcome and delivery (Phase 3)

- Reply starts with `NOTHING_NOTEWORTHY` → status `ran_quiet`, set event
  strip `meta.quiet = true` (frontend collapses the reply under the strip),
  **no notification**. Counts toward the org daily run cap, **not** the user's
  weekly cap.
- Otherwise → status `sent`, `sent_at=now`, `preview` = first line of the reply
  (≤280 chars), then `NotifyService.notify(db, sender=user, organization=org,
  report=report, subject=<localized "Follow-up: {report title}">,
  body=preview + deep link, source=SOURCE_CHECKIN)` — recipients empty, so the
  service delivers to self only (in-app inbox + one nudge).
- Add `SOURCE_CHECKIN = "checkin"` to `models/notification.py` and `SOURCES`;
  make sure the inbox UI renders it (label/icon + i18n keys).
- Run raised → `failed:run_failed`, no notification. (The event strip already
  shows ❌ per `run_machine_turn`.)

### 3.10 Frontend (Phase 3)

- `frontend/pages/reports/[id]/index.vue` (`:177`, `:1951-2009`): add
  `trigger_source === 'checkin'` label + icon ("Follow-up") and the
  `meta.quiet` collapsed variant ("Checked back — nothing new since {date}",
  expandable to show the reply).
- Inbox: render `source='checkin'` rows.
- AI settings: three new settings render generically; verify.
- i18n: all new strings in every `locales/*.json`; RTL check in `he`.
- UI changes require before/after evidence via the **ui-evidence** skill.

### 3.11 Per-user opt-out (Phase 4)

- `Membership.checkins_opt_out` (bool, default false) + toggle on the user's
  profile/notification area; checked at plan and fire.
- In the check-in reply UI, a small "Don't follow up like this" action that sets
  the opt-out (or records a memory line — decide with product).

## 4. Phases and exit criteria

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Settings (+locales), `agent_checkins` model + migration, `CheckinPolicy` | Unit tests for every policy rule pass on sqlite + postgres; setting renders in AI settings |
| 2 | Planner + `CheckinService.arm/cancel` + post-analysis dispatch | With setting on and a stubbed planner, a qualifying chat turn creates exactly one `planned` row and one `checkin:<id>` job; setting off / machine turn / training → zero rows, zero LLM calls |
| 3 | Wake, re-check, evidence, judge, machine turn, outcome, notify, frontend strip | Loop A scenarios below all pass; Loop B shows a real follow-up end to end |
| 4 | Per-user opt-out, engagement back-off tuning, admin visibility (optional read-only list of org check-ins for debugging) | Opt-out respected at plan and fire |

## 5. Feedback loop (to run in the new session)

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md`. Environment:

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

### Loop A — deterministic (no real LLM, no real clock)

Stub at the boundaries only (LLM provider for planner/judge/agent; clock via the
repo's clock utilities; scheduler via the same stub pattern as
`tests/unit/test_wait_tool.py`). Seed via `tests/fixtures/*`.

Unit (`backend/tests/unit/test_checkin_policy.py`, `test_checkin_service.py`):
1. Each policy rule allows at the boundary and denies one past it (vary values;
   don't encode one magic number).
2. Working-window shift: Friday 17:59 → allowed; Friday 18:01 → next Monday
   09:00–10:30 in org timezone; DST boundary case.
3. `arm` registers a `date` job with id `checkin:<id>`; `cancel` removes it;
   `run_checkin_wake` is module-level and round-trips through
   `obj_to_ref`/`ref_to_obj`.
4. Planner output validation: malformed JSON / missing hypothesis → no row.

E2E (`backend/tests/e2e/test_agent_checkins.py`, both `--db=sqlite` and `--db=postgres`):
5. **Setting off** → a completed chat turn creates 0 rows and makes 0 planner calls.
6. **Setting on, planner says propose** → exactly 1 `planned` row, 1 job, and the
   report timeline (`GET` completions v2) is **unchanged** (no new blocks/entries).
7. **Machine turn never plans** → a turn with `trigger_source` set (wait wake /
   check-in run) creates 0 rows.
8. **Fire → user returned** (bump `ReportView`) → `cancelled:user_returned`, no
   new completions, no notification.
9. **Fire → judge skip** → `skipped:judge_skip`, timeline unchanged, no notification.
10. **Fire → judge run → noteworthy reply** → one event strip + reply in the same
    report, status `sent`, exactly one inbox notification for the user with
    `source='checkin'`.
11. **Fire → `NOTHING_NOTEWORTHY`** → status `ran_quiet`, strip `meta.quiet`,
    **zero** notifications, weekly cap unchanged.
12. **Caps** → third check-in in 7 days is rejected at plan (`weekly_cap`);
    org daily cap cancels at fire.
13. **Permissions (non-admin path)** → user removed from the data source's
    access before fire → `cancelled:access_lost`; nothing runs.
14. **Idempotent fire** → invoking the wake twice for the same job runs the turn
    once (claim).

Each test must be watched failing first (stash the implementation) per
`backend/tests/AGENTS.md` rule 6.

### Loop B — live (real LLM, real stack)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
```
1. Enable `enable_agent_checkins` in `/settings/ai_settings`; screenshot.
2. In a report on seeded demo data, have a conversation that plainly warrants a
   follow-up ("the data refreshes tomorrow; I'll want to know if revenue drops
   below X"). Confirm in DB: one `agent_checkins` row `planned`, sensible
   `hypothesis`/`due_at`; confirm the report UI shows **nothing** new.
3. Have a conversation that does **not** warrant one (a one-off lookup) — confirm
   no row. Repeat 5–10 varied sessions and record the propose rate (target:
   well under half).
4. Force the job due (admin/debug path or set `due_at` to now + re-arm) — no
   waiting in real time. Observe re-check → judge → run → report strip +
   reply + inbox notification. Screenshot before/after (ui-evidence skill).
5. Repeat with data unchanged → expect `skipped` or `ran_quiet`, no notification.
6. Record in the loop doc: planner/judge prompts used, propose rate, judge
   run rate, quiet rate, token cost per stage, and any false positives.

### Metrics to report in the loop doc

- plan rate = planned / eligible sessions (want low)
- run rate = runs / fired (want low–moderate)
- quiet rate = ran_quiet / runs (high means the judge is too loose)
- open rate = opened / sent (want high); ignored → back-off working
- cost per stage (planner, judge, run)

## 6. Risks and decisions to confirm with product

- **Visibility of outside-turn side effects:** the run may edit notes/dashboards;
  accepted for v1.
- **Default state:** off per org (lab). Revisit after Loop B metrics.
- **Evidence coverage:** v1 freshness signals may be partial; the judge must
  treat `unknown` as "no evidence" and skip — prefer missing a follow-up over
  sending noise.
- **Who is the recipient on shared reports:** v1 = the human who drove the
  planning turn only.
- **Channel:** in-app inbox always + one external nudge via existing
  `NotifyService` preference order (Teams → Slack → Google Chat → email).
