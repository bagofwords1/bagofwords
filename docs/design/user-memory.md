# Per-user memory: entries, capture and a generated profile

Status: **plan** (nothing implemented yet). Build it as a sandbox feedback loop
(`.agents/skills/sandbox-feedback-loop/SKILL.md`). The loop report goes in
`docs/feedback-loops/user-memory.md`.

Related plan: `docs/design/agent-checkins.md`. Check-ins **read** memory, but
memory does not depend on check-ins (see §11).

## 1. What we are building

Today the agent keeps one free-text memory document per user per org. We are
replacing it with:

- **Memory entries.** One row per fact about the user. Each entry has a section,
  optional dates, and a record of where it came from. Entries are private to the
  user.
- **A generated profile.** Code, not an LLM, turns the active entries into a
  compact document of about 2,000 characters. The profile is injected into every
  turn exactly where `<user_memory>` goes today.
- **Two ways to write memory:**
  1. The agent in the live turn, using an operations-based tool (add, update,
     forget). It never rewrites the whole document.
  2. A **silent capture pass after the turn** (small model, background). It picks
     up things the user said or did that are worth remembering, without being
     asked.
- **A memory UI in the user's profile.** Entries are listed by section. The user
  can edit, delete and add entries, see where each one came from, and forget
  everything.
- **Org settings gate everything.**

**What memory is about.** Memory covers the person, not the data:

| Section | What goes in it | Example |
|---|---|---|
| `style` | Writing and output style | "Prefers short answers with the number first, then one line of context"; "Writes to execs, likes bullet summaries"; "Amounts in €M, one decimal" |
| `role` | Role and work context | "Finance, owns EMEA revenue reporting; presents to the CFO monthly" |
| `vocabulary` | Their own words for things | "'my region' = EMEA"; "'the board deck' = report *Q3 Board Pack*" |
| `events` | Dated things in their work life: **meetings, deadlines, reviews, travel, time off** | "Board meeting Thu 2026-10-09"; "Out of office 2026-10-13 → 10-17"; "Quarterly business review 2026-10-20" |
| `focus` | What they are working on right now | "Investigating Q3 churn (since 2026-09-20)" |
| `preferences` | How they like to work with the agent | "Wants the SQL shown"; "Asks before running expensive queries"; "Prefers tables over charts" |

**Never stored:**
- query results, numbers, counts or other data values (they go stale and then
  read as fact)
- secrets or credentials
- facts about other people
- org-wide definitions or business rules. Those belong in **instructions**; the
  existing knowledge harness handles them.

### Out of scope for v1

- **Nightly consolidation.** No background LLM process that merges or rewrites
  entries. v1 relies on dedupe when entries are written, expiry computed in code,
  and a per-user cap (§6).
- A calendar or email integration. Events come **only from conversations**.
- Proposing org instructions based on patterns across users.
- A recall or search tool. The profile is the only read path in v1.

## 2. Current state (checked against the code)

| What | Where |
|---|---|
| `Membership.memory` (text, one per user per org) | `backend/app/models/membership.py:17-25` |
| Cap of 2,000 characters | `MEMBERSHIP_MEMORY_MAX_LENGTH`, `backend/app/schemas/organization_schema.py:15` |
| `update_user_memory` tool: full rewrite, `allowed_modes=["chat"]` | `backend/app/ai/tools/implementations/update_user_memory.py`, schema in `backend/app/ai/tools/schemas/update_user_memory.py` |
| Loading memory for a turn | `_resolve_user_profile()`, `backend/app/ai/agent_v2.py:979-1010` |
| Injection as `<user_memory>` in the per-turn user message (outside the cached prefix) | `PromptBuilderV3._format_user_memory`, `backend/app/ai/agents/planner/prompt_builder_v3.py:594-607`, used at `:855` |
| Prompt rule (declarative facts, org instructions win, nothing one-off or sensitive) | `prompt_builder_v3.py:429` |
| Profile API: GET/PUT the memory text | `backend/app/routes/user_profile.py:38-100` |
| Profile UI: one textarea | `frontend/components/UserProfileModal.vue:253-262, 743-766` |
| Tool card in the report timeline | `frontend/pages/reports/[id]/index.vue`, `frontend/pages/c/[token]/index.vue` |
| Post-turn hook, also used by the check-ins plan | `backend/app/ai/agent_v2.py:6796-6818` |
| Org settings pattern | `backend/app/schemas/organization_settings_schema.py:340-360` (e.g. `enable_agent_notes`); `locales/*.json` |

Problems with the current design:

- **Full rewrite loses data.** Two parallel sessions overwrite each other, and
  the model silently drops lines when it prunes.
- **Writes are explicit only.** Memory is saved only when the user asks, so style
  and events the user never phrased as "remember this" are lost.
- **No provenance and no dates.** There is no way to expire an event after it
  happens, to let the user's focus move on, or to answer "why do you know this?".

## 3. Settings (Phase 1)

Add to `OrganizationSettingsConfig`:

```python
enable_user_memory: FeatureConfig = FeatureConfig(
    value=True, name="User memory",
    description="Let the agent remember things about each user across sessions — "
                "writing style, role, their vocabulary, upcoming meetings and events, "
                "current focus. Each user can see, edit and delete their memory.",
    is_lab=False, editable=True)
enable_user_memory_capture: FeatureConfig = FeatureConfig(
    value=False, name="Automatic memory capture",
    description="After each conversation, quietly note things worth remembering "
                "about the user (style, events, vocabulary) even if they didn't "
                "ask. Requires User memory.",
    is_lab=True, editable=True)
```

- `enable_user_memory` defaults to **on**, because memory exists today. Turning it
  off does three things:
  - hides the memory tool
  - stops injecting the profile
  - hides the memory section in the profile UI

  It does **not** delete stored entries.
- `enable_user_memory_capture` defaults to **off** while it is in lab. It only
  takes effect when `enable_user_memory` is also on.
- Add names and descriptions to **every** `locales/*.json` catalog. The catalogs
  must keep an identical shape. Hebrew follows the vocabulary rules in
  `AGENTS.md`.

| Point | Gate |
|---|---|
| Injecting the profile into a turn | `enable_user_memory` |
| Memory tool present in the catalog | `enable_user_memory` |
| Dispatching the post-turn capture | `enable_user_memory` **and** `enable_user_memory_capture` |
| Applying captured operations | Re-checked just before writing |
| Memory API (read/write own entries) and profile UI section | `enable_user_memory`. When off, the API returns 403 with a typed error code and the UI hides the section |
| Check-in planner and judge reading the profile | `enable_user_memory` |

## 4. Data model (Phase 1)

New table `user_memory_entries`. The model goes in
`backend/app/models/user_memory_entry.py` with an Alembic migration in
`backend/alembic/versions/`. It must work on both SQLite and Postgres.

| Column | Type | Notes |
|---|---|---|
| `id` | str(36) pk | |
| `organization_id` | FK, indexed | |
| `user_id` | FK, indexed | Memory is per user **per org**, same as today |
| `section` | str(16) | `style`, `role`, `vocabulary`, `events`, `focus`, `preferences` |
| `text` | text, 280 chars max | One declarative fact ("Prefers…", "Board meeting…"). Never an imperative |
| `aliases` | JSON list, nullable | Mainly for `vocabulary` and `focus`: other words the user uses for the same thing |
| `event_start` | date or datetime, nullable | Required for `events` |
| `event_end` | date or datetime, nullable | For ranges such as time off |
| `expires_at` | datetime, nullable | Explicit expiry. When empty, the defaults in §6 apply |
| `source` | str(16) | `user` (typed in the UI), `agent` (tool call in a turn), `capture` (post-turn pass), `migration` |
| `evidence` | JSON, nullable | `{report_id, completion_id, quote}`. The quote is at most 200 chars of the user's own words |
| `seen_count` | int, default 1 | Increased when the same fact is written again |
| `last_seen_at` | datetime | |
| `status` | str(16), indexed | `active`, `superseded`, `forgotten` (expiry is computed, not stored) |
| `superseded_by_id` | str(36), nullable | |
| `created_at` / `updated_at` | | From `BaseSchema` |

Notes:
- Each entry has a short display handle, `m` + a base36 sequence number per user,
  shown in the profile so the tool can refer to it (`m7`). Either store it as a
  `handle` column, or derive it from ordering. Pick one and keep it stable.
- `forgotten` rows keep only their id, status and timestamps. Blank `text`,
  `aliases` and `evidence` when an entry is forgotten, so forgetting really
  removes the content.
- When a membership is deleted, cascade the delete. Include entries in any user
  data export.

**Migration of existing data.** For every non-empty `Membership.memory`, split
the text into lines or bullets, one entry per line:
- `section='preferences'`
- `source='migration'`
- `evidence` left empty

Keep the `Membership.memory` column, read-only, for one release so the change can
be rolled back, then drop it in a follow-up migration. The migration must be
idempotent: running it twice must not create duplicates.

## 5. Writing memory

### 5.1 In-turn tool (Phase 2)

Replace the full-rewrite schema of `update_user_memory` with operations. Keep the
tool name so the existing timeline card and prompt references don't break.

```python
class MemoryOp(BaseModel):
    op: Literal["add", "update", "forget"]
    handle: Optional[str]            # required for update/forget, e.g. "m7"
    section: Optional[Literal["style","role","vocabulary","events","focus","preferences"]]
    text: Optional[str]              # ≤280 chars, declarative
    aliases: Optional[List[str]]
    event_start: Optional[str]       # ISO date/datetime; required when section="events"
    event_end: Optional[str]
    expires_at: Optional[str]

class UpdateUserMemoryInput(BaseModel):
    operations: List[MemoryOp]       # 1..5
    title: Optional[str]
```

Behavior:
- `add` goes through the write-time dedupe in §6. If it matches an existing
  active entry, it increases `seen_count` instead of inserting a new row.
- `update` creates the new version and marks the old entry `superseded` (setting
  `superseded_by_id`), rather than editing in place. This keeps history for the
  user UI.
- `forget` marks the entry `forgotten` and blanks its content.
- `evidence` is filled automatically from the runtime context (report,
  completion, and the last user message truncated to 200 chars). The model never
  writes evidence.
- Validation:
  - `events` require `event_start`.
  - Text is at most 280 chars.
  - At most 5 operations per call.
  - Unknown handles return an error to the agent.
- The tool is available in **chat and Slack/Teams/email turns**, meaning any turn
  initiated by a human. It stays unavailable in training mode and in machine
  turns (turns with `trigger_source` set).
- Update the prompt rule at `prompt_builder_v3.py:429` to cover:
  - use `update_user_memory` operations
  - write declarative facts
  - put dated things in `events` with dates
  - send org-wide definitions to instructions, not memory
  - store nothing sensitive, nothing about others, no data values

### 5.2 Silent capture after the turn (Phase 3)

**Where:** the `agent_v2.py` post-analysis block, next to the harness and the
check-in planner. It runs as a background task with its own DB session. It emits
**no SSE and no blocks**, so the user sees nothing.

**Eligibility.** This is code with no LLM call, and every item must hold:
1. `enable_user_memory` and `enable_user_memory_capture` are both on.
2. The turn is human-initiated: `trigger_source IS NULL` and `webhook_id IS NULL`.
   Training mode is excluded, and so are errored turns.
3. The turn contains user text of at least 20 characters. Skip "thanks" and "ok".
4. The agent did **not** already call `update_user_memory` in this turn. That
   avoids double writes; the agent already handled it.

**The capture model** lives in `backend/app/ai/agents/user_memory/capture.py`. It
is one structured call to the small model.

Input:
- the user's messages in this turn (plus the previous user message, for context)
- the final answer, truncated
- the **current rendered profile, with handles**, so it can update instead of
  duplicating
- today's date and the timezone

Output: `{"operations": [MemoryOp, ...], "reason": "..."}` with **0 to 3
operations**. An empty list is the expected answer for most turns.

The prompt should state these rules explicitly:
- Capture only what is **about this person** and will matter in **future
  sessions**:
  - their writing and output style
  - their role
  - their words for things
  - dated events (meetings, deadlines, reviews, travel, time off), with dates
    resolved to absolute ISO dates
  - their current focus
  - how they like to work with the agent
- **Corrections are the strongest signal.** "No, I meant net revenue", "shorter
  please" and "don't use charts for this" are durable preferences when they are
  about the person's style. They are *instructions*, and not to be captured here,
  when they define a business term for everyone.
- Don't capture:
  - anything one-off or task-specific
  - data values or results
  - anything sensitive (health, personal life beyond work availability) or
    credentials
  - anything about other people
  - anything already present in the profile. Use `update` with its handle instead.
- Relative dates ("next Thursday", "after the offsite") must be resolved against
  today's date. If a date can't be resolved, don't create the event.

Code then **applies** the operations through the same service as the tool:
- source is `capture`
- evidence is filled automatically
- dedupe and caps apply (§6)
- the settings are re-checked before writing

LLM usage is recorded with the scope `user_memory_capture`, so its cost is
visible separately.

### 5.3 User edits (Phase 2)

These are REST endpoints for the **current user only**. They replace the memory
text field in `routes/user_profile.py`.

- `GET /api/users/me/memory`: active entries grouped by section, plus handles,
  evidence (report title and link) and the rendered profile preview.
- `POST /api/users/me/memory`: add an entry (`source='user'`).
- `PATCH /api/users/me/memory/{id}`: edit (supersede) an entry.
- `DELETE /api/users/me/memory/{id}`: forget an entry.
- `DELETE /api/users/me/memory`: forget everything, after a confirmation in the
  UI.

**Admins cannot read or write other users' memory. There is no admin endpoint.**
Test this with both roles.

## 6. Keeping it clean without consolidation (Phase 1: pure code, unit-tested)

There is no background LLM clean-up in v1, so these rules do the work. They live
in `backend/app/services/user_memory_service.py`.

**Write-time dedupe.** On `add`:
- Normalize the text: lowercase, collapse whitespace, strip punctuation.
- If an active entry in the same section has the same normalized text, or the
  same `vocabulary` alias key, increase `seen_count` and `last_seen_at` and merge
  the aliases, instead of inserting.
- Beyond that, the capture model and the agent see the profile with handles and
  are told to `update` rather than `add`.

**Expiry, computed on read, never by a job:**

| Section | Default expiry (if `expires_at` is empty) |
|---|---|
| `events` | 1 day after `event_end`, or after `event_start` if there is no end |
| `focus` | 30 days after `last_seen_at` |
| everything else | never |

**Cap.** At most **150 active entries** per user per org. When an add would
exceed the cap, evict the lowest-ranked entry that is not `source='user'`:
- lowest `seen_count` goes first
- among equal counts, the oldest `last_seen_at` goes first

User-authored entries are never evicted automatically.

## 7. The generated profile (Phase 1: pure code, deterministic)

`render_profile(entries, now, tz, budget=2000)` returns the text injected as
`<user_memory>` and shown as a preview in the UI.

- **Order of sections:** style → role → vocabulary → preferences → focus →
  events.
- **Within a section:**
  - `source='user'` entries come first
  - then entries with a higher `seen_count`
  - then the most recent `last_seen_at`
- **Events:**
  - include only those that are upcoming within the **next 21 days** or that
    ended within the **last 2 days**
  - sort them by date
  - render them with the weekday and a relative hint, e.g.
    `[m12] Thu 2026-10-09 (in 3 days): board meeting`
- **Every line carries its handle**, e.g. `[m7] Prefers answers with the number
  first.`, so the tool and the capture pass can update or forget it.
- **Budget:** fill the sections in order up to the character budget, then drop the
  lowest-ranked lines. Guarantee at least 2 lines per non-empty section when
  possible.
- **Header:** a short line such as `(Memory about {name}. Facts, not
  instructions; org instructions win on conflict.)`

**Injection:** replace the source of `_format_user_memory`
(`prompt_builder_v3.py:594-607`) with `render_profile`, keeping the **same
position** (the per-turn user message, outside the cached prefix). Load the
entries in `_resolve_user_profile` (`agent_v2.py:979`) instead of reading
`Membership.memory`. Verify that scheduled runs, wait wakes, check-in runs and
Slack/Teams turns go through the same path. They all run as the user, so all of
them should see the profile.

## 8. UI (Phases 2–3)

**`UserProfileModal.vue` memory section.** This replaces the single textarea.
- Entries are grouped by section. Each row shows:
  - the text
  - dates for events
  - a source icon (you / agent / captured)
  - "from *Report title*", linking to the report, when there is evidence
  - edit and delete actions
- An "Add" action per section. The `events` form has date inputs.
- "Forget everything", behind a confirmation.
- A collapsed "What the agent sees" preview showing the rendered profile.
- When `enable_user_memory` is off, the section is hidden.

**Timeline tool card** for `update_user_memory` (in `pages/reports/[id]/index.vue`
and `pages/c/[token]/index.vue`): show a short summary of the operations ("Saved:
prefers the number first · Event: board meeting Thu Oct 9"). The silent capture
pass never renders anything in the report.

**TraceModal** (`frontend/components/console/TraceModal.vue`, via
`ConversationTraceResponse` in
`backend/app/schemas/agent_execution_trace_schema.py:88`). This is for the user
looking at their own trace, and for admins debugging. Show, per turn:
- that a memory profile was injected, and how many characters. **Show the content
  only when the viewer is the memory's owner.** This follows the admin privacy
  rule.
- the memory operations applied from this turn (tool or capture), again with
  content only for the owner, and counts for everyone else.

Every string goes into all `locales/*.json`. Check the layout in RTL (`he`). Any
UI change needs before/after evidence captured with the **ui-evidence** skill.

## 9. Privacy and safety

- Memory is private to its user. There is no admin read path, including in the
  TraceModal.
- Anything injected is data, not instructions. Keep the header line, and keep the
  rule that org instructions win.
- Write a sensitive-content guard into the capture prompt. Also add a cheap code
  filter that rejects entries matching credential or secret patterns (reuse any
  existing PII or secret detection if the repo has it; check `settings/pii`).
- Deleting a membership deletes its entries. User data exports include them.

## 10. Phases and exit criteria

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Settings and locales; the `user_memory_entries` model and migration; the migration of existing memory text; `UserMemoryService` (dedupe, expiry, cap); `render_profile`; injection switched to the rendered profile | Unit tests pass on sqlite and postgres. Existing memory text shows up unchanged in meaning in the new profile. Turning the setting off stops injection |
| 2 | Operations-based `update_user_memory`; REST endpoints; the new profile UI section; the tool card | A user can add, edit and forget entries in the UI and through the agent. Parallel writes don't lose entries. Admins get 403 on other users' memory |
| 3 | Silent post-turn capture behind `enable_user_memory_capture`; TraceModal memory lines | With capture on and a stubbed model, eligible turns write entries with evidence. Machine turns, errored turns and training turns capture nothing |
| 4 | Tuning from Loop B metrics; the link to check-ins (§11) | Capture precision meets the targets in §12 |

## 11. Relationship to check-ins

**The link is one-way: check-ins read memory. Memory doesn't depend on
check-ins.**

- **Check-in planner and judge.** Add the rendered profile to their input,
  gated by `enable_user_memory`. The value is mostly in the **events** section:
  - a follow-up can land before or after a meeting the user mentioned
  - a follow-up can avoid their time off
  - the judge can skip when the user said the topic is closed

  This is a small addition to `docs/design/agent-checkins.md` (§6 planner input,
  §9 judge input). Do it after both features exist.
- **Check-in runs** see the profile automatically, because they run as the user
  through the same path (§7).
- **Check-ins never write memory in v1.** Check-in runs are machine turns, and
  capture and the tool are disabled for machine turns. Check-in engagement stays
  in `agent_checkins`.
- **Not in v1:** creating a check-in *because of* a memory event (for example,
  preparing a brief the day before a board meeting). It's a good follow-up
  feature once both are live.

## 12. Feedback loop (to run in the new session)

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md`.

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

### Loop A: deterministic (no real LLM)

Stub only at the boundaries: the LLM (capture model and agent) and the clock.
Seed data through `tests/fixtures/*`. Each test must first be watched failing
(stash the implementation), per rule 6 in `backend/tests/AGENTS.md`.

Unit tests (`backend/tests/unit/test_user_memory_service.py`,
`test_user_memory_render.py`):
1. **Dedupe.** The same fact with different case, whitespace or punctuation
   increases `seen_count` and doesn't insert. A different section inserts. An
   alias match on `vocabulary` merges.
2. **Expiry.**
   - An event disappears from the profile 1 day after its end.
   - A range event (time off) stays visible for its whole duration.
   - Focus disappears 30 days after it was last seen, and reappears when it is
     seen again.
   - Vary the dates; don't hard-code a single scenario.
3. **Cap.** The 151st add evicts the lowest-ranked non-user entry. User-authored
   entries are never evicted, even when they rank lowest.
4. **Render.**
   - It respects the budget.
   - Section order holds.
   - Events outside the display window are excluded.
   - The handles are present and stable across renders.
   - At least 2 lines per non-empty section when the budget allows.
5. **Operations validation.**
   - An event without a date is rejected.
   - An unknown handle is rejected.
   - Text over 280 characters is rejected.
   - More than 5 operations is rejected.
6. **Migration.** Multi-line memory text becomes N entries. Running the migration
   twice creates no duplicates. Empty memory creates nothing.

E2E tests (`backend/tests/e2e/test_user_memory.py`, run with both `--db=sqlite`
and `--db=postgres`):
7. **Setting off.**
   - No profile is injected (check through the planner input in the context
     snapshot).
   - The tool is absent from the catalog.
   - The API returns 403.
   - The entries still exist after the setting is turned back on.
8. **The API is scoped to the user's own memory.**
   - A member can create, read, update and delete only their own entries.
   - Another member, and **an org admin**, cannot read or change them (403/404).
9. **Parallel writes.** Two concurrent `add` operations from different reports
   both persist. This is the regression that motivates entries over the full
   rewrite.
10. **Capture on**, with a stubbed model that returns an `events` add and a
    `style` add:
    - both entries are stored with `source='capture'` and evidence pointing to the
      turn
    - the report timeline is **unchanged**
11. **Capture skips.** With capture off, or for a machine turn (`trigger_source`
    set), or an errored turn, or a turn where the agent already called the tool:
    zero capture calls.
12. **Forget through chat.** A stubbed tool call with `forget m3`: the entry is
    `forgotten`, its content is blanked, and it is absent from the next profile.

### Loop B: live (real LLM, real stack)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
```

1. Turn on `enable_user_memory_capture`. Take a screenshot of AI settings.
2. **Style.** Across 2–3 turns, correct the agent's style ("shorter, number
   first", "use €M"). Check that the entries appear in the profile UI with
   evidence links. In a **new report**, ask a fresh question and confirm the
   answer follows the style.
3. **Events.** Say "I have the board meeting next Thursday and I'm off the week
   after". Confirm two `events` entries with correct absolute dates. Then move the
   clock forward, or edit the dates to the past, and confirm they drop out of the
   profile.
4. **Vocabulary.** Say "when I say my region I mean EMEA". In a new report, ask
   "revenue in my region" and confirm it filters to EMEA.
5. **No-capture cases.** Run 10 varied one-off questions and record how many
   entries get captured. The target is close to zero.
6. **Forget.** "Forget that I'm off next week" removes the entry. Also test
   "forget everything" in the UI.
7. **Privacy.** Log in as an admin. There must be no access to the member's
   entries in the UI or the API, and the TraceModal shows counts only.
8. Take before/after UI screenshots (ui-evidence skill).
9. Record in the loop doc: the final prompts; capture precision (what share of
   captured entries a human would keep); the number of captures per 10 turns;
   the cost per capture; and any wrong captures, with their traces.

### Metrics

- **Capture precision:** the share of captured entries kept or unedited after 7
  days. Target ≥ 80%.
- **User deletes of captured entries:** high means capture is too eager.
- **Repeated corrections:** the same style correction made again after it was
  captured should trend to 0.
- **Profile budget usage:** p50 and p95 characters.
- **Cost:** capture tokens per eligible turn.

## 13. Risks and decisions

- **Default for capture:** off, while it is in lab. Revisit after the Loop B
  precision numbers.
- **Admin visibility:** none, by design. If support needs to debug, the user can
  share a screenshot, or we add an explicit per-user "share memory with support"
  toggle later.
- **Without consolidation, entries can pile up.** The cap, expiry and dedupe
  bound this. If Loop B shows near-duplicate entries piling up, schedule the
  nightly consolidation as the next plan, rather than tightening capture until it
  misses things.
- **Personal content in events.** Keep to work availability (meetings,
  deadlines, travel, time off). The capture prompt excludes reasons for time off
  and personal details.
