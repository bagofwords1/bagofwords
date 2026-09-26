# Agent check-ins: follow-ups that stay invisible until verified

Status: **plan** (nothing implemented yet). Build it as a sandbox feedback loop
(`.agents/skills/sandbox-feedback-loop/SKILL.md`). The loop report goes in
`docs/feedback-loops/agent-checkins.md`.

## 1. What we are building

After a normal chat turn ends, the system quietly decides whether this
conversation deserves a **follow-up later**. Examples: "the month-end data
lands Monday, re-run the reconciliation"; "churn was 3.8%, worth telling them if
it tops 4% after the next refresh".

If it does, the system writes a short **note to its future self** and arms an
invisible one-shot job. When the job comes due:

1. Code guardrails run first. They are facts, not judgments.
2. A small **judge** model decides whether the check-in is worth running now,
   and **explains why**, whether it says yes or no.

If the judge says run, a normal machine turn runs in the **same report**. That
turn uses the existing `notify` tool to message the user, but only when the
result meets explicit criteria.

The user configures nothing. No scheduled task appears in any list, and no tool
card appears in the conversation. Org admins turn the whole feature on or off in
AI settings, and **every step is gated by that setting**. Admins and debuggers
can see the full lifecycle of every check-in in the **TraceModal**.

### What the user sees

| Moment | What the user sees |
|---|---|
| Check-in planned, pending, cancelled or skipped | **Nothing** in the report or anywhere else |
| Judge says run | A normal machine turn in the same report: a compact "Follow-up" event strip, then the agent's steps, queries, charts and a short reply |
| The run calls `notify` | One notification (in-app inbox plus one external nudge) with a deep link to the report |
| The run does not call `notify` | The turn stays in the report, collapsed under the strip ("Checked back: nothing new"). **No notification** |

### Out of scope for v1

- Recurring check-ins. A check-in fires once. If the run thinks a recurring
  task makes sense, it may *suggest* one in its reply, but it never creates one.
- Messaging anyone other than the human who drove the planning turn.

## 2. What we reuse (checked against the code)

| Need | What to reuse | Where |
|---|---|---|
| Hook after the turn | The post-analysis block that runs the knowledge harness | `backend/app/ai/agent_v2.py:6796-6818` (`# === Post-analysis tasks ===`) |
| Background work with its own DB session | The `_bg_final_snap` pattern right below that hook | `backend/app/ai/agent_v2.py:~6826` |
| Invisible one-shot job | `WaitService.schedule_wait`: APScheduler `date` trigger, module-level callable, `misfire_grace_time` | `backend/app/services/wait_service.py:104-140`, `run_wait_wake` at `:43` |
| Each fire runs exactly once across workers and replicas | `claim_scheduled_run(job_id)` | `backend/app/core/scheduler.py:109` |
| Machine turn in the same report | `run_machine_turn(..., trigger_source=, message_type=)`. It creates a visible `role='external'` strip, a hidden `role='user'` trigger and a normal `role='system'` reply. Wait passes `message_type="wait_resume_event"` | `backend/app/services/machine_turn.py:32-110`, `wait_service.py:95` |
| Hide the trigger prompt from the timeline | The `get_completions_v2` filter `(webhook_id OR trigger_source) AND role='user'` | `backend/app/services/completion_service.py:1052-1058` |
| Machine-turn strip in the report UI | Rendered when `m.role === 'external' && m.trigger_source` | `frontend/pages/reports/[id]/index.vue:177`, `:1951-2009` |
| Notifying the user | The `notify` tool. With empty `recipients` it sends to the requesting user only. It is backed by `NotifyService.notify(..., source=)` | `backend/app/ai/tools/implementations/notify.py`, `backend/app/services/notify_service.py:141` |
| Notification source labels | `SOURCE_*` and `SOURCES` | `backend/app/models/notification.py:11-15` |
| Org feature flag | `FeatureConfig` fields. The AI settings page renders them generically. Names and descriptions live in `locales/*.json` | `backend/app/schemas/organization_settings_schema.py:340-360` (model: `enable_agent_notes`); `frontend/pages/settings/ai_settings.vue:26`; `locales/en.json:2708` |
| Reading a flag | `organization_settings.get_config("…")` | `backend/app/ai/agent_v2.py:795` |
| Org timezone | `_org_timezone_for_report(report_id)` | `backend/app/services/scheduled_prompt_service.py:25` |
| Trace UI | `TraceModal` loads `GET /api/console/reports/{id}/conversation`, which returns `ConversationTraceResponse`, and shows per-turn detail via `…/agent_executions/by-completion/{id}` | `frontend/components/console/TraceModal.vue:910,1145`; `backend/app/schemas/agent_execution_trace_schema.py:88`; `backend/app/services/console_service.py:2078-2356` |
| Reference loop and tests for this scheduling style | The wait tool | `docs/feedback-loops/wait-tool.md`, `backend/tests/unit/test_wait_tool.py` |

Things the planning checks turned up that shape the design:

- **Planning cannot be a harness tool.** The knowledge harness runs only when
  its trigger conditions fire, and it saves visible blocks and emits
  `instructions.suggest.*` SSE events. So check-in planning is a **separate,
  silent step** in the same post-analysis block.
- **No per-user timezone exists today.** v1 uses the org timezone.
- **`run_machine_turn` always creates the visible strip.** That is fine: we only
  call it after the judge says run.

## 3. Flow

```
chat turn completes (agent_v2 post-analysis block)
  ├─ knowledge harness (unchanged)
  └─ [setting on?] ── no ──▶ stop (no task, no LLM call)
       └─ CheckinPlanner  (background task, own DB session, no SSE, no blocks)
            eligibility (code) ──no──▶ stop
            planner (small model) ── propose=false ──▶ record "not_proposed"
            [setting on?] + limits (code) ──deny──▶ record "rejected:<code>"
            record "planned" (note, due_at) + arm date job "checkin:<id>"
                            ⋮ hours or days later
run_checkin_wake(checkin_id)   (module-level, claim_scheduled_run)
       [setting on?] ──no──▶ "cancelled:disabled"
       guardrails (code) ──fail──▶ "cancelled:<code>" or re-arm
       judge (small model): run | skip, always with a reason
            skip ──▶ "skipped" (judge_reason)
            run  ──▶ [setting on?] ──▶ run_machine_turn(trigger_source="checkin")
                        the agent decides whether to call `notify`
                        (check-in guardrails inside the notify tool)
       result: notify called ──▶ "sent"; not called ──▶ "ran_quiet"
       every step is written to agent_checkins and visible in TraceModal
```

## 4. Settings gate every step (Phase 1)

Add three fields to `OrganizationSettingsConfig` in
`backend/app/schemas/organization_settings_schema.py`, next to
`enable_agent_notes`:

```python
enable_agent_checkins: FeatureConfig = FeatureConfig(
    value=False, name="Agent check-ins",
    description="Let the agent follow up on its own after a conversation when "
                "something is worth revisiting (e.g. after a data refresh). "
                "Follow-ups run as the user in the same report and notify them "
                "only when something meaningful changed.",
    is_lab=True, editable=True)
checkins_max_per_user_per_week: FeatureConfig = FeatureConfig(
    value=2, name="Check-ins per user per week",
    description="Maximum follow-up runs a single user can receive in 7 days.",
    is_lab=True, editable=True)
checkins_max_runs_per_org_per_day: FeatureConfig = FeatureConfig(
    value=50, name="Check-in runs per day",
    description="Upper bound on follow-up runs across the organization per day (cost cap).",
    is_lab=True, editable=True)
```

- The feature is **off by default** while it is in lab.
- Add `name` and `description` for all three keys to **every** `locales/*.json`
  catalog. The catalogs must keep an identical shape (see `docs/design/i18n.md`).
  Hebrew follows the vocabulary rules in `AGENTS.md`.
- The two limit fields are read only while `enable_agent_checkins` is on. If
  `ai_settings.vue` supports showing a field as dependent on another, use it.

`enable_agent_checkins` is checked at **every** point below:

| Point | What happens when the setting is off |
|---|---|
| Post-analysis dispatch | This is the first check. No background task, no LLM call |
| Before inserting a `planned` row | Re-checked, because it may have flipped during the planner call. Nothing is recorded |
| The job fires | `cancelled:disabled`. No guardrails, no judge, no run |
| After the judge, just before the run | Re-checked → `cancelled:disabled` |
| Inside the `notify` tool during a check-in run | The tool refuses; nothing is sent |
| **An admin turns the setting off** | Hook the settings update: **remove every pending `checkin:*` job for the org right away** and mark those rows `cancelled:disabled`. No dormant jobs are left behind |
| Frontend | Past check-in turns and traces stay visible as history. Nothing new happens |

## 5. Data model (Phase 1)

New table `agent_checkins`. The model goes in `backend/app/models/agent_checkin.py`
with an Alembic migration in `backend/alembic/versions/`. It must work on both
SQLite and Postgres.

| Column | Type | Notes |
|---|---|---|
| `id` | str(36) pk | |
| `organization_id` | FK organizations, indexed | |
| `user_id` | FK users, indexed | The human the follow-up is for |
| `report_id` | FK reports, indexed | The follow-up runs in this same report |
| `source_completion_id` | FK completions, indexed | The turn that planned it; the TraceModal anchors on this |
| `note` | text | The planner's free-text note to its future self: what to check, why, and what would make it worth notifying |
| `plan_reason` | text | One line explaining why a follow-up is warranted at all |
| `due_at` | datetime (UTC) | After clamping and the working-hours shift |
| `job_id` | str | `checkin:<id>` |
| `status` | str(24), indexed | `not_proposed`, `rejected`, `planned`, `cancelled`, `skipped`, `running`, `ran_quiet`, `sent`, `failed` |
| `status_reason` | str | Machine-readable code (§7) |
| `judge_decision` | str, nullable | `run` or `skip` |
| `judge_reason` | text, nullable | **Always filled when the judge ran**, for both run and skip |
| `judge_focus` | text, nullable | When the decision is run: what the run should concentrate on |
| `judged_at` | datetime, nullable | |
| `run_completion_id` | FK completions, nullable | The check-in turn's system reply |
| `notified` | bool, default false | True if the run successfully called `notify` |
| `notify_subject` | str(280), nullable | Copied from the `notify` call |
| `sent_at` | datetime, nullable | |
| `created_at` / `updated_at` | | From `BaseSchema` |

Notes:

- `not_proposed` rows are kept so that a planner "no" is also visible in the
  trace. They cost one row per eligible turn. If volume becomes a problem, trim
  them with a retention job, the way `step_retention_days` works.
- LLM usage for the planner and the judge is recorded through the existing
  usage pipeline, with distinct scopes (`checkin_planner`, `checkin_judge`). This
  lets the trace show their token counts and cost.
- When a report is deleted, cancel its pending jobs (mirror
  `WaitService.cancel_waits_for_report`) → `cancelled:report_deleted`.

## 6. Planning (Phase 2)

**Where:** the `agent_v2.py` post-analysis block, after the harness `try`
(around line 6818), in the non-training branch. It is dispatched as a background
task with its own session, so it never delays the user's turn, never emits SSE
and never writes blocks.

**Eligibility.** This is code with no LLM call, and every item must hold:

1. `enable_agent_checkins` is on.
2. The mode is not training, and `completion_errored` is false.
3. The head completion is **human-initiated**: `trigger_source IS NULL` and
   `webhook_id IS NULL`. This is the guard that stops check-ins spawning
   check-ins, and stops wait wakes, scheduled runs, evals and webhooks from
   planning.
4. The limits pre-check (§7) passes. This avoids paying for an LLM call whose
   result we would reject.

**The planner** lives in `backend/app/ai/agents/checkins/planner.py`. It is one
structured call to the small model.

Input, kept under about 6k tokens:
- the report title
- the last 3 or fewer user prompts
- the final answer text, truncated
- the titles and last-run times of the report's data steps
- any known refresh cadence (scheduled prompt or custom query) for that data
- today's date and the org timezone
- the user's display name

Output, validated with pydantic. Anything invalid means no check-in.

```json
{
  "propose": true,
  "due_in_hours": 72,
  "note": "They're waiting on the Oct 1 subscription refresh to see if churn tops 4%. Re-run churn by plan; worth notifying only if total churn crossed 4% or Enterprise churn moved noticeably.",
  "reason": "User said they'll decide on the retention campaign after the refresh."
}
```

The planner prompt should state these rules explicitly:
- The default is `propose=false`. Most conversations need no follow-up.
- Propose only when **something will change with time** that matters to what the
  user was doing. Examples: a stated future event, a data refresh, a threshold
  they cared about, a question blocked on data that doesn't exist yet.
- Don't propose for one-off lookups, questions that were fully answered, idle
  curiosity, or things the user can trivially re-run themselves.
- The `note` must be self-contained, because a future run reads it cold. It must
  say what to check, how, and **what result would be worth notifying about**.
- `due_in_hours` must follow from the note. For example, if the note is about the
  next refresh, set the due time just after that refresh.

**Persist:**
1. Clamp `due_at` to between now+2h and now+14d.
2. Shift it into the working window (§7).
3. Re-check the setting and the limits.
4. Insert the row as `planned` and arm the job.
5. Log `checkin_id, report_id, due_at`.

## 7. Guardrails and limits (Phase 1)

These live in `backend/app/services/checkin_policy.py`. They are code only and
contain **facts, no judgments**. Thresholds come from org settings or module
constants.

| Rule | Value | Checked at |
|---|---|---|
| Feature on | `enable_agent_checkins` | every step (§4) |
| Pending per user | at most 1 `planned` | plan |
| Pending per report | at most 1 `planned` | plan |
| Runs per user per 7 days | at most `checkins_max_per_user_per_week` (counts `ran_quiet` + `sent`) | plan, fire |
| Runs per org per day | at most `checkins_max_runs_per_org_per_day` | fire |
| Due window | clamp to between +2h and +14d | plan |
| Working window | Mon–Fri 09:00–18:00 in the **org timezone**. Outside it, move to the next window start plus 0–90 min of random jitter | plan, fire |
| Report exists and access holds | The user is still an active member and can read the report and its data sources. Reuse the access check the completion route uses | fire |
| No live run in this report | If an agent run is in progress in this report right now, re-arm for +30 min. This only avoids two runs colliding in one report | fire |

There is deliberately **no** "user already came back to the report" rule. Whether
the follow-up still matters is the judge's call.

Reason codes stored in `status_reason`: `disabled`, `pending_exists_user`,
`pending_exists_report`, `weekly_cap`, `org_daily_cap`, `report_deleted`,
`access_lost`, `judge_skip`, `run_failed`.

## 8. Scheduling (Phase 2)

Create `backend/app/services/checkin_service.py`, modelled on `wait_service.py`:

- `arm(checkin)` calls `scheduler.add_job(func=run_checkin_wake, trigger="date",
  run_date=due_at, id=f"checkin:{id}", kwargs={"checkin_id": id},
  replace_existing=True, misfire_grace_time=6*3600)`.
- `cancel(checkin_id, reason)`, `cancel_for_report(report_id)` and
  `cancel_all_for_org(org_id)`. The last one is used by the settings-off hook.
- `run_checkin_wake(checkin_id)` must be a **module-level** async function. The
  job store serializes callables by import path; `wait-tool.md` explains why this
  matters. Its first action is
  `if not await asyncio.to_thread(claim_scheduled_run, job_id): return`.
- Job kwargs carry only the id. Everything else is re-read from the DB at fire
  time, so setting and permission changes are respected.

## 9. The judge (Phase 3)

The judge lives in `backend/app/ai/agents/checkins/judge.py`. It is one
structured call to the small model. It is deliberately **loose**: it reads free
text and weighs it, and it is not a rule engine.

**Input.** Plain text, gathered cheaply, with no queries against customer data:
- the `note` and `plan_reason`
- the report title, and an excerpt of the last answer in the report
- how long ago the planning turn was, and when `due_at` was
- what happened in the report since then: new turns by the user, and their
  prompts, truncated
- when the underlying data, queries or scheduled prompts last refreshed, where
  known. Where unknown, say "unknown"; the judge weighs this rather than treating
  it as a no
- this user's last few check-ins in this org: judge decision, whether it
  notified, and whether they opened it

**Output:**
```json
{
  "decision": "run",
  "reason": "The note waits on the Oct 1 subscription refresh; the refresh job last ran Oct 1 08:12, after the conversation, so the churn question is now answerable.",
  "focus": "Re-run churn by plan for September vs August; compare total and Enterprise churn to 3.8% / previous values."
}
```

Rules:
- A `reason` is **required for both run and skip**. It must name the concrete
  facts that led to the decision. It is stored in `judge_reason` and shown in the
  TraceModal.
- Choose `run` when the note's question is now plausibly answerable or changed.
  Examples: data refreshed, the awaited date passed, the user's recent activity
  shows it still matters.
- Choose `skip` in these cases:
  - The user has already clearly resolved the question in the report since then.
  - Nothing that the note depends on can have changed.
  - The user's recent check-in history shows this kind of follow-up gets ignored.
- When the judge is torn, it may choose `run`. Noise is still bounded, because the
  run itself notifies only on explicit criteria.

## 10. The check-in run (Phase 3)

Set the row to `running`, then call:

```python
run_machine_turn(
    session, report=report, user=user, organization=org,
    trigger_source="checkin",
    message_type="checkin_event",          # wait uses "wait_resume_event"
    summary="Follow-up",                   # fallback text; meta drives the localized label
    meta={"checkin_id": checkin.id},
    instruction=render_checkin_prompt(checkin),
)
```

- The run executes **as the user**, with the full normal tool set. It may create
  queries, charts and notes, which product has accepted.
- After the run, store `run_completion_id`, which is the latest `role='system'`
  completion in the report.
- The planning eligibility rule "human-initiated only" (§6.3) stops this turn from
  planning another check-in.

**Trigger prompt** (`backend/app/ai/agents/checkins/prompts.py`). This prompt is
what the agent reads as its instruction:

> You are following up, on your own initiative, on this conversation with
> {user_name}. When it ended you left yourself this note:
> "{note}"
> Why a follow-up was warranted: {plan_reason}
> Why it is being run now: {judge_reason}
> Focus: {judge_focus}
>
> Re-run or re-inspect only what you need to answer the note. Compare against the
> most recent result already shown in this report; the user has seen that.
>
> **Call the `notify` tool only if at least one of these is true:**
> 1. **Threshold met.** The condition the note cares about is now true. For
>    example, a number crossed the value the user cared about, or a status became
>    what they were waiting for.
> 2. **Blocked question now answerable.** The note was waiting on data that did
>    not exist yet (a refresh, month-end, a file). It now exists, and you answered
>    the question.
> 3. **Material change.** A number the user focused on changed direction, or
>    moved by at least 10% relative to what this report last showed, **and** the
>    change affects the conclusion they reached.
>
> **Do not call `notify`** in any of these cases:
> - The data has not been refreshed, or the numbers are unchanged.
> - There is a change, but it is below the bars above.
> - Your queries failed, returned empty results, or the data is still missing.
>   Never notify about your own errors.
> - The user already saw this finding in this report.
> - The only interesting thing is unrelated to the note.
>
> If you are unsure, do not notify.
>
> If you do notify:
> - Leave `recipients` empty (it goes to this user only).
> - Use `subject` as a one-line headline, 120 characters or fewer.
> - In `body`, say what changed, give the one number that matters with its
>   previous value, and suggest one next step.
>
> Whether or not you notify, end with a short reply in this report: what you
> checked and what you found, in 5 lines or fewer. Include at most one chart, and
> only if it makes a change obvious.
>
> Do not create scheduled tasks.

The 10% threshold starts as a module constant. Promote it to an org setting only
if the live-stack loop shows it needs tuning per org.

### Guardrails inside the `notify` tool during a check-in run

The tool detects a check-in run from the runtime context: the head completion's
`trigger_source == "checkin"`. In that case:

- **Recipients are self only.** A non-empty `recipients` list returns an error to
  the agent. A check-in can never message coworkers.
- **At most one call per run.** A second call returns an error.
- **The setting is checked.** If `enable_agent_checkins` is off, the tool refuses.
- **Source label.** Delivery uses `source=SOURCE_CHECKIN`. Add
  `SOURCE_CHECKIN = "checkin"` to `models/notification.py` and to `SOURCES`, and
  make the inbox render it.
- Optionally, hide `create_scheduled_task` from the tool catalog for check-in runs.

### Outcome

- If the run contains a successful `notify` tool execution, set `sent`,
  `notified=true`, `notify_subject`, and `sent_at`.
- If the run completed without calling `notify`, set `ran_quiet`. The frontend
  collapses the reply under the strip.
- If the run raised an error, set `failed:run_failed`. `run_machine_turn` already
  marks the strip with ❌. No notification is sent.
- Both `sent` and `ran_quiet` count toward the per-user weekly limit and the
  org's daily limit.

## 11. TraceModal: every check-in step is visible (Phase 3)

Admins and debuggers must be able to see **every** decision, including the ones
that left nothing visible to the user.

**Backend.** Extend `ConversationTraceResponse`
(`backend/app/schemas/agent_execution_trace_schema.py:88`) and its builder in
`console_service.py:2078-2356`:

```python
class CheckinTraceSchema(BaseModel):
    id: str
    status: str
    status_reason: Optional[str]
    source_completion_id: Optional[str]
    run_completion_id: Optional[str]
    note: Optional[str]
    plan_reason: Optional[str]
    due_at: OptionalUTCDatetime
    judge_decision: Optional[str]
    judge_reason: Optional[str]
    judge_focus: Optional[str]
    judged_at: OptionalUTCDatetime
    notified: bool = False
    notify_subject: Optional[str]
    sent_at: OptionalUTCDatetime
    planner_llm_tokens: Optional[int]
    planner_llm_cost_usd: Optional[float]
    judge_llm_tokens: Optional[int]
    judge_llm_cost_usd: Optional[float]
    created_at: OptionalUTCDatetime

class ConversationTraceResponse(BaseModel):
    ...
    checkins: List[CheckinTraceSchema] = []
```

- Include every row for the report, including `not_proposed`, `rejected`,
  `cancelled` and `skipped`. The endpoint is already admin/console-scoped. Confirm
  its permission check, and add a non-admin test.
- Mark the check-in turn in `turns[]` as a machine turn with
  `trigger_source="checkin"` and link it to its row, so the per-turn pane can show
  the judge's reason next to the run.

**Frontend** (`frontend/components/console/TraceModal.vue`):
- In the conversation rail, under the turn that planned it
  (`source_completion_id`), show a compact **check-in card** with:
  - a status chip and due time
  - the planner **note** and reason
  - the **judge decision and reason**, for both run and skip
  - the outcome: notified with its subject, quiet, cancelled with its reason, or
    failed
  - planner and judge token counts and cost
- For a planner `not_proposed` row, show a single muted line ("No follow-up
  planned"), expandable to show the planner's reason.
- The check-in run turn itself renders like any machine turn. Its detail pane
  shows the judge's reason and focus at the top, then the usual execution trace.
  The `notify` call appears in that trace as a normal tool execution.
- Add i18n keys for every label in all `locales/*.json`, and check the layout in
  RTL (`he`).

## 12. Report UI (Phase 3)

- In `frontend/pages/reports/[id]/index.vue` (lines `:177` and `:1951-2009`):
  - Add a "Follow-up" label and icon for `trigger_source === 'checkin'`.
  - When the linked check-in is `ran_quiet`, collapse the reply under the strip
    ("Checked back: nothing new"), expandable.
  - Expose `ran_quiet` either as a field on completions v2 or through the strip's
    `meta`. Pick one.
- In the inbox, render rows with `source='checkin'`.
- In AI settings, the three settings render generically; verify this.
- Any UI change needs before/after evidence captured with the **ui-evidence**
  skill.

## 13. Phases and exit criteria

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Settings and locales; `agent_checkins` model and migration; `CheckinPolicy`; the settings-off hook that cancels pending jobs | Unit tests for every policy rule pass on sqlite and postgres. The settings render in AI settings. Turning the setting off removes pending jobs |
| 2 | Planner; `CheckinService.arm/cancel`; post-analysis dispatch | With the setting on and a stubbed planner, a qualifying turn creates exactly one `planned` row and one `checkin:<id>` job, and the report timeline is unchanged. With the setting off, a machine turn or training mode: zero rows and zero LLM calls |
| 3 | Wake; guardrails; judge; machine turn; `notify` guardrails; outcome; TraceModal; report strip; inbox source | All Loop A scenarios pass, and Loop B shows a real follow-up end to end, including the trace |
| 4 | Per-user opt-out (`Membership.checkins_opt_out`, a toggle on the profile, respected only while the org setting is on); tuning of limits and prompts based on Loop B metrics | The opt-out is respected at plan and fire time |

## 14. Feedback loop (to run in the new session)

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md`. Environment:

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

### Loop A: deterministic (no real LLM, no real clock)

Stub only at the boundaries:
- the LLM provider, for the planner, the judge and the agent run
- the clock, using the repo's clock utilities
- the scheduler, with the same stub pattern as `tests/unit/test_wait_tool.py`

Seed data through `tests/fixtures/*`. Each test must first be watched failing
(stash the implementation), per rule 6 in `backend/tests/AGENTS.md`.

Unit tests (`backend/tests/unit/test_checkin_policy.py`, `test_checkin_service.py`):
1. Each limit allows exactly at its boundary and denies one past it. Vary the
   values rather than encoding a single magic number.
2. Working-window shift:
   - Friday 17:59 is allowed.
   - Friday 18:01 moves to next Monday between 09:00 and 10:30, in the org
     timezone.
   - Include a DST boundary case.
3. `arm` registers a `date` job with id `checkin:<id>`. `cancel` and
   `cancel_all_for_org` remove it. `run_checkin_wake` is module-level and
   round-trips through `obj_to_ref`/`ref_to_obj`.
4. Planner and judge output validation:
   - Malformed JSON, or a missing `note`, produces no row.
   - A judge answer without a `reason` counts as `skip`, with reason
     `invalid_judge_output`.

E2E tests (`backend/tests/e2e/test_agent_checkins.py`, run with both
`--db=sqlite` and `--db=postgres`):

5. **Setting off.** A completed chat turn creates 0 rows and makes 0 planner
   calls.
6. **Setting on, planner proposes.** Exactly 1 `planned` row and 1 job. The report
   timeline (completions v2) is **unchanged**.
7. **Setting on, planner declines.** A `not_proposed` row, no job, and the
   timeline is unchanged.
8. **Machine turns never plan.** A turn with `trigger_source` set (a wait wake or
   a check-in run) creates 0 rows and makes 0 planner calls.
9. **Setting turned off while a check-in is pending.** The job is removed
   immediately and the row becomes `cancelled:disabled`. A later fire does
   nothing.
10. **Judge says skip.** The row becomes `skipped`, `judge_reason` is filled, the
    timeline is unchanged, and no notification is sent.
11. **Judge says run, and the agent calls `notify`.** The same report gets one
    strip and a reply. The status is `sent`, `judge_reason` is filled, and the
    user has exactly one inbox notification with `source='checkin'`.
12. **Judge says run, and the agent does not call `notify`.** The status is
    `ran_quiet`, and there are zero notifications.
13. **`notify` guardrails during a check-in run:**
    - Non-empty recipients are rejected.
    - A second call is rejected.
    - The call is refused if the setting was flipped off mid-run.
14. **Limits.** A third run in 7 days is rejected at plan time with `weekly_cap`.
    The org daily cap cancels at fire time.
15. **Permissions, non-admin path.**
    - A user who loses access to the data source before the fire gets
      `cancelled:access_lost`, and nothing runs.
    - The TraceModal conversation endpoint with check-ins is not readable by a
      non-admin.
16. **Idempotent fire.** Invoking the wake twice for the same job runs the turn
    only once.
17. **Trace.** For each of the scenarios above, `ConversationTraceResponse.checkins`
    contains the row with the expected status, note and judge reason.

### Loop B: live (real LLM, real stack)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
```

1. Turn on `enable_agent_checkins` in `/settings/ai_settings`. Take a screenshot.
2. In a report on seeded demo data, have a conversation that clearly warrants a
   follow-up, e.g. "the data refreshes tomorrow; I'll want to know if revenue
   drops below X". Then confirm:
   - The DB has one `planned` row with a sensible `note` and `due_at`.
   - The report UI shows **nothing** new.
   - The TraceModal shows the check-in card.
3. Run 5–10 varied sessions, including one-off lookups, and record the propose
   rate. The target is well under half.
4. Force a job due through a debug path, or by setting `due_at` to now and
   re-arming. Don't wait in real time. Observe each step: guardrails, then the
   judge's decision with its reason in the TraceModal, then the run, then either
   `notify` plus an inbox row or a quiet collapse. Take before/after screenshots
   (ui-evidence skill).
5. Repeat with the data unchanged. Expect either `skipped` with a reason, or
   `ran_quiet` with no notification.
6. Turn the setting off while a check-in is pending. Confirm the job is gone.
7. Record in the loop doc:
   - the final planner, judge and trigger prompts
   - the metrics below
   - token cost per stage
   - every false positive or false negative, with its trace

### Metrics for the loop doc

| Metric | Formula | Target |
|---|---|---|
| Plan rate | planned / eligible turns | Low |
| Judge run rate | run / judged | Moderate |
| Quiet rate | ran_quiet / runs | High means the judge is too loose |
| Notify rate | sent / runs | |
| Open rate | opened / sent | High |
| Cost per stage | planner, judge, run | |

"Opened" means `ReportView.last_viewed_at > sent_at`.

## 15. Risks and decisions to confirm

- **Side effects outside the turn.** The run may edit notes or dashboards. This is
  accepted for v1.
- **Default state.** Off per org while in lab. Revisit after the Loop B metrics.
- **The judge is loose on purpose.** It can choose `run` when uncertain, because
  the explicit `notify` criteria in the run are the last line of defence against
  noise. If the quiet rate is high, tighten the judge prompt before adding code
  rules.
- **Recipient on shared reports.** v1 sends only to the human who drove the
  planning turn.
- **Channel.** The in-app inbox always, plus one external nudge, following the
  existing `NotifyService` preference order: Teams, then Slack, then Google Chat,
  then email.
