# User memory: entries, tools, context builder and nightly dream

Status: **plan** (nothing implemented yet). Build it as a sandbox feedback loop
(`.agents/skills/sandbox-feedback-loop/SKILL.md`). The loop report goes in
`docs/feedback-loops/user-memory.md`.

Related plan: `docs/design/agent-checkins.md`. Check-ins **read** memory;
memory never depends on check-ins (§12).

## 1. What we are building

- **Memory entries.** One row per durable fact about the user, with a section,
  tags, optional dates, and provenance (the report, the turn, and a short quote).
  Entries are private to that user.
- **Three agent tools**, named like the existing instruction and note tools:
  `create_memory`, `edit_memory` and `search_memory`. The agent saves memory
  **whenever it notices something durable**, not only when asked.
- **A tiered memory context builder.** Budgets are fixed no matter how many
  entries a user has. It has four parts:
  - an **always** part: style, role, preferences and upcoming events
  - a **matched** part: entries whose tags or aliases match the current prompt,
    report, agents or data sources
  - an **index line** summarizing the rest
  - `search_memory` for everything else
- **A nightly dream** for each user who was active that day. It **extracts** new
  memories from the day's sessions, then **consolidates** them: merges
  duplicates, replaces outdated facts, retags entries, and updates the user's
  current focus.
- **A memory UI in the user's profile.** The user can view, edit, add and delete
  entries, see where each one came from, and forget everything.
- **Org settings gate every step.**

### What goes into user memory

| Section | Content | Example |
|---|---|---|
| `style` | Writing and output style | "Prefers the number first, then one line of context"; "Amounts in €M, one decimal"; "Writes for execs: bullet summaries" |
| `role` | Role and work context | "Finance, owns EMEA revenue reporting; presents to the CFO monthly" |
| `vocabulary` | Their own words for things | "'my region' = EMEA"; "'the board deck' = report *Q3 Board Pack*" |
| `events` | Dated items in their work life: meetings, deadlines, reviews, travel, time off | "Board meeting Thu 2026-10-09"; "Out of office 2026-10-13 → 10-17" |
| `focus` | What they are working on now | "Investigating Q3 churn (since 2026-09-20)" |
| `preferences` | How they like to work with the agent | "Wants the SQL shown"; "Asks before running expensive queries" |

**Never stored:**
- query results or data values
- secrets or credentials
- facts about other people
- health or other personal details (events are limited to work availability)
- org-wide definitions or business rules. Those are **instructions**, and the
  knowledge harness handles them.

### Out of scope

- A calendar or email integration. Events come only from conversations.
- Proposing org instructions from patterns across users.
- Admin access to user memory (by design, §11).

## 2. Current state (checked against the code)

| What | Where |
|---|---|
| `Membership.memory` (one text field per user per org), 2,000-char cap | `backend/app/models/membership.py:17-25`; `MEMBERSHIP_MEMORY_MAX_LENGTH` in `backend/app/schemas/organization_schema.py:15` |
| `update_user_memory` tool: rewrites the whole document, `allowed_modes=["chat"]` | `backend/app/ai/tools/implementations/update_user_memory.py`; schema in `backend/app/ai/tools/schemas/update_user_memory.py` |
| Loading memory | `_resolve_user_profile()`, `backend/app/ai/agent_v2.py:979-1010` |
| Injection as `<user_memory>` in the per-turn user message (outside the cached prefix) | `PromptBuilderV3._format_user_memory`, `backend/app/ai/agents/planner/prompt_builder_v3.py:594-607`, used at `:855` |
| Prompt rule for memory | `prompt_builder_v3.py:429` |
| Profile API (GET/PUT the memory text) | `backend/app/routes/user_profile.py:38-100` |
| Profile UI (one textarea) | `frontend/components/UserProfileModal.vue:253-262, 743-766` |
| Tool card in the report timeline | `frontend/pages/reports/[id]/index.vue`, `frontend/pages/c/[token]/index.vue` |
| Tool naming convention to mirror | `ai/tools/implementations/{create,edit,search,read}_instruction.py`, `{create,edit}_note.py` |
| Keyword matching to reuse (always vs intelligent, keyword extraction, relevance gate) | `backend/app/ai/context/builders/instruction_context_builder.py` (`_extract_keywords`, `search_instructions` `:377`, `build` `:481`) |
| Scheduler, leader-only jobs, exactly-once claim | `backend/main.py:470-611`; `claim_scheduled_run`, `backend/app/core/scheduler.py:109` |
| Rolling session summary per report | `backend/app/models/report_context_state.py` (`summary_json`); `services/context_compaction_service.py` |
| Org settings pattern | `backend/app/schemas/organization_settings_schema.py:340-360`; names and descriptions in `locales/*.json` |

There is **no** DB-backed work queue (`skip_locked` isn't used anywhere). The
dream uses leader-driven, bounded-concurrency processing with per-user claims
(§8).

Problems with today's design:
- The full rewrite loses data under parallel sessions, and the model drops lines
  when it prunes.
- Memory is saved only on explicit requests.
- There's no provenance, no dates, and no expiry.
- A single 2k document can't grow.

## 3. Settings (Phase 1)

Add these fields to `OrganizationSettingsConfig`:

```python
enable_user_memory: FeatureConfig = FeatureConfig(
    value=True, name="User memory",
    description="Let the agent remember things about each user across sessions — "
                "writing style, role, their vocabulary, upcoming meetings and events, "
                "current focus. Each user can see, edit and delete their memory.",
    is_lab=False, editable=True)
enable_memory_dreaming: FeatureConfig = FeatureConfig(
    value=False, name="Nightly memory learning",
    description="Each night, review the day's conversations of active users to "
                "learn new memories and tidy existing ones (merge duplicates, "
                "retire outdated facts). Requires User memory.",
    is_lab=True, editable=True)
```

- `enable_user_memory` defaults to **on**, because memory exists today. Turning it
  off does three things:
  - hides the tools
  - stops injection
  - hides the UI section and makes the API return 403 with a typed error code

  Entries are **kept**.
- Add names and descriptions to **every** `locales/*.json` catalog. The catalogs
  must keep an identical shape. Hebrew follows the vocabulary rules in
  `AGENTS.md`.

| Point | Gate |
|---|---|
| Memory context injection | `enable_user_memory` |
| `create_memory` / `edit_memory` / `search_memory` in the tool catalog | `enable_user_memory` |
| Nightly dream: enqueueing orgs, then again per user before writing | `enable_user_memory` **and** `enable_memory_dreaming` |
| User memory API and profile UI section | `enable_user_memory` |
| Check-in planner and judge reading memory | `enable_user_memory` |

## 4. Data model (Phase 1)

New table `memory_entries`. The model goes in `backend/app/models/memory_entry.py`
with an Alembic migration in `backend/alembic/versions/`. It must work on both
SQLite and Postgres.

| Column | Type | Notes |
|---|---|---|
| `id` | str(36) pk | |
| `organization_id` | FK, indexed | |
| `user_id` | FK, indexed | The owner. Memory is per user **per org**, same as today |
| `handle` | str(12) | Stable short id, unique per (org, user): `m1`, `m2`… |
| `section` | str(16) | `style`, `role`, `vocabulary`, `events`, `focus`, `preferences` |
| `text` | text, at most 280 chars | One declarative fact, never an imperative |
| `tags` | JSON list | 1–4 normalized slugs (§6) |
| `aliases` | JSON list, nullable | Other words for the same thing (vocabulary, focus) |
| `event_start` / `event_end` | datetime, nullable | `event_start` is required for `events` |
| `expires_at` | datetime, nullable | When empty, the defaults in §7 apply |
| `source` | str(12) | `user` (typed in the UI), `agent` (in-turn tool), `dream`, `migration` |
| `evidence` | JSON, nullable | `{report_id, completion_id, quote}`. The quote is at most 200 chars of the user's own words and is filled by code, never by the model |
| `seen_count` | int, default 1 | |
| `last_seen_at` | datetime | |
| `status` | str(12), indexed | `active`, `superseded`, `forgotten` |
| `superseded_by_id` | str(36), nullable | |
| `created_at` / `updated_at` | | From `BaseSchema` |

Rules:
- **User-authored entries (`source='user'`) are changed only by the user.** The
  tool and the dream may *read* them, and may *propose* in the trace that one
  looks stale, but they never update, supersede or forget them.
- `forgotten` blanks `text`, `aliases`, `tags` and `evidence`. Only the id, status
  and timestamps are kept.
- Deleting a membership deletes its entries. User data exports include them.
- **Migration:** every non-empty `Membership.memory` becomes one entry per line
  or bullet, with `section='preferences'`, `source='migration'` and no tags. The
  first dream can retag them. The migration must be idempotent. Keep
  `Membership.memory` read-only for one release so the change can be rolled back,
  then drop it.

## 5. Tools (Phase 2)

These replace `update_user_memory`. Update the timeline tool card in both report
pages accordingly. They are available on **human-initiated turns in every
channel** (web, Slack, Teams, email). They are **not** available in training
mode or in machine turns (`trigger_source` set).

### `create_memory`
```python
class CreateMemoryInput(BaseModel):
    text: str                         # ≤280, declarative fact
    section: Literal["style","role","vocabulary","events","focus","preferences"]
    tags: List[str]                   # 1..4; reuse tags shown in <memory> index
    aliases: Optional[List[str]] = None
    event_start: Optional[str] = None # ISO; required for section="events"
    event_end: Optional[str] = None
    expires_at: Optional[str] = None
    title: Optional[str] = None       # status line
```
- Validation:
  - events require a date
  - text is at most 280 chars
  - tags are normalized, with 1–4 of them
- **Dedupe (§7):** if the entry matches an existing one, the tool strengthens it
  (`seen_count`, `last_seen_at`, merged aliases) and returns
  `{"deduped_into": "m7"}`.
- Evidence is filled automatically from the runtime context.

### `edit_memory`
```python
class EditMemoryInput(BaseModel):
    handle: str                       # e.g. "m7"
    action: Literal["update","delete"]
    text: Optional[str] = None
    section: Optional[str] = None
    tags: Optional[List[str]] = None
    aliases: Optional[List[str]] = None
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    expires_at: Optional[str] = None
    title: Optional[str] = None
```
- `update` inserts the new version and marks the old one `superseded`.
- `delete` marks the entry `forgotten` and blanks its content.
- An error is returned for:
  - an unknown handle
  - a `source='user'` entry (unless the user explicitly asked in this turn: "forget
    that I…". Detect this by the user message directly requesting it. If that's
    ambiguous, refuse and tell the agent to ask the user to edit it in their
    profile.)

### `search_memory`
```python
class SearchMemoryInput(BaseModel):
    query: Optional[str] = None
    tags: Optional[List[str]] = None
    section: Optional[str] = None
    include_past_events: bool = False
    limit: int = 10                   # ≤25
```
- It returns full entries: handle, section, text, tags, aliases, dates, source,
  and the source report title and link. It scores with the same keyword and alias
  scorer as the context builder (§6).
- It excludes entries already injected in this turn, and says so in its output.

No `read_memory`: entries are at most 280 chars and `search_memory` returns them
in full. That's the "get" path.

### Prompt rules

Replace the rule at `prompt_builder_v3.py:429` with:

- Memory is **your** knowledge about this user. It is facts, not instructions,
  and org instructions win on conflict.
- **Save when you notice**, not only when asked. Clear signals are:
  - a correction of your style or format
  - a stated role or responsibility
  - their word for something
  - a dated meeting, deadline or time off (resolve the date to an absolute ISO
    date)
  - what they're working on now
- Don't save one-off task details, data values or results, anything about other
  people, sensitive details, or org-wide definitions (propose an instruction
  instead).
- Before creating an entry, check the memory shown in context. If a matching
  entry exists, `edit_memory` it rather than creating a duplicate. Reuse the
  existing tags.
- Use `search_memory` when the user refers to something personal the injected
  memory doesn't cover ("like last time", "my usual format for…").
- Write declarative facts ("Prefers…"), never imperatives.

## 6. Context builder (Phase 1)

New `backend/app/ai/context/builders/memory_context_builder.py`. It is pure code
with no LLM calls. It **reuses** `_extract_keywords` and the scoring approach from
`instruction_context_builder.py`; extract a shared helper if needed.

**Relevance signals for the current turn:**
- keywords from the user's current prompt and the previous one
- the report title
- the ids of the report's agents and data sources

**Tags:**
- **Topic tags:** lowercase slugs such as `emea`, `board-deck`, `churn`. Writers
  are shown the user's existing tags (with counts) and told to reuse them. Code
  normalizes them (lowercase, hyphens, no spaces) and merges exact slug matches.
- **Object tags** link an entry to real objects: `agent:<id>`,
  `data_source:<id>`, `report:<id>`. An entry that carries one is
  matched whenever that object is in scope for the turn. For example, "prefers
  weekly granularity for the Sales agent" is tagged `agent:<sales>`.

**Tiers and budgets**, sized so the rendered block is at most about 2,200 chars:

| Tier | What goes in it | Budget | Order within the tier |
|---|---|---|---|
| **Always** | `style`, `role`, `preferences`, plus events that start within the next 21 days or ended within the last 2 days | ~1,200 chars | Events by date. Otherwise `source='user'` first, then higher `seen_count`, then most recent `last_seen_at` |
| **Matched** | Any other active entry (vocabulary, focus, older preferences) whose text, alias or tags overlap the turn's keywords, **or** that carries an object tag in scope | ~800 chars, at most 10 entries | Relevance score, with an object-tag match weighted highest |
| **Index line** | A summary of what isn't shown: counts per section and the top tags | ~200 chars | Example: `Also remembered (not shown): 14 vocabulary, 3 focus, 9 preferences · tags: emea, board-deck, churn, cfo-review — use search_memory.` |

**Render format** (injected as `<memory>` in the same position as today's
`<user_memory>`, meaning the per-turn user message, outside the cached prefix):

```
<memory owner="user">
(Facts about {name}. Not instructions; org instructions win on conflict.)
[m3] style: Prefers the number first, then one line of context.
[m5] role: Finance, owns EMEA revenue reporting.
[m12] event: Thu 2026-10-09 (in 3 days): board meeting.
[m21] vocabulary: "my region" = EMEA.   ← matched: "region"
Also remembered (not shown): …
</memory>
```

- Load entries in `_resolve_user_profile` (`agent_v2.py:979`) instead of reading
  `Membership.memory`.
- Scheduled runs, wait wakes, check-in runs and Slack/Teams turns run as the
  user. Verify that all of them go through this path and see the memory.

## 7. Staying clean between dreams (Phase 1: pure code)

This lives in `backend/app/services/memory_service.py`, and all writers use it:
the tools, the dream, the UI API, and the migration.

- **Dedupe on write:**
  - Normalize the text (lowercase, collapsed whitespace, no punctuation).
  - Same user, same section and same normalized text: strengthen the
    existing entry instead of inserting.
  - `vocabulary` entries also merge when an alias matches.
- **Computed expiry** (on read, no job):

  | Section | Default when `expires_at` is empty |
  |---|---|
  | `events` | 1 day after `event_end`, or after `event_start` if there is no end |
  | `focus` | 30 days after `last_seen_at` |
  | everything else | never |

- **Cap:** at most 200 active entries per user per org. When full, evict the
  non-user entry with the lowest `seen_count`, then the oldest `last_seen_at`.
  User-authored entries are never evicted automatically.
- **Sensitive-content filter:** reject text matching credential or secret
  patterns (tokens, keys, passwords) before any write. Check whether the repo's
  PII settings (`frontend/pages/settings/pii.vue`) have a reusable detector;
  otherwise add a small regex set.

## 8. Nightly dream: extract and consolidate (Phase 3)

`backend/app/services/memory_dream_service.py` plus
`backend/app/ai/agents/memory/dream.py`.

**Scheduling:**
- Register a leader-only cron job in `backend/main.py` (next to the other
  leader-only jobs), running hourly as `memory_dream_sweep`.
- On each tick, for each org with both settings on, if the local time in the org
  timezone is between 01:00 and 05:00, pick the **due users**:
  - memberships with human-initiated completions since the membership's
    `memory_dreamed_at` watermark (new column on `Membership`)
  - at most N per org per tick, default 100
- Process them with bounded concurrency: a global semaphore of about 4, and at
  most 2 at a time per org.
- Each user is claimed with `claim_scheduled_run(f"memory_dream:{membership_id}:{local_date}")`,
  so they're processed exactly once per night across workers.
- Advance the watermark only after the write succeeds. A crash resumes the next
  hour.

**Input for one user**, capped at about 15k tokens, trimmed oldest-first:
- For each report the user worked in since the watermark (human-initiated turns
  only; excludes check-in, wait, scheduled and webhook turns, training mode and
  errored turns):
  - the report title
  - the **user's own messages** (truncated per message)
  - the existing rolling summary (`report_context_state.summary_json`), rather
    than full transcripts
- Memory entries the agent created that day, flagged "already saved today".
- All current active entries (handles, sections, tags, sources, seen counts),
  plus the list of tags in use.
- Today's date and timezone.

**Output:** validated with pydantic. Invalid output means nothing is written for
that user, and the watermark is not advanced.

```json
{
  "operations": [
    {"op": "create", "section": "events", "text": "Quarterly business review",
     "event_start": "2026-10-20", "tags": ["qbr"], "evidence_report_id": "…"},
    {"op": "update", "handle": "m9", "text": "Investigating Q3 churn by plan (since 2026-09-20)", "tags": ["churn"]},
    {"op": "merge", "handles": ["m14","m22"], "into_text": "Prefers tables over charts for breakdowns", "tags": ["format"]},
    {"op": "forget", "handle": "m4", "reason": "focus superseded by m9"},
    {"op": "retag", "handle": "m30", "tags": ["emea","revenue"]}
  ],
  "notes": "one line summary for the trace"
}
```

**Dream prompt rules:**
- **Extract:** facts about the person that recur, or that are clearly durable:
  - style corrections, especially repeated ones
  - role
  - their vocabulary
  - dated work events, resolved to absolute dates
  - current focus

  **Repetition across sessions is the strongest signal.** A single weak signal is
  not enough unless it is explicit ("I'm off next week").
- **Consolidate:**
  - merge near-duplicates
  - update entries that newer evidence contradicts
  - retag untagged entries (for example, migrated ones)
  - forget focus entries clearly superseded
- **Never touch `source='user'` entries.** If one looks stale, emit
  `{"op":"flag_stale","handle":…}`. It is only shown in the trace and the UI, and
  never applied.
- The same "never store" list as §1. Prefer 0–5 operations. Empty output is fine.

**Applying the operations:** everything goes through `MemoryService`, with
`source='dream'`, dedupe, the cap and the sensitive-content filter. Evidence is
built from `evidence_report_id` plus the matching user message quote, located by
code. Both settings are re-checked before writing. LLM usage is recorded under
the scope `memory_dream`.

**Log:** add a `memory_dream_runs` table with `membership_id`, `run_at`,
`status`, `ops_applied` (counts per op), `notes`, `tokens` and `cost`. It is used
by the trace and UI (§10) and by the metrics.

## 9. User API and UI (Phase 2)

**API** (current user only), replacing the memory text field in
`routes/user_profile.py`:
- `GET /api/users/me/memory`: entries grouped by section, tags, evidence (report
  title and link), stale flags, the rendered preview, and the last dream run.
- `POST /api/users/me/memory`: create (`source='user'`).
- `PATCH /api/users/me/memory/{id}`: update, which supersedes the old version.
- `DELETE /api/users/me/memory/{id}`: forget.
- `DELETE /api/users/me/memory`: forget everything.

**There is no admin endpoint.** Other members and admins get 403 or 404.

**`UserProfileModal.vue` memory section** (replacing the textarea):
- Entries grouped by section. Each row shows:
  - the text
  - dates for events
  - tag chips
  - a source icon (you / agent / nightly)
  - "from *Report*", linking to the report
  - edit and delete actions
  - a "may be outdated" badge when the dream flagged it
- An "Add" action per section. The events form has date inputs.
- A filter by tag.
- "Forget everything", behind a confirmation.
- A collapsed "What the agent sees" preview of the always tier and the index
  line.

Every string goes into all `locales/*.json`. Check the layout in RTL (`he`). Any
UI change needs before/after evidence captured with the **ui-evidence** skill.

## 10. Trace visibility (Phase 3)

These go into `TraceModal` (`frontend/components/console/TraceModal.vue`), via
`ConversationTraceResponse`
(`backend/app/schemas/agent_execution_trace_schema.py:88`) and its builder in
`console_service.py:2078-2356`.

- **Per turn:**
  - which memory tier entries were injected (handles and tiers), with the
    rendered size
  - `create_memory`, `edit_memory` and `search_memory` calls. These show as
    normal tool executions.
- **Privacy:** the entry **text** is shown only when the viewer is the memory's
  owner. Everyone else, admins included, sees handles, sections and counts only.
- The **dream run log** for the report's owner: operations that came from this
  report's sessions (matched by `evidence.report_id`), with the notes line.

## 11. Privacy and safety

- User memory is private, and there is no admin read path. If support needs
  access later, add an explicit per-user "share my memory with admins" toggle.
  Not in v1.
- Memory is injected as data, never instructions, and org instructions win.
- The sensitive-content filter (§7) runs on every write, and the prompts exclude
  personal details.
- Deleting a membership deletes the memory. Exports include it.

## 12. Relationship to check-ins

**The link is one-way: check-ins read memory.**

- The check-in planner and judge get the rendered always tier, events in
  particular. Follow-ups can land around the user's meetings and avoid their time
  off. This is gated by `enable_user_memory` and is a small addition to
  `agent-checkins.md` (§6 planner input, §9 judge input).
- Check-in runs see memory automatically, because they run as the user.
- **Check-ins never write memory.** Their turns are machine turns, so the tools
  are unavailable and the dream skips them.
- Not in scope: creating check-ins *from* memory events, such as a brief before a
  board meeting.

## 13. Phases and exit criteria

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Settings and locales; the `memory_entries` model and migration; migration of existing memory text; `MemoryService` (dedupe, expiry, cap, filter); `MemoryContextBuilder` (tiers, tags, keyword matching); injection switched over | Unit tests pass on sqlite and postgres. Migrated memory appears in the always tier. Turning the setting off stops injection. Budgets hold with 200 entries |
| 2 | `create_memory` / `edit_memory` / `search_memory` (replacing `update_user_memory`); the prompt rule; the user API and profile UI; the tool cards | The agent saves on corrections without being asked. Parallel writes don't lose entries. User-authored entries are protected. Admins get 403 |
| 3 | The nightly dream (sweep, claim, watermark, extraction and consolidation, the run log); TraceModal memory lines | With a stubbed model, the dream creates, merges, updates and forgets correctly, never touches user entries, and skips machine turns. The watermark resumes after a crash |

## 14. Feedback loop (to run in the new session)

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md`.

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

### Loop A: deterministic (no real LLM)

Stub only at the boundaries: the LLM (agent and dream) and the clock. Seed data
through `tests/fixtures/*`. Each test must first be watched failing (stash the
implementation), per rule 6 in `backend/tests/AGENTS.md`.

Unit tests (`backend/tests/unit/test_memory_service.py`,
`test_memory_context_builder.py`, `test_memory_dream_apply.py`):

1. **Dedupe:**
   - normalization variants strengthen the existing entry
   - a different section inserts a new one
   - a vocabulary alias match merges
2. **Expiry:**
   - an event is hidden 1 day after its end
   - a range event stays visible for its whole duration
   - focus expires 30 days after last seen, and comes back when seen again

   Vary the dates.
3. **Cap:** the 201st write evicts the lowest-ranked non-user entry. User entries
   are never evicted.
4. **Context builder:**
   - Budgets hold with 0, 10 and 200 entries.
   - The always tier includes style, role, preferences and events in the window
     only.
   - An alias match puts the vocabulary entry in the matched tier.
   - An object tag (`agent:<id>`) matches when that agent is in the report.
   - The index line counts what was left out.
   - Handles are stable across renders.
5. **Tag normalization** (`Board Deck`, `board_deck` → `board-deck`) and
   exact-slug merging.
6. **Tool validation:**
   - an event without a date is rejected
   - more than 4 tags is rejected
   - text over 280 chars is rejected
   - an unknown handle is rejected
   - `edit_memory` on a `source='user'` entry without an explicit request is
     rejected
7. **Dream apply:**
   - each op type works
   - a `flag_stale` on a user entry changes nothing
   - an op targeting a user entry is rejected
   - invalid JSON writes nothing and doesn't advance the watermark
8. **Migration:** it is idempotent, and multi-line text becomes N entries.

E2E tests (`backend/tests/e2e/test_memory.py`, run with both `--db=sqlite` and
`--db=postgres`):

9. **Setting off:**
   - no `<memory>` in the planner input (checked through the context snapshot)
   - the tools are absent from the catalog
   - the API returns 403
   - entries survive turning the setting back on
10. **API is scoped to the user's own memory:**
    - a member can create, read, update and delete their own entries
    - **another member and an org admin** can't read or change them
11. **Parallel writes:** two concurrent `create_memory` calls from different
    reports both persist.
12. **Agent saves on noticing:** a stubbed agent calls `create_memory` after a
    style correction. The entry has `source='agent'` and evidence pointing to the
    turn. The next turn's `<memory>` contains it.
13. **`search_memory`** returns matching entries not already injected, with
    handles and source links.
14. **Dream sweep:**
    - with both settings on and a stubbed dream, only memberships with new
      human-initiated turns are processed
    - the claim prevents double processing
    - the watermark advances
    - machine-turn-only activity is skipped
    - `enable_memory_dreaming` off means no processing
15. **Trace privacy:** an admin viewing a member's conversation trace sees
    handles and counts, never the entry text.

### Loop B: live (real LLM, real stack)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
```

1. Turn on `enable_memory_dreaming`. Take a screenshot of AI settings.
2. **Style.** Correct the agent twice ("shorter, number first", "use €M"). Confirm
   `create_memory` fires without being asked, and the entries appear in the
   profile UI with source links. In a **new report**, confirm the answers follow
   the style.
3. **Events.** "Board meeting next Thursday; I'm off the week after." Confirm two
   `events` entries with absolute dates. Move the clock forward, or edit the
   dates into the past, and confirm they leave the always tier.
4. **Vocabulary and matching.** "When I say my region I mean EMEA." In a new
   report, ask "revenue in my region". Confirm the entry is injected in the
   matched tier (visible in the trace) and the query filters to EMEA.
5. **Scale.** Seed 150 varied entries for one user. Confirm the injected block
   stays within budget, the index line is present, and `search_memory` finds a
   non-injected entry when the question needs it.
6. **Dream.** Run the sweep manually for the test org, without waiting for the
   night window (use a debug trigger or call the service). Confirm:
   - extraction from the day's sessions
   - a merge of two near-duplicates
   - no changes to user-typed entries
   - the run log appears in the trace
7. **No over-capture.** Run 10 one-off questions. Count the entries created by the
   agent and by the dream. The target is close to zero.
8. **Privacy.** As an admin, confirm there's no access to the member's memory in
   the API, the UI or the trace text.
9. Take before/after screenshots (ui-evidence skill). Record in the loop doc:
   - the final prompts
   - precision: the share of agent- and dream-created entries a human would keep
   - entries created per 10 turns
   - dream ops per user, tokens and cost
   - any wrong captures, with their traces

### Metrics

| Metric | Target |
|---|---|
| Precision: agent- and dream-created entries not deleted or edited by the user within 7 days | ≥ 80% |
| Repeated corrections after capture | Trending toward 0 |
| Injected memory size (p50 and p95) | Within budget |
| Matched-tier hit rate: turns where a matched entry was injected and the answer used it (spot-check) | |
| Dream cost per active user per night | |

## 15. Risks and decisions

- **Dream default:** off while in lab. Revisit after the Loop B precision numbers.
- **Explicit user-authored edits through chat:** the plan allows `edit_memory` on
  a user entry only when the user directly asks in that turn. Confirm the
  detection approach during implementation. If it's unreliable, always route
  users to the profile UI for their own entries.
- **Keyword matching is lexical.** "My region" matches because of aliases; purely
  semantic matches rely on `search_memory`. Consider embeddings only if Loop B
  shows misses that aliases and tags can't fix.
