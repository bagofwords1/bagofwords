# Overnight learning: the agent dream and the user dream

Status: **implemented** on `claude/overnight-learning` (which also carries
check-ins, PR #1200, and user memory, PR #1201). The loop report is
`docs/feedback-loops/overnight-learning.md`. The plan below is kept as
written; where the build differs, §0 says how and why.

## 0. As built: differences from the plan

| Plan | As built | Why |
|---|---|---|
| Each dream is a PlannerV3 sub-loop with its own tools (§3.3, §4.3, §5.2) | Each dream is **one structured small-model call** (`app/ai/agents/dreams/agent_prompts.py`, `user_prompts.py`). The model proposes; code validates and writes | Every write already had to be re-checked in code (gates, ownership, memory rules, limits). A single call is cheaper, deterministic to test, and cannot take side effects the validator did not see. The sub-loop extraction was not needed |
| `AI_DRAFT_EXPIRY_DAYS = 30` constant | Org setting **`ai_suggestion_expiry_days`** (default 30, `0` = never, clamped to 0–3650), shown under *Agent overnight learning* in AI settings | Requested: stale-suggestion expiry is an org decision |
| Merged / expired drafts get a distinct status | Closed with status `rejected`, `approved_by = NULL`, **no per-hunk verdicts**, and a `[merged]` / `[expired]` marker in `rejection_reason` | `build_verdict` only reads per-hunk verdicts, so these never count as human rejections, with no schema change to builds. Only `pending_approval` AI builds are ever closed; human and training drafts are untouched. A partly-absorbed build stays open |
| Pre-cluster in code, then the model consolidates | The model groups drafts it sees as saying the same thing; **code recomputes the gate** (≥3 distinct users, ≥2 distinct org-local days, expired drafts included as evidence) and holds any group below it | Lexical pre-clustering missed paraphrases; the gate in code keeps the model from promoting a one-off |
| — | Fixed `AgentReliabilityService._resolve_suggestion_agents`: counts only changed rows (`is_change`) and removals, and detects a global instruction per instruction | Before, a build with several instructions on one agent (or any unchanged global row copied from main) looked like it affected every agent, so the Self-Learning hand-off ran evals on the wrong agents |
| User dream writes memory, open threads, up to 2 check-ins and a habit offer; results in a "Since you were here" briefing card; per-user "Prepare things for me overnight" toggle and log (§2.3, §5.3–5.6) | **The user dream writes only memory and planned check-ins.** No threads, habit offers, briefing, per-user overnight toggle or overnight log; no `user_open_threads` / `habit_offers` tables, no `overnight_prep` / `briefing_seen_at` / `briefing_feedback` columns, no `/users/me/briefing` or `/users/me/overnight` routes. Unfinished work with a date or blocker becomes a planned check-in; without one it isn't tracked. Users control it through what they already have: their memory (edit / delete) and the check-in opt-out | Four output types with two new concepts made the feature hard to explain and to trust. Memory and check-ins already have UI, limits, privacy rules and opt-outs; reusing them keeps the user side to one prompt and no new UI. A visible morning moment can come back later as a view over check-ins |
| Two org master switches, `enable_agent_dreaming` and `enable_user_dreaming` (§2.1) | **One switch, `enable_agent_dreaming`** (default on). The user dream has no setting of its own: it runs whenever `enable_user_memory` is on, and its planned check-ins follow `enable_agent_checkins` and each user's opt-out | The user dream only maintains memory and plans check-ins, which already have their own switches. A third toggle added settings without adding control |
| Review feed filters for merged / expired | Closed builds leave the pending list; the build explorer shows a **Merged** / **Expired** badge (with a tooltip saying it is not a reviewer's rejection) instead of *Rejected*. No extra filter | Scope |

## 1. What we are building and why

The goal is a product that visibly gets better and gets ahead of people
overnight. There are two independent nightly processes, each with a clear owner:

| | **Agent dream** (Part A) | **User dream** (Part B) |
|---|---|---|
| Unit | One per agent (data source) that had activity | One per user who was active that day |
| Reads | Pending AI instruction drafts, instruction feedback, and usage for that agent | That user's sessions (summaries plus their own messages), memory, upcoming events, check-ins, recurring asks |
| Writes | **One consolidated suggestion build** for the agent. Nothing is applied directly | Memory updates, open threads, planned check-ins, habit offers |
| Trust gate | The agent's existing **Self-Learning policy** (off / auto-approve / eval-review / eval-auto) via `run_for_suggestion` | `MemoryService` rules; `CheckinPolicy` plus the judge for anything that later runs; nothing touches customer data overnight |
| Moment the user notices | The agent's managers see: "Sales agent learned 3 things from 41 conversations last night · evals 18/18 ✓" | The user sees: "Since you were here: board pack refreshed for Thursday · October data landed · want your Monday pipeline ready at 8:30?" |

The magical moments:
1. The agent's answers get better overnight, backed by evidence and evals.
2. The review backlog shrinks. Admins see one nightly item per agent instead of
   dozens of near-duplicate drafts.
3. Things are ready before the user asks (check-ins planned from events and open
   threads).
4. The user picks up where they left off (open threads in the session-start
   briefing).
5. The agent notices their habits (repeated asks become a one-click offer).

**Principles:**
- **Think overnight, act only through existing gates.**
  - Dreams never query customer data and never message anyone.
  - Anything that touches data runs as a check-in (limits, judge, `notify`
    criteria).
  - Anything that changes instructions goes through the agent's Self-Learning
    policy.
- **Default to doing nothing.** Every output must cite evidence: reports,
  drafts, feedback or memory entries.
- **Dreams never learn from their own output.** Machine turns (check-ins, waits,
  scheduled runs, evals, webhooks) are never used as input.

## 2. Settings

There are four new controls. Each gates exactly one thing, and each sits next to
existing settings for the same area.

### 2.1 Org master switches

Add to `OrganizationSettingsConfig`
(`backend/app/schemas/organization_settings_schema.py`):

```python
enable_agent_dreaming: FeatureConfig = FeatureConfig(
    value=True, name="Agent overnight learning",
    description="Each night, agents review their pending suggestions, feedback and "
                "usage and propose one consolidated improvement per agent. What "
                "happens to it follows each agent's Self-Learning setting.",
    is_lab=True, editable=True)
enable_user_dreaming: FeatureConfig = FeatureConfig(
    value=True, name="Overnight preparation for users",
    description="Each night, the agent reflects on each active user's work to keep "
                "their memory tidy, remember open threads, and prepare follow-ups "
                "(e.g. before a meeting). Users can turn it off for themselves.",
    is_lab=True, editable=True)
```

- These are **ceilings**. When off, nothing runs, whatever the per-agent or
  per-user values are.
- Add names and descriptions to **every** `locales/*.json` catalog (identical
  shape; Hebrew vocabulary rules in `AGENTS.md`).

### 2.2 Per agent: a field in the existing Self-Learning policy

- Add `nightly_learning: bool = True` to `AgentAutomationPolicy`
  (`backend/app/schemas/agent_automation_schema.py`).
- It resolves like the other fields: the org defaults
  (`agent_automation_defaults`) merged with the per-agent override
  (`data_source.automation_settings`), via `resolve_policy`
  (`backend/app/services/agent_reliability_service.py:92`).
- It is shown in `frontend/components/AgentAutomationSettings.vue` as one toggle,
  "Learn overnight", with a hint:
  > Consolidates pending suggestions and feedback nightly. What happens to the
  > result follows the mode above.
- The toggle is visible only when `enable_agent_dreaming` is on. Otherwise it is
  shown disabled with a hint that the org has turned the feature off.
- The existing `mode` still decides the outcome:
  - `off`: the build waits in Review. Consolidation is still useful here.
  - `auto_approve`: the build is promoted.
  - `eval_review` / `eval_auto`: the build is evaluated first.

### 2.3 Per user: one profile toggle

- Add `Membership.overnight_prep` (boolean, default true) with a migration.
- It appears in `UserProfileModal.vue` as "Prepare things for me overnight", with
  a short explanation and a link to the overnight log (§5.6).
- It is hidden when `enable_user_dreaming` is off.
- The existing `checkins_opt_out` (#1200) still applies separately. A user who
  opted out of check-ins still gets memory tidying, open threads and the
  briefing, but no planned check-ins.

### 2.4 How the switches combine

| Dream or capability | Runs only if |
|---|---|
| Agent dream for agent X | `enable_agent_dreaming` **and** X's resolved `nightly_learning` |
| User dream for user U | `enable_user_dreaming` **and** `enable_user_memory` **and** `U.overnight_prep` |
| The user dream's `plan_checkin` tool | additionally `enable_agent_checkins` **and not** `U.checkins_opt_out`. Otherwise the tool is removed from the catalog |
| Session-start briefing | Shows whatever items exist, with no separate switch. With nothing to show, it doesn't render |

Every switch is checked when the work is **queued** and again right before any
**write**. Turning a master switch off cancels queued, not-yet-started dream runs
for the org (they end `cancelled:disabled`).

**Code constants, not settings:**
- the night window (01:00–05:00 org local time)
- concurrency: 4 runs globally, 2 per org
- nightly token budget per org: `DREAM_ORG_NIGHTLY_TOKENS = 2_000_000`
- step limits
- draft expiry: `AI_DRAFT_EXPIRY_DAYS = 30`
- promotion gates

## 3. Shared runtime (Phase 1)

### 3.1 The sweep

Register a leader-only job `overnight_sweep` in `backend/main.py`, next to the
other `is_scheduler_leader` jobs. It runs **hourly** as an interval job.

On each tick, for each org whose local time (using the existing org timezone
helper) is inside the night window:

| Local time | Work queued |
|---|---|
| 01:00–03:00 | **User dreams**, if `enable_user_dreaming` is on. Due users are memberships with `overnight_prep` on, **human-initiated** completions (`trigger_source IS NULL AND webhook_id IS NULL`) since `user_dreamed_at`, **or** a memory `events` entry in the next 3 days |
| 03:00–05:00 | **Agent dreams**, if `enable_agent_dreaming` is on. Due agents have `nightly_learning` resolved true **and** at least one of: new AI drafts touching the agent, instruction feedback events on its instructions, or AI drafts older than `AI_DRAFT_EXPIRY_DAYS` (so expiry can run), since `agent_dreamed_at` |

- Work runs in-process with bounded concurrency: an `asyncio.Semaphore(4)`
  globally and 2 per org.
- Each unit is claimed with
  `claim_scheduled_run(f"dream:{kind}:{target_id}:{local_date}")`
  (`backend/app/core/scheduler.py:109`), so it runs exactly once across workers
  and replicas.
- Anything not finished by 05:00 local waits for the next night. The watermark
  isn't advanced for it.

### 3.2 Run log and watermarks

New table `dream_runs` (model `backend/app/models/dream_run.py`, Alembic
migration, SQLite and Postgres):

| Column | Notes |
|---|---|
| `id`, `organization_id` | |
| `kind` | `agent` or `user` |
| `data_source_id` / `user_id` | Exactly one is set, depending on `kind` |
| `local_date` | The org-local night |
| `status` | `queued`, `running`, `done`, `skipped`, `cancelled`, `failed` |
| `status_reason` | e.g. `nothing_new`, `disabled`, `budget`, `window_closed`, `error:<type>` |
| `inputs_summary` | JSON of counts only, e.g. `{drafts: 14, feedback: 5, sessions: 3}` |
| `tool_calls` | JSON: name, arguments and result per call. **User-dream entries are owner-only** (§7) |
| `outputs` | JSON: build id, memory ops, threads, check-ins, habit offers |
| `tokens`, `cost_usd`, `started_at`, `finished_at` | |

Watermarks:
- `Membership.user_dreamed_at`: a new column.
- `DataSource.agent_dreamed_at`: a new column. (A small JSON field on
  `automation_settings` would also work, but a column is simpler to query.)
- A watermark advances **only** when its run ends `done` or `skipped`.

**Crash safety:**
- A run is wrapped end to end. Any exception ends it `failed:error:<type>` with
  the watermark unchanged, so the next night retries.
- On each tick, runs left in `running` for more than 60 minutes are marked
  `failed:stale`.

**Budget:** before starting a run, sum `tokens` of today's runs for the org. If
it is at or above `DREAM_ORG_NIGHTLY_TOKENS`, end the run `skipped:budget`.
Record LLM usage with the scopes `dream_agent` and `dream_user`.

### 3.3 Sub-loop runner

Both dreams use the **PlannerV3 sub-loop pattern** that `_run_knowledge_harness`
uses (`backend/app/ai/agent_v2.py:1741`):
- a mode-specific tool catalog (`registry.get_catalog_for_plan_type(..., mode=...)`)
- `self.small_model`
- a bounded step count
- no report, SSE or blocks

The new modes `dream_agent` and `dream_user` are registered through the tools'
`allowed_modes`. Extract the harness's loop into a reusable runner, e.g.
`backend/app/ai/agents/subloop.py`, so the harness and both dreams share it. That
refactor must keep the harness behavior unchanged; its existing tests guard it.

**Debug trigger** (Loop A and B, admin-only): `tools/agent/run_dream.py --org
<id> --kind agent|user --target <id>` runs one dream immediately, ignoring the
night window, and honours every setting.

## 4. Part A: the agent dream (Phases 2–3)

### 4.1 Gather (code, no LLM)

For agent X (data source `ds`), since `agent_dreamed_at`:

1. **Pending AI drafts:** instructions with `source_type='ai'` (or
   `ai_source` set) in builds whose `status IN ('draft','pending_approval')`,
   `source='ai'` and `is_main=false`, that reference `ds` (the instruction's data
   source associations or table references). For each draft, also load:
   - its `trigger_reason` and evidence from the build description
   - the user id and report of the session that created it
   - its created date
2. **Negative and positive feedback** (`instruction_feedback_event`) on
   instructions scoped to `ds`, with the linked completion feedback message.
3. **Usage** (`instruction_stats`, `instruction_usage_event`) for AI instructions
   scoped to `ds`: last used and counts.
4. **Expired-draft evidence:** drafts previously marked expired for `ds`, which
   still count toward the promotion gates (§4.4).

If there are no new drafts, no new feedback, nothing to expire and no unused AI
instructions to review, end `skipped:nothing_new` with no LLM call.

### 4.2 Pre-cluster (code)

Group drafts by table reference first, then by normalized text similarity. Reuse
`backend/app/services/suggestion_merge.py` (`covers`,
`superseded_by_containment`). For each cluster, compute:
- `distinct_users`
- `distinct_days`
- `sessions`
- contradicting feedback (negative events on a live instruction that says the
  same thing)

Clusters that already meet the gates are marked **promotable**. The others are
**held**.

### 4.3 Consolidate (sub-loop, mode `dream_agent`)

**Tools:** `search_instructions`, `read_instruction`, `create_instruction`,
`edit_instruction`, `describe_tables`, `inspect_data`. It can't use `create_data`,
reports or notify.
- `inspect_data` is limited to schema and sample checks, the same as in the
  harness. If product prefers zero data access overnight, remove it and keep only
  `describe_tables`. Decide in Phase 2.
- At most 10 steps.

**Input:**
- the agent's name and description
- the promotable clusters (member drafts, counts, evidence quotes)
- live instructions with repeated negative feedback, with their feedback
  messages
- AI instructions unused for 60 days or more
- the held-cluster summary, as counts only

**Prompt rules:**
- For each **promotable** cluster, write **one** clear, general instruction that
  captures what the drafts agree on. Before creating one, check whether a live
  instruction already covers it; if so, edit that instruction rather than adding a
  new one.
- For a live instruction with repeated negative feedback, propose an edit only
  if the feedback states what is wrong. Otherwise leave it alone.
- **Propose archiving** AI instructions unused for 60 days or more. **Never**
  archive or rewrite `source_type='user'` or `git` instructions. Suggesting an
  edit to them is allowed only through `edit_instruction`'s normal
  draft-to-review path.
- Every change needs evidence: cluster counts, feedback ids, usage numbers.
  Without evidence, make no change.
- Held clusters are **not** promoted.

**Output:** every `create_instruction` / `edit_instruction` call lands in **one
new suggestion build**, created through `BuildService.create_build(...,
source='ai')` and `add_to_build`:
- title: `Nightly · {agent name} · {local date}`
- description: a generated changelog, one line per change with its evidence
  ("Revenue = net excl. VAT — 6 users, 4 days, drafts #…")

If the sub-loop makes no changes, don't create a build (`done` with `outputs =
{changes: 0}`).

### 4.4 Close the backlog: merge and expire (code)

- **Merged:** every draft whose cluster fed a change is closed through the
  existing rejection path with `rejection_reason = "Merged into nightly build
  #{n}"`. Also store a machine marker (`merged_into_build_id` in the build's
  metadata, or a small link table) so the UI can show "merged" instead of
  "rejected". Merged drafts must **not** count as human rejections anywhere,
  including `rejected_hunks` and feedback statistics.
- **Expired:** AI drafts that are still pending, older than
  `AI_DRAFT_EXPIRY_DAYS` (30), and not part of any promoted cluster, are closed as
  **expired** (a distinct status or marker, not a human rejection).
  - Expired drafts **keep contributing evidence**: users, days and sessions count
    toward future clusters for 90 days, so a slow-building pattern can still be
    promoted.
  - Drafts authored or submitted by humans (`source='user'` builds) are never
    expired.
- The Review feed hides merged and expired drafts by default, with a filter to
  show them.

### 4.5 Hand-off to the Self-Learning policy

- Call `AgentReliabilityService.run_for_suggestion(db, org, build_id)`
  (`agent_reliability_service.py:869`).
- Add `TRIGGER_NIGHTLY = "nightly"` to `backend/app/models/agent_automation_run.py`
  and `TRIGGERS`, and make the resulting `AgentAutomationRun` rows record it.
  `run_for_suggestion` currently records `TRIGGER_SUGGESTION`; pass the trigger
  in.
- The nightly build touches **only instructions scoped to this agent**, so
  `_resolve_suggestion_agents` should resolve to exactly this agent.
  - Assert this in the consolidation step: reject tool calls that would create
    a global instruction, or one scoped to another data source.
  - Add a test for it.
- The policy's existing behavior decides what happens next:
  - review
  - auto-promote
  - evaluate on the candidate build, then promote if green or leave for review
  - on failure with `auto_fix_on_failure`, the existing fix loop

### 4.6 Tell the agent's managers

After the hand-off, call `InboxService.notify_agent_managers(db,
organization_id=…, data_source_id=…, type="nightly_learning", …)` **once**, and
only when `changes > 0` or drafts were merged or expired. Examples:

> **Sales agent learned 3 things from 41 conversations last night**
> Revenue = net excl. VAT (6 users) · Fiscal-year fix (5 thumbs-down) · 9 unused
> instructions proposed for archive · 14 pending drafts merged
> Evals 18/18 ✓ — promoted  *(or)*  Waiting for your review → *Open build*

- It links to the build review page.
- Add i18n keys to all locales.
- Add the `nightly_learning` type to the inbox renderer.

## 5. Part B: the user dream (Phases 4–5)

### 5.1 Gather (code, no LLM)

For user U, since `user_dreamed_at`:

1. **Sessions:** reports with **human-initiated** completions by U. For each
   report:
   - title
   - agents used
   - the rolling summary (`report_context_state.summary_json`)
   - U's own messages (the last 10, each at most 400 chars)
   - last activity time
2. **Memory** (#1201):
   - the rendered always tier and index line
   - entries the agent created since the watermark
   - `events` in the next 14 days
3. **Check-ins** (#1200):
   - U's pending check-ins
   - U's last 10 outcomes: `sent` and opened, `sent` and ignored, `ran_quiet`,
     `skipped`
4. **Recurring asks:** U's human prompts from the last 28 days, normalized
   (lowercased, numbers and dates stripped), grouped by similarity. Keep groups
   with 3 or more occurrences on distinct weeks. Attach weekday and hour
   statistics and the report ids.
5. **Existing open threads and habit offers**, so they can be updated or closed.

Cap the input at about 15k tokens, trimming the oldest sessions first. If there
are no new sessions and no events in the next 3 days, end
`skipped:nothing_new`.

### 5.2 Reflect (sub-loop, mode `dream_user`)

**Tools:**
- Existing:
  - `read_report(report_id)`, to open a session when its summary isn't enough
  - `search_reports`
- From #1201: `search_memory`, `create_memory`, `edit_memory`. These go through
  `MemoryService`, with dedupe, the cap, the definition refusal and the secrets
  filter. **They never modify `source='user'` entries.** `source='dream'` must be
  added to the memory `source` enum.
- New:
  - `plan_checkin(report_id, note, due_at)`: a thin wrapper over
    `CheckinService` that creates a `planned` check-in with
    `source_completion_id = NULL` and `origin='dream'`. **All `CheckinPolicy`
    rules apply** (pending limits, the weekly cap, the working window, the due
    clamp). It is present only if the check-in gates in §2.4 hold.
  - `set_open_threads(threads: [{report_id, text, unblocked_by?}])`: replaces
    U's open threads. At most 5.
  - `offer_habit(intent, cadence, suggested_time, report_id)`: creates a habit
    offer. At most one new offer per night, and none if an offer for the same
    intent was declined in the last 60 days.
- **Not available:** `create_data`, `inspect_data`, `describe_tables`, `notify`,
  `send_email`, instruction tools, `create_scheduled_task`.
- At most 8 steps.

**Prompt rules:**
- Think about what U is in the middle of and what's coming up. **The default is
  to do nothing.**
- **Memory:** save durable personal facts that **repeat across sessions**, update
  facts that newer sessions contradict, retire focus entries that are clearly
  over, and reuse tags. Personal only: no business definitions (those are
  instructions, see #1201 §2).
- **Open threads:** list what U left unfinished and what would unblock each,
  citing the report.
- **Check-ins:** at most **2**, and only for a concrete reason:
  - an upcoming memory event that a specific report prepares for (prep the day
    before, inside working hours), **or**
  - data U is waiting on that is due to land

  The `note` must say what to check and what result would be worth notifying
  about, exactly as the check-in planner requires. Don't plan something the user
  already asked to be scheduled.
- **Habits:** offer one only for a recurring ask with 3 or more occurrences on
  distinct weeks. Phrase it as an offer. Never create a schedule.
- Weigh U's check-in engagement: if they ignore a kind of follow-up, don't plan
  that kind.
- Every output must reference a report id or a memory handle.

### 5.3 New storage

`user_open_threads` (model + migration):

| Column | Notes |
|---|---|
| `id`, `organization_id`, `user_id` | |
| `report_id` | FK, indexed |
| `text` | at most 200 chars |
| `unblocked_by` | at most 200 chars, nullable |
| `dream_run_id` | |
| `status` | `open`, `resolved`, `dismissed` |
| `created_at`, `updated_at` | |

`habit_offers` (model + migration):

| Column | Notes |
|---|---|
| `id`, `organization_id`, `user_id` | |
| `intent_text` | |
| `cadence` | `weekly:mon`, `daily`, … |
| `suggested_time` | |
| `report_id` | |
| `status` | `offered`, `accepted`, `declined` |
| `scheduled_prompt_id` | nullable, set on accept |
| `decided_at` | |
| `dream_run_id` | |

A thread becomes `resolved` automatically when U has a new human turn in that
report after the thread was created, or when a check-in on that report ends
`sent`.

### 5.4 Planned check-ins

These use the #1200 pipeline unchanged from the point of firing:
- fire, then guardrails, then the judge (which always gives a reason), then
  `run_machine_turn(trigger_source="checkin")` in the report, as the user, then
  `notify` only on the explicit criteria
- the TraceModal check-in card shows `origin=dream` and the dream run id
- the `agent_checkins` schema gets `origin` (`turn` or `dream`) and a nullable
  `source_completion_id`

### 5.5 Session-start briefing (Phase 4, can ship before the user dream)

**API:** `GET /api/users/me/briefing?report_id=` (current user only) returns
items since `Membership.briefing_seen_at`:
- **Check-in results:** `sent` or `ran_quiet` since last seen, e.g. "Board pack
  refreshed — churn 4.2%", linked to the report.
- **Open threads:** at most 3, the most recent first (or those for `report_id`
  when on a report page).
- **Upcoming events:** memory `events` in the next 3 days, only when there's a
  related open thread or planned check-in.
- **Habit offers** with `status='offered'`: at most 1.

**Actions:**
- `POST …/briefing/seen`
- `POST …/briefing/items/{kind}/{id}/feedback` with `useful` or `not_useful`.
  "Not useful" dismisses the item, and U's check-in engagement records it.
- `POST …/habit_offers/{id}/accept`: creates a normal `ScheduledPrompt` through
  `ScheduledPromptService.create_scheduled_prompt` in the offer's report, with
  the suggested cron. After that it's visible and editable like any scheduled
  task.
- `POST …/habit_offers/{id}/decline`

**UI:**
- A compact "Since you were here" card on the home page, and a slimmer variant at
  the top of a report when that report has items.
- Each line shows its source icon, a one-line text, a "why" tooltip ("Because you
  mentioned the board meeting on Thursday", from the thread or check-in note),
  and "not useful" (×).
- Nothing is rendered when there are no items.
- Every string goes into all locales, with an RTL check (`he`).
- Before/after evidence is captured with the **ui-evidence** skill.

The briefing is deliberately **pull only**. It never sends notifications. Pushes
happen only through check-ins' `notify` criteria.

### 5.6 "What I did overnight" (owner-only log)

- The profile gets a section listing the last 14 nights of the user's dream
  runs, in plain language: "Updated 2 memories · noted 2 open threads · planned
  1 follow-up for Wed 07:45 (board pack)".
- Each line links to the affected entries, reports or check-ins.
- **Only the user sees it.** Admins see counts only (§7).

## 6. How it fits together (one night, one example)

```
01:30  user dream (Dana)      → memory: focus m9 updated
                              → threads: "Churn by plan — waiting for October data"
                              → plan_checkin(board-pack, Wed 07:45, "refresh Q3 pack; flag churn >4%")
                              → offer_habit("weekly pipeline by region", mon 08:30)
03:10  agent dream (Sales)    → 14 drafts → 3 clusters promotable → build "Nightly · Sales · Sep 29"
                              → 14 drafts merged, 4 expired → run_for_suggestion → evals 18/18 ✓ → promoted
                              → managers notified
07:45  check-in fires         → guardrails → judge: run ("meeting tomorrow, data refreshed 06:10")
                              → agent_v2 turn in board-pack report → churn 4.2% → notify Dana
08:30  Dana opens BOW         → "Since you were here" card: board pack · open churn thread · Monday pipeline offer
```

## 7. Privacy and safety

- **The user dream's inputs and outputs are private to the user.** In
  `dream_runs.tool_calls` and `outputs`, the text of user-dream content (memory
  text, thread text, check-in notes, prompts) is readable **only by that user**.
  The console and admins see kind, status, counts, tokens and cost.
- **Cross-dream isolation:** the agent dream reads only instruction drafts,
  feedback and usage, which are already visible to agent managers through
  Review. It never reads user memory, threads or user-dream outputs. The user
  dream never reads or writes instructions.
- **No customer data access overnight in the user dream.** The agent dream's
  `inspect_data` is subject to the Phase 2 decision (§4.3).
- **Dreams never act for other users.** The user dream only plans check-ins that
  deliver to U themselves (the #1200 `notify` guardrail: self only).
- **Redaction:** check that the public share path (`get_public_conversation`)
  and admin diagnosis never expose dream-originated tool arguments. This is the
  class of leak found in #1201.

## 8. Phases and exit criteria

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Settings and locales; `nightly_learning` policy field and UI toggle; `Membership.overnight_prep`; `dream_runs`, watermarks, sweep, claims, budget, stale sweep; extracted sub-loop runner (harness unchanged); `run_dream.py` debug trigger | The sweep queues the right targets in the night window only. Every switch combination behaves as in §2.4. Harness tests still pass |
| 2 | Agent dream: gather, pre-cluster, consolidate, one build, `TRIGGER_NIGHTLY`, hand-off to `run_for_suggestion` | With a stubbed LLM: a clustered set of drafts produces one build scoped to this agent only. It is promoted or pending according to each policy mode. User or git instructions are never archived |
| 3 | Merge/expire backlog; review-feed filters; manager notification | Merged drafts show as merged (not rejected) and don't count as human rejections. Expired drafts still count as evidence. One notification per agent per night |
| 4 | Session-start briefing (from check-in results and memory events); feedback actions | The briefing shows correct items since last seen, and "not useful" dismisses and records |
| 5 | User dream: gather, reflect, open threads, `plan_checkin`, habit offers and acceptance; "What I did overnight" | With a stubbed LLM: memory ops, threads, check-ins within limits, and one habit offer. It never touches user-authored entries, never reads machine turns, and privacy holds |

## 9. Feedback loop (to run in the new session)

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md`.

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

### Loop A: deterministic (stubbed LLM, stubbed clock)

Seed data through `tests/fixtures/*`. Each test must first be watched failing
(stash the implementation), per rule 6 in `backend/tests/AGENTS.md`. Run e2e on
both `--db=sqlite` and `--db=postgres`.

**Runtime** (`tests/unit/test_overnight_sweep.py`, `tests/e2e/test_dream_runtime.py`):
1. **Night window:** orgs in different timezones are queued only inside
   01:00–05:00 local. User dreams run in 01–03 and agent dreams in 03–05.
2. **Switch matrix** (§2.4):
   - each ceiling off: nothing queued
   - `nightly_learning` false for one agent: only that agent is skipped
   - `overnight_prep` false: that user is skipped
   - check-ins off, or the user opted out: the `plan_checkin` tool is absent
3. **Exactly once:** firing the same unit twice processes it once (the claim).
4. **Watermarks:** they advance on `done`/`skipped`, not on `failed`. A crash
   mid-run is retried the next night. Rows left `running` become
   `failed:stale`.
5. **Budget:** hitting the org nightly token cap skips the remaining units with
   `skipped:budget`.
6. **Harness regression:** the existing knowledge-harness tests pass unchanged
   after the sub-loop extraction.

**Agent dream** (`tests/unit/test_agent_dream_cluster.py`, `tests/e2e/test_agent_dream.py`):
7. **Gates:** a cluster with 3 users on 2 days is promoted. With 2 users, or on 1
   day, it is held. Contradicting feedback holds it. Vary the counts.
8. **One build per agent:**
   - All changes land in a single `source='ai'` build.
   - Instructions scoped to another data source, or global ones, are refused.
   - `_resolve_suggestion_agents` returns only this agent.
9. **Policy hand-off, per mode:**
   - `off`: pending
   - `auto_approve`: promoted
   - `eval_review` with evals green: promoted, or pending according to mode
     semantics
   - evals red: pending, and main is untouched
   - `AgentAutomationRun.trigger == "nightly"`
10. **Merge and expire:**
    - Merged drafts are closed as merged and excluded from rejection statistics.
    - AI drafts older than 30 days are expired. User-authored drafts are never
      expired.
    - Expired drafts still count toward a later cluster's user and day totals.
11. **Protection:** user and git instructions are never archived or rewritten by
    the dream.
12. **Notification:** exactly one inbox item per agent per night when there are
    changes, and none when there are no changes.
13. **Nothing new:** `skipped:nothing_new`, with zero LLM calls.

**User dream** (`tests/unit/test_user_dream_gather.py`, `tests/e2e/test_user_dream.py`):
14. **Inputs exclude machine turns:** check-in, wait, scheduled, eval and webhook
    turns never appear in the gathered sessions.
15. **Memory ops** go through `MemoryService`. A stubbed `edit_memory` on a
    `source='user'` entry is refused.
16. **`plan_checkin` respects `CheckinPolicy`:**
    - a third pending check-in, or a weekly-cap breach, is rejected
    - the due time is clamped into the working window
    - the row has `origin='dream'`
17. **Open threads:** at most 5, replaced each night, and auto-resolved on a new
    human turn in the report.
18. **Habit offers:**
    - at most 1 per night
    - suppressed for 60 days after a decline
    - accepting creates exactly one `ScheduledPrompt` in the report
19. **Briefing API:**
    - returns only the current user's items since `briefing_seen_at`
    - "not useful" dismisses the item
    - another member or an admin gets 403/404
20. **Privacy:**
    - an admin reading `dream_runs` for a user dream sees counts only
    - the public share and admin diagnosis payloads never contain dream tool
      arguments

### Loop B: live (real LLM, real stack)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
```

1. **Agent dream:**
   - Turn on `enable_agent_dreaming`, and set a seeded agent's Self-Learning mode
     to `eval_review` with a small eval suite.
   - As 3 different users across 2 simulated days (backdate
     `created_at`), trigger the knowledge harness with the same correction
     ("revenue should exclude VAT"), plus a few unrelated one-offs.
   - Run `tools/agent/run_dream.py --kind agent`. Confirm:
     - one nightly build with a correct consolidated instruction and an evidence
       changelog
     - the one-offs held
     - drafts merged
     - evals run, then promoted or pending
     - one manager notification
   - Screenshot the build and the inbox item.
2. **User dream:**
   - Turn on `enable_user_dreaming` and check-ins.
   - As a user: mention "board meeting next Thursday", work on a board-pack
     report, leave a churn question open, and ask the same "pipeline by region"
     question on 3 backdated Mondays.
   - Run `run_dream.py --kind user`. Confirm the memory updates, 2 open threads,
     1 planned check-in the day before the meeting within working hours, and 1
     habit offer.
   - Force the check-in due. Confirm the judge's reason, the run, and `notify`
     (if the criteria hold).
   - Open the home page and confirm the briefing card. Click "why", "not useful",
     and accept the habit. Confirm the scheduled prompt exists.
   - Take before/after screenshots (ui-evidence skill).
3. **Negative runs:**
   - a user with only one-off lookups: the dream does nothing
   - an agent with only one-off drafts: nothing promoted, and drafts expire only
     after 30 days
   - turn a ceiling off mid-night: queued runs end `cancelled:disabled`
4. **Record in the loop doc:**
   - final prompts
   - tokens and cost per dream kind
   - consolidation ratio (drafts to changes)
   - admin accept rate on nightly builds versus raw drafts
   - user-dream output counts per user
   - briefing click-through and "not useful" rate
   - every wrong output, with its `dream_runs` trace

### Metrics

| Metric | Target |
|---|---|
| Review items per agent per week (before vs after) | Down by at least 70% |
| Admin accept rate: nightly builds vs raw harness drafts | Higher |
| Eval pass rate of nightly builds | ≥ current suggestion builds |
| Briefing items clicked vs dismissed | Clicked > dismissed |
| Planned check-ins that end `sent` and get opened | ≥ 50% |
| Habit offers accepted | ≥ 30% |
| Cost per active user per night / per active agent per night | Tracked, within budget |

## 10. Risks and decisions

- **`inspect_data` in the agent dream:** it verifies table and column claims, but
  touches customer data unattended. Decide in Phase 2. The default proposal is to
  keep it, limited to schema and sample checks, as the harness does today.
- **Expiry of held drafts:** 30 days, with evidence kept for 90. Revisit after
  Loop B if slow patterns are being lost.
- **Quality of the user dream on the small model:** start with the small model.
  If the briefing's "not useful" rate is high, try the main model for the user
  dream in lab orgs before tightening the prompts.
- **Habit offers can feel pushy:** at most 1 per night, a 60-day suppression
  after a decline, and they are shown only in the briefing (pull), never
  notified.
- **Existing scheduled prompts:** the user dream must not offer a habit that
  duplicates one of U's active scheduled prompts. Check by report and intent.
