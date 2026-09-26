# User memory: entries, tools and context builder

Status: **plan** (nothing implemented yet). Build it as a sandbox feedback loop
(`.agents/skills/sandbox-feedback-loop/SKILL.md`). The loop report goes in
`docs/feedback-loops/user-memory.md`.

Related plan: `docs/design/agent-checkins.md`. Check-ins **read** memory; memory
never depends on check-ins (§11).

## 1. What we are building

- **Memory entries.** One row per durable, **personal** fact about a user. Each
  entry has a section, tags, optional dates, and provenance (the report, the turn,
  and a short quote). Entries are private to that user.
- **Three agent tools**, named like the existing instruction and note tools:
  `create_memory`, `edit_memory` and `search_memory`. The agent saves memory
  **whenever it notices** something durable and personal, not only when asked.
- **A tiered memory context builder** with fixed budgets, however many entries a
  user has:
  - **always:** style, role, preferences and upcoming events
  - **matched:** entries whose tags or aliases match the current prompt or report
  - **index line:** a summary of what isn't shown
  - **`search_memory`:** for everything else
- **A memory UI in the user's profile.** The user can view, edit, add and delete
  entries, see where each one came from, and forget everything.
- **One org setting**, `enable_user_memory`, which gates every step.

### Out of scope for now

- Any background or nightly processing of memory. Memory is written only by the
  agent during a turn and by the user in the UI.
- A calendar or email integration. Events come only from conversations.
- Admin access to user memory (by design, §10).

## 2. Memory is not instructions

These are two separate systems that cover different things. They don't overlap,
so they can't conflict.

| | **Instructions** | **Memory** |
|---|---|---|
| What it is | The **semantic layer and agent rules**: business definitions, metric logic, table and column meaning, join rules, and how the agent must behave for the org | **Personal context about one user**: who they are, how they like to work and communicate, their own shorthand, their dates |
| Who it applies to | Everyone in the org (or everyone using a data source or agent) | Only that user |
| Who writes it | Admins and users through the instructions flow, the knowledge harness and training, with review and builds | The agent, when it notices something personal, and the user in their profile |
| Examples | "Active customer = paid invoice in the last 90 days" · "Revenue means net revenue excluding VAT" · "Always filter out test accounts" · "Use `orders.created_at` for order date" | "Prefers the number first, then one line of context" · "Presents to the CFO monthly" · "'my region' = EMEA" · "Board meeting Thu 2026-10-09" · "Out of office 2026-10-13 → 10-17" |
| Can it change what numbers mean? | **Yes.** That is its purpose | **Never.** Memory changes framing, format, timing and the user's personal shortcuts, not business logic |

**Routing rule for the agent**, which goes in the prompt and the tool
descriptions:
- If something would be true or required **for anyone** asking (a definition, a
  metric rule, a filter everyone must apply, how a table works), it belongs in
  **instructions**. Use the existing instruction flow; the knowledge harness
  already captures these. **Never** store it in memory.
- If it is about **this person** (their style, role, schedule, focus, or their
  own shorthand), it belongs in **memory**.
- **Personal shorthand vs a definition:** "when I say *my region* I mean EMEA" is
  memory, because it maps *their* words to a value. "EMEA includes Turkey" is an
  instruction, because it defines the business entity for everyone.

**Enforcement:**
- The tool descriptions for `create_memory` and `create_instruction` each state
  the boundary and point at the other tool.
- `create_memory` refuses text that reads like a rule or definition: it returns an
  error telling the agent to use the instruction flow instead. Use a light
  heuristic (for example "X means / is defined as / must / always filter") plus the
  prompt rule, and record every refusal in the trace so the heuristic can be
  tuned.
- The memory block header says what it is (§6), so the model never treats it as
  rules.

## 3. What goes into memory

| Section | Content | Example |
|---|---|---|
| `style` | Writing and output style | "Prefers the number first, then one line of context"; "Amounts in €M, one decimal"; "Writes for execs: bullet summaries" |
| `role` | Role and work context | "Finance, owns EMEA revenue reporting; presents to the CFO monthly" |
| `vocabulary` | **Their personal shorthand** (never business definitions) | "'my region' = EMEA"; "'the board deck' = report *Q3 Board Pack*" |
| `events` | Dated items in their work life: meetings, deadlines, reviews, travel, time off | "Board meeting Thu 2026-10-09"; "Out of office 2026-10-13 → 10-17" |
| `focus` | What they are working on now | "Investigating Q3 churn (since 2026-09-20)" |
| `preferences` | How they like to work with the agent | "Wants the SQL shown"; "Asks before running expensive queries" |

**Never stored in memory:**
- business definitions, metric logic, rules or required filters (these are
  instructions, §2)
- query results or data values
- secrets or credentials
- facts about other people
- health or other personal details. Events are limited to work availability.

## 4. Current state (checked against the code)

| What | Where |
|---|---|
| `Membership.memory` (one text field per user per org), 2,000-char cap | `backend/app/models/membership.py:17-25`; `MEMBERSHIP_MEMORY_MAX_LENGTH` in `backend/app/schemas/organization_schema.py:15` |
| `update_user_memory` tool: rewrites the whole document, `allowed_modes=["chat"]` | `backend/app/ai/tools/implementations/update_user_memory.py`; schema in `backend/app/ai/tools/schemas/update_user_memory.py` |
| Loading memory | `_resolve_user_profile()`, `backend/app/ai/agent_v2.py:979-1010` |
| Injection as `<user_memory>` in the per-turn user message (outside the cached prefix) | `PromptBuilderV3._format_user_memory`, `backend/app/ai/agents/planner/prompt_builder_v3.py:594-607`, used at `:855` |
| Current memory prompt rule | `prompt_builder_v3.py:429` |
| Profile API (GET/PUT the memory text) | `backend/app/routes/user_profile.py:38-100` |
| Profile UI (one textarea) | `frontend/components/UserProfileModal.vue:253-262, 743-766` |
| Tool card in the report timeline | `frontend/pages/reports/[id]/index.vue`, `frontend/pages/c/[token]/index.vue` |
| Tool naming convention to mirror | `ai/tools/implementations/{create,edit,search,read}_instruction.py`, `{create,edit}_note.py` |
| Keyword matching to reuse | `backend/app/ai/context/builders/instruction_context_builder.py` (`_extract_keywords`, `search_instructions` `:377`, `build` `:481`) |
| Trace UI | `frontend/components/console/TraceModal.vue`; `ConversationTraceResponse` in `backend/app/schemas/agent_execution_trace_schema.py:88`; builder in `backend/app/services/console_service.py:2078-2356` |
| Org settings pattern | `backend/app/schemas/organization_settings_schema.py:340-360`; names and descriptions in `locales/*.json` |

What's wrong with today's design:
- The full rewrite loses data when sessions run in parallel, and the model drops
  lines when it prunes.
- Memory is saved only when the user explicitly asks.
- There's no provenance, no dates, and no expiry.
- A single 2k document can't grow.
- The rule and the tool don't separate personal context from definitions.

## 5. Setting (Phase 1)

Add one field to `OrganizationSettingsConfig`:

```python
enable_user_memory: FeatureConfig = FeatureConfig(
    value=True, name="User memory",
    description="Let the agent remember personal context about each user across "
                "sessions — writing style, role, their own shorthand, upcoming "
                "meetings and events, current focus. Business definitions and rules "
                "stay in instructions. Each user can see, edit and delete their memory.",
    is_lab=False, editable=True)
```

- It defaults to **on**, because memory exists today.
- Add the name and description to **every** `locales/*.json` catalog. The
  catalogs must keep an identical shape. Hebrew follows the vocabulary rules in
  `AGENTS.md`.

The setting gates every step:

| Point | When `enable_user_memory` is off |
|---|---|
| Memory context injection | No `<memory>` block |
| `create_memory`, `edit_memory`, `search_memory` | Removed from the tool catalog |
| User memory API | Returns 403 with a typed error code |
| Profile UI memory section | Hidden |
| Check-in planner and judge reading memory | Not included |
| Stored entries | **Kept.** They come back when the setting is turned on again |

## 6. Data model (Phase 1)

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
| `text` | text, at most 280 chars | One declarative, personal fact. Never an imperative, never a definition |
| `tags` | JSON list | 1–4 normalized slugs (§8) |
| `aliases` | JSON list, nullable | Other words the user uses for the same thing (vocabulary, focus) |
| `event_start` / `event_end` | datetime, nullable | `event_start` is required for `events` |
| `expires_at` | datetime, nullable | When empty, the defaults in §9 apply |
| `source` | str(12) | `user` (typed in the UI), `agent` (the in-turn tool), `migration` |
| `evidence` | JSON, nullable | `{report_id, completion_id, quote}`. The quote is at most 200 chars of the user's own words and is filled by code, never by the model |
| `seen_count` | int, default 1 | |
| `last_seen_at` | datetime | |
| `status` | str(12), indexed | `active`, `superseded`, `forgotten` |
| `superseded_by_id` | str(36), nullable | |
| `created_at` / `updated_at` | | From `BaseSchema` |

Rules:
- **Entries the user typed (`source='user'`) are changed only by the user**, with
  one exception: the user directly asks the agent in that turn (§7).
- `forgotten` blanks `text`, `aliases`, `tags` and `evidence`. Only the id, status
  and timestamps are kept.
- Deleting a membership deletes its entries. User data exports include them.

**Migration:**
- Every non-empty `Membership.memory` becomes one entry per line or bullet, with
  `section='preferences'`, `source='migration'` and no tags.
- It must be idempotent.
- Keep `Membership.memory` read-only for one release so the change can be rolled
  back, then drop it.
- Existing lines that read like business definitions stay as migrated entries.
  Log them in the migration output so they can be reviewed and moved to
  instructions by hand. Don't convert them automatically.

## 7. Tools (Phase 2)

These replace `update_user_memory`.

**How the tools look in the report while the agent runs: a single line, nothing
to expand.** Reuse the existing `frontend/components/tools/UpdateUserMemoryTool.vue`
pattern for all three tools. That component is already a non-expandable single
line that shows the agent's `title` and never the memory content.

- **While running:** a bookmark icon with a shimmer, plus the tool's dynamic
  `title`. For example: "Updating memory: prefers the number first",
  "Remembering your board meeting", "Checking what I know about your region".
  Fallbacks when `title` is empty:
  - `create_memory` / `edit_memory`: "Updating memory…"
  - `search_memory`: "Checking memory…"
- **Done:** the same line, static, with the `title` (or "Memory updated" /
  "Checked memory").
- **Failed:** "Couldn't update memory" with an error icon. There's no detail, and
  the agent handles the error itself.
- **Never shown:** entry text, handles, tags, search results, or a
  dedupe/refusal explanation. Details live in the profile UI and the trace.
- **Wiring:** map `create_memory`, `edit_memory` and `search_memory` to this
  component in `getToolComponent`, in `frontend/pages/reports/[id]/index.vue`
  (~`:2576`) and `frontend/pages/c/[token]/index.vue` (~`:683`). Keep
  `update_user_memory` mapped too, so old reports still render. Rename the
  component (e.g. `MemoryTool.vue`) and pick the running/done/fallback strings by
  `tool_name`. Add the i18n keys under `tools.memory.*` in every
  `locales/*.json`.
- Every tool schema's `title` description tells the model to write a short,
  friendly, 3–7 word status in the user's language, with no private details
  beyond what the user just said.

The tools are available on **human-initiated turns in every channel** (web,
Slack, Teams, email). They are **not** available in training mode or in machine
turns (`trigger_source` set).

### `create_memory`
```python
class CreateMemoryInput(BaseModel):
    text: str                         # ≤280, declarative, personal
    section: Literal["style","role","vocabulary","events","focus","preferences"]
    tags: List[str]                   # 1..4; reuse tags shown in the <memory> index
    aliases: Optional[List[str]] = None
    event_start: Optional[str] = None # ISO; required for section="events"
    event_end: Optional[str] = None
    expires_at: Optional[str] = None
    title: Optional[str] = None       # status line
```
- **Description:** "Save a personal fact about the current user: their style,
  role, schedule, focus, or their own shorthand. NOT for business definitions,
  metric logic or rules; those belong in instructions (`create_instruction`)."
- **Validation:**
  - events require a date
  - text is at most 280 chars
  - 1–4 tags, normalized
  - the definition/rule heuristic (§2) returns an error that points to
    instructions
  - the sensitive-content filter (§9)
- **Dedupe (§9):** a matching entry is strengthened instead of duplicated. The
  tool returns `{"deduped_into": "m7"}`.
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
  - a `source='user'` entry, unless the user's message in this turn directly asks
    for the change ("forget that I…", "change my…"). If that's ambiguous, refuse
    and tell the agent to point the user to their profile.

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
  and the source report title and link.
- It uses the same keyword and alias scoring as the context builder (§8).
- It excludes entries already injected in this turn, and says so in its output.

There's no `read_memory`. Entries are at most 280 chars, and `search_memory`
returns them in full.

### Prompt rules

Replace the rule at `prompt_builder_v3.py:429` with:

- `<memory>` is personal context about this user. It is **not** business logic.
  Definitions, metric rules and required filters come only from `<instructions>`.
- **Save when you notice**, not only when asked. Clear signals:
  - a correction of your style or format
  - a stated role or responsibility
  - their personal shorthand
  - a dated meeting, deadline or time off (resolve the date to an absolute ISO
    date)
  - what they're working on now
- Don't save:
  - business definitions or rules. Those are instructions.
  - one-off task details
  - data values or results
  - anything about other people
  - sensitive details
- Before creating an entry, check the memory shown in context. If a matching
  entry exists, `edit_memory` it rather than creating a duplicate. Reuse the
  existing tags.
- Use `search_memory` when the user refers to something personal the injected
  memory doesn't cover ("like last time", "my usual format for…").
- Write declarative facts ("Prefers…"), never imperatives.

## 8. Context builder (Phase 1)

New `backend/app/ai/context/builders/memory_context_builder.py`. It is pure code
with no LLM calls. It **reuses** `_extract_keywords` and the scoring approach from
`instruction_context_builder.py`; extract a shared helper if needed.

**Relevance signals for the current turn:**
- keywords from the current and previous user prompts
- the report title
- the ids of the report's agents and data sources

**Tags:**
- **Topic tags:** lowercase slugs such as `emea`, `board-deck`, `churn`. Writers
  see the user's existing tags (with counts) in the index line and are told to
  reuse them. Code normalizes them (lowercase, hyphens) and merges exact slug
  matches.
- **Object tags:** `agent:<id>`, `data_source:<id>`, `report:<id>`. An entry that
  carries one is matched whenever that object is part of the turn. For example,
  "prefers weekly granularity when using the Sales agent" is tagged
  `agent:<sales>`.

**Tiers and budgets**, sized so the rendered block is at most about 2,200 chars:

| Tier | What goes in it | Budget | Order within the tier |
|---|---|---|---|
| **Always** | `style`, `role`, `preferences`, plus events that start within the next 21 days or ended within the last 2 days | ~1,200 chars | Events by date. Otherwise `source='user'` first, then higher `seen_count`, then most recent `last_seen_at` |
| **Matched** | Other active entries (vocabulary, focus, older preferences) whose text, alias or tags overlap the turn's keywords, **or** that carry an object tag present in the turn | ~800 chars, at most 10 entries | Relevance score, with an object-tag match weighted highest |
| **Index line** | A summary of what isn't shown: counts per section and the top tags | ~200 chars | e.g. `Also remembered (not shown): 14 vocabulary, 3 focus, 9 preferences · tags: emea, board-deck, churn — use search_memory.` |

**Render format.** It is injected as `<memory>` in the same position as today's
`<user_memory>`: the per-turn user message, outside the cached prefix.

```
<memory>
(Personal context about {name}: style, role, schedule, their own shorthand.
Business definitions and rules are in <instructions>, not here.)
[m3] style: Prefers the number first, then one line of context.
[m5] role: Finance, owns EMEA revenue reporting.
[m12] event: Thu 2026-10-09 (in 3 days): board meeting.
[m21] vocabulary: "my region" = EMEA.   ← matched: "region"
Also remembered (not shown): …
</memory>
```

- Load entries in `_resolve_user_profile` (`agent_v2.py:979`) instead of reading
  `Membership.memory`.
- Scheduled runs, wait wakes, check-in runs and Slack/Teams turns all run as the
  user. Verify they go through this path and see the memory.

## 9. Keeping memory clean (Phase 1: pure code)

This lives in `backend/app/services/memory_service.py`, and every writer uses it:
the tools, the UI API and the migration.

- **Dedupe on write:**
  - Normalize the text (lowercase, collapsed whitespace, no punctuation).
  - Same user, same section and same normalized text: strengthen the existing
    entry (`seen_count`, `last_seen_at`, merged aliases) instead of inserting.
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
- **Definition/rule heuristic** (§2): applied to agent writes only, not to what
  the user types in the UI.

## 10. User API, UI, trace and privacy

**API (Phase 2)**, for the current user only. It replaces the memory text field
in `routes/user_profile.py`:
- `GET /api/users/me/memory`: entries grouped by section, with tags, evidence
  (report title and link) and the rendered preview.
- `POST /api/users/me/memory`: create an entry (`source='user'`).
- `PATCH /api/users/me/memory/{id}`: update. The old version is superseded.
- `DELETE /api/users/me/memory/{id}`: forget an entry.
- `DELETE /api/users/me/memory`: forget everything.

There is **no admin endpoint**. Other members and admins get 403 or 404.

**`UserProfileModal.vue` memory section (Phase 2)**, replacing the textarea:
- Entries are grouped by section. Each row shows:
  - the text
  - dates, for events
  - tag chips
  - a source icon (you / agent)
  - "from *Report*", linking to the report
  - edit and delete actions
- An "Add" action per section. The events form has date inputs.
- A filter by tag.
- "Forget everything", behind a confirmation.
- A collapsed "What the agent sees" preview of the always tier plus the index
  line.
- One line of help text explaining the difference from instructions, with a link
  to the instructions page.

**TraceModal (Phase 3)**, via `ConversationTraceResponse` and its builder in
`console_service.py`. Per turn it shows:
- the memory entries injected, with handles, tiers and rendered size
- `create_memory`, `edit_memory` and `search_memory` calls, shown as normal tool
  executions
- refusals from the definition/rule heuristic, including the refused text

The entry **text** appears only when the viewer owns the memory. Everyone else,
admins included, sees handles, sections and counts only.

**Privacy:**
- User memory is private, and there is no admin read path.
- Memory is injected as personal context, and the header states that it holds no
  rules.
- The sensitive-content filter runs on every write, and the prompts exclude
  personal details beyond work availability.
- Deleting a membership deletes its memory, and user data exports include it.

All strings go into every `locales/*.json`. Check the layout in RTL (`he`). Any UI
change needs before/after evidence captured with the **ui-evidence** skill.

## 11. Relationship to check-ins

**The link is one-way: check-ins read memory.**

- The check-in planner and judge get the rendered always tier, mainly for events,
  so follow-ups can land around the user's meetings and avoid their time off.
  This is gated by `enable_user_memory`. It is a small addition to
  `agent-checkins.md` (§6 planner input, §9 judge input).
- Check-in runs see memory automatically, because they run as the user.
- **Check-ins never write memory.** Check-in runs are machine turns, and the memory
  tools are unavailable in machine turns.

## 12. Phases and exit criteria

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | The `enable_user_memory` setting and locales; the `memory_entries` model and migration; migration of existing memory text; `MemoryService` (dedupe, expiry, cap, filters); `MemoryContextBuilder` (tiers, tags, keyword matching); injection switched over | Unit tests pass on sqlite and postgres. Migrated memory appears in the always tier. Turning the setting off stops injection. Budgets hold with 200 entries |
| 2 | `create_memory`, `edit_memory`, `search_memory` (replacing `update_user_memory`); prompt rules and tool descriptions carrying the memory vs instructions boundary; user API and profile UI; single-line memory tool status in the report | The agent saves on corrections without being asked. It routes definitions to instructions, not memory. Parallel writes don't lose entries. User-authored entries are protected. Admins get 403 |
| 3 | TraceModal memory lines; tuning from Loop B | The trace shows injection and tool activity, and the privacy rule holds |

## 13. Feedback loop (to run in the new session)

Follow `.agents/skills/sandbox-feedback-loop/SKILL.md`.

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

### Loop A: deterministic (no real LLM)

Stub only at the boundaries: the LLM and the clock. Seed data through
`tests/fixtures/*`. Each test must first be watched failing (stash the
implementation), per rule 6 in `backend/tests/AGENTS.md`.

Unit tests (`backend/tests/unit/test_memory_service.py`,
`test_memory_context_builder.py`, `test_memory_tools.py`):

1. **Dedupe.**
   - Normalization variants strengthen the existing entry.
   - A different section inserts a new one.
   - A vocabulary alias match merges.
2. **Expiry.** Vary the dates in each case.
   - An event is hidden 1 day after its end.
   - A range event stays visible for its whole duration.
   - Focus expires 30 days after last seen, and comes back when seen again.
3. **Cap.** The 201st write evicts the lowest-ranked non-user entry. User entries
   are never evicted.
4. **Context builder.**
   - Budgets hold with 0, 10 and 200 entries.
   - The always tier includes style, role, preferences and events in the window
     only.
   - An alias match puts the vocabulary entry in the matched tier.
   - An object tag (`agent:<id>`) matches when that agent is in the report.
   - The index line counts what was left out.
   - Handles are stable across renders.
   - The header states that memory holds no definitions or rules.
5. **Tag normalization.** `Board Deck` and `board_deck` both become `board-deck`,
   and exact slugs merge.
6. **Tool validation.** Each of these is rejected:
   - an event without a date
   - more than 4 tags
   - text over 280 chars
   - an unknown handle
   - `edit_memory` on a `source='user'` entry without a direct request
7. **Memory vs instructions boundary.** `create_memory` rejects texts that read
   like definitions or rules, with an error pointing to instructions. Personal
   shorthand ("when I say my region I mean EMEA") is accepted. Vary the phrasings;
   don't test only one pair.
8. **Migration.** Idempotent. Multi-line text becomes N entries. Lines that look
   like definitions are logged.

E2E tests (`backend/tests/e2e/test_memory.py`, run with both `--db=sqlite` and
`--db=postgres`):

9. **Setting off.**
   - No `<memory>` block in the planner input (checked through the context
     snapshot).
   - The tools are absent from the catalog.
   - The API returns 403.
   - Entries survive turning the setting back on.
10. **The API only reaches the user's own memory.** A member can create, read,
    update and delete their own entries. **Another member and an org admin**
    cannot read or change them.
11. **Parallel writes.** Two concurrent `create_memory` calls from different
    reports both persist.
12. **The agent saves on noticing.** A stubbed agent calls `create_memory` after a
    style correction. The entry has `source='agent'` and evidence pointing to the
    turn. The next turn's `<memory>` contains it.
13. **`search_memory`** returns matching entries that weren't already injected,
    with handles and source links.
14. **Machine turns.** Wait wakes, scheduled runs and check-in runs have no memory
    tools, but **do** receive the `<memory>` block.
15. **Trace privacy.** An admin viewing a member's conversation trace sees handles
    and counts, never entry text.

### Loop B: live (real LLM, real stack)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
```

1. **Style.**
   - Correct the agent twice ("shorter, number first", "use €M").
   - Confirm `create_memory` fires without being asked, and the entries appear in
     the profile UI with source links.
   - In a **new report**, confirm answers follow the style.
2. **Events.**
   - Say "Board meeting next Thursday; I'm off the week after."
   - Confirm two `events` entries with absolute dates.
   - Move the clock forward (or edit the dates into the past) and confirm they
     leave the always tier.
3. **Vocabulary and matching.**
   - Say "When I say my region I mean EMEA."
   - In a new report, ask "revenue in my region".
   - Confirm the entry is injected in the matched tier (visible in the trace) and
     the query filters to EMEA.
4. **Boundary.**
   - Say "Active customers are those who paid in the last 90 days."
   - Confirm **no** memory entry is created, and the knowledge harness or
     instruction flow handles it instead.
   - Repeat with 3–5 other definition-style statements and record any leaks into
     memory.
5. **Scale.**
   - Seed 150 varied entries for one user.
   - Confirm the injected block stays within budget and the index line is
     present.
   - Confirm `search_memory` finds a non-injected entry when the question needs
     it.
6. **No over-capture.** Run 10 one-off questions and count the entries created.
   The target is close to zero.
7. **Privacy.** As an admin, confirm there's no access to the member's memory in
   the API, the UI or the trace text.
8. **Evidence and record.**
   - Capture before/after screenshots with the ui-evidence skill.
   - In the loop doc, record: the final prompts and tool descriptions; precision
     (the share of agent-created entries a human would keep); entries per 10
     turns; boundary leaks; any wrong captures, with their traces.

### Metrics

| Metric | Target |
|---|---|
| Precision: agent-created entries the user hasn't deleted or edited within 7 days | ≥ 80% |
| Boundary leaks: definitions or rules found in memory | 0 in Loop B |
| Repeated style corrections after capture | Trending toward 0 |
| Injected memory size (p50 and p95) | Within budget |

## 14. Risks and decisions

- **Definition/rule heuristic.** Too strict and it blocks legitimate personal
  shorthand. Too loose and definitions leak into memory. Tune it from the trace's
  refusal log and the Loop B boundary tests. The prompt rule is the primary guard;
  the heuristic is a backstop.
- **Editing user-authored entries through chat.** The agent may change one only
  when the user directly asks in that turn. If detecting that proves unreliable,
  always send users to their profile to edit their own entries.
- **Keyword matching is lexical.** "My region" matches because of aliases, while
  semantic matches rely on `search_memory`. Consider embeddings only if Loop B
  shows misses that aliases and tags can't fix.
- **No background processing.** Memory quality depends entirely on the agent
  saving and deduping well within turns. If Loop B shows near-duplicates piling
  up, a background consolidation pass is the next plan.
