# Feedback Loop — User memory: facts, not rules

Replaces the single 2,000-char `Membership.memory` document with one row per
fact about a user (`memory_entries`), three agent tools
(`create_memory` / `edit_memory` / `search_memory`), a tiered, fixed-budget
`<memory>` block, a one-click way for the agent to offer the user a personal
instruction (`suggest_personal_instruction`), a simple profile UI, owner-only
memory lines in the trace, and one org setting (`enable_user_memory`).

## The one rule

**Is it a rule for how to answer or compute, or a fact about the user?**

| | Where it goes | Who writes it |
|---|---|---|
| Rule that holds for everyone (definitions, metric logic, required filters, shared conventions) | Org instructions | Existing instruction flow / knowledge harness |
| Rule only this user wants (format, units, length, what to show) | The user's **Custom instructions** (`Membership.note`) | The agent *offers* it; the user accepts with one click |
| Fact about the user (work, projects, dates, what they follow, their own shorthand) | **Memory** | The agent, when it notices; the user in their profile |

Memory never holds a rule, so it can't conflict with instructions. Rules are
proposed, never saved silently. Nothing the agent writes to memory is
permanent: a dated fact ends after its date; an undated fact goes stale 90
days after it was last seen unless the user added or edited it (confirmed it).

## What changed (map)

| Area | Where |
|------|-------|
| Model + migration | `backend/app/models/memory_entry.py`, `backend/alembic/versions/usrmem01_add_memory_entries.py` (also `agent_executions.memory_context_json`) |
| Legacy split (idempotent): facts → memory, rules → Custom instructions | `backend/app/services/memory_migration.py` |
| Pure rules: rule-vs-fact test, expiry, normalization, secrets, fact/rule signals, note helpers | `backend/app/services/memory_rules.py` |
| Write/read path: facts only, dedupe, supersede, forget, cap, search | `backend/app/services/memory_service.py` |
| Tiered context builder | `backend/app/ai/context/builders/memory_context_builder.py`; shared matcher `backend/app/ai/context/keyword_match.py` |
| Tools | `backend/app/ai/tools/implementations/{create,edit,search}_memory.py`, `suggest_personal_instruction.py`, `_memory_common.py`; schemas `backend/app/ai/tools/schemas/memory.py` |
| Agent wiring | `backend/app/ai/agent_v2.py` (`_build_memory_block`, `_memory_hint`, catalog gating, trace stamp) |
| Prompt rules | `backend/app/ai/agents/planner/prompt_builder_v3.py`; boundary text in every memory tool and in `create_instruction` |
| User API | `backend/app/routes/user_memory.py` (`GET/POST/PATCH/DELETE /api/users/me/memory[/{id}]`), `POST /api/users/me/instructions/rules` in `user_profile.py` |
| Privacy | `backend/app/services/memory_privacy.py` (snapshot scrub), `backend/app/serializers/completion_v2.py` (payload redaction) |
| Trace | `ConversationTurnSchema.memory`, `ConsoleService._turn_memory_sections`, `frontend/components/console/TraceModal.vue` |
| UI | `frontend/components/profile/UserMemoryPanel.vue` (flat list, search, tag chips), `MemoryEntryForm.vue`, `frontend/components/tools/{MemoryTool,SuggestInstructionTool}.vue` |
| i18n | all 10 catalogs: `profile.memory.*`, `tools.memory.*`, `tools.suggestInstruction.*`, `traceModal.memory.*`, `errors.memory.*`, `errors.profile.instructions_full` |

## Environment

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

## Loop A — deterministic (no real LLM)

The LLM is stubbed at `AgentV2.main_execution` (the real completion path runs;
the stub reads the agent's catalog, rendered `<memory>` block and hint, and
calls the real tools); the clock is passed as `now=`.

```bash
cd backend
uv run pytest tests/unit/test_memory_rules.py tests/unit/test_memory_context_builder.py \
  tests/unit/test_memory_service.py tests/unit/test_memory_tools.py tests/unit/test_memory_migration.py \
  tests/unit/test_memory_privacy.py tests/unit/test_prompt_builder_v3_user_profile.py \
  tests/unit/test_batch_observation.py tests/e2e/test_memory.py -q
# Postgres (no Docker here → local PG 16 via --db=external)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bow_test BOW_DATABASE_URL=$TEST_DATABASE_URL \
uv run pytest --db=external tests/unit/test_memory_service.py tests/unit/test_memory_tools.py \
  tests/unit/test_memory_migration.py tests/e2e/test_memory.py -q
```

Observed: **205 passed** on SQLite, **64 passed** on Postgres.

What they pin:
- **Rule vs fact** — 20 rule phrasings (definitions, filters, conventions,
  format specs, "prefers the number first", "Prefers USD") are refused on
  every write, agent *and* user; 14 fact phrasings, including the user's own
  shorthand, are accepted.
- **Signals** — fact signals (role, shorthand, a date, a project, what they
  track) and rule signals (how they want answers) are told apart; ordinary
  questions carry neither.
- **Expiry** — dated facts end a day after their date (date-only = end of
  day; ranges visible throughout); undated agent facts go stale after 90 days
  since last seen and come back when seen again; user-confirmed facts don't
  expire; explicit `expires_at` wins.
- **Dedupe / supersede / forget / cap / concurrency** — as before: variants
  strengthen one entry, a date given later merges in, shared aliases merge,
  the 201st write evicts the weakest non-user entry, 6 concurrent writers all
  persist.
- **Builder** — budgets at 0/10/200 facts; dated facts in the window first,
  then the strongest undated ones; alias and object-tag matches; index line
  counts what's hidden; header says there are no rules in memory.
- **Suggestion** — `suggest_personal_instruction` saves nothing, reports when
  the rule is already in the user's Custom instructions, is unavailable on
  machine turns; the accept endpoint appends once, only to the caller's own
  instructions, and returns `profile.instructions_full` at the cap.
- **Migration** — facts → entries, rules → Custom instructions (bullets,
  deduped, within 500 chars), rules that don't fit are logged; a re-run
  changes nothing.
- **E2E** — setting off removes block, tools and API (403), entries survive;
  own-memory-only API; membership removal deletes memory; the agent saves a
  fact with evidence and the next turn sees it; a style correction makes the
  hint point at `suggest_personal_instruction` and never at memory; machine
  turns get the block but no tools; trace text only for the owner.

Every guard's test fails when that guard is removed (checked one at a time,
then restored): rule check, catalog gating (setting and machine turns),
injection gate, dedupe, cap, owner-only trace text, user-entry protection,
entry scoping, snapshot scrub, payload redaction, note dedupe, migration
routing, rule hint.

## Loop B — live (`gpt-6-luna`, real stack, Playwright as a user)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo   # + members via the API
```

Sandbox notes: set a stable `BOW_ENCRYPTION_KEY` before booting, and when
restarting the backend kill uvicorn's spawned workers too.

| Scenario | What the user did | Observed | |
|---|---|---|---|
| **Rule** | "Way too long. From now on: the number first, then one line of context, and money in thousands…" | `suggest_personal_instruction` → card "Save as your instruction?"; one click → added to Custom instructions; answer redone ("$0.5K — Top five…"). Memory stayed empty (a parallel `create_memory` attempt was refused by the rule check and hidden from the report) | ✅ |
| **Rule + fact in one message** | "Too long — from now on just the numbers first… Also, I'm leading the APAC launch next month." | Rule → suggestion card; fact → `create_memory` ("Leading the APAC launch next month"); answer "13 USA · 8 Canada · 5 France." | ✅ |
| **Dated facts** | "Board meeting next Thursday, I'm off the week after, and I now track weekly revenue for Germany…" | Three facts: board meeting 2026-10-01, off 10-05 → 10-11, tracks weekly revenue for Germany | ✅ |
| **Shorthand** | Admin: "When I say my region, I mean Germany…", then a new report "revenue in my region by year?" | Fact saved; next report injected it and filtered to Germany (owner trace shows it) | ✅ |
| **Definitions (org rules)** | 5 reports opening with a definition ("Active customers are…", "Revenue means…", "Always exclude…", …) | 0 memory writes, 0 personal-instruction suggestions — they are org rules | ✅ 0 leaks |
| **No over-capture** | 10 one-off questions | 0 memory writes, 0 suggestions | ✅ |
| **Privacy** | Admin opens a member's conversation trace | Handles/tiers only; none of the member's 6 fact texts in any admin payload | ✅ |
| **Migration** | Member's old 4-line document | "Prefers the number first…", "Amounts in €M…" → Custom instructions; "Presents to the CFO monthly", "Leads the Q3 churn project" → memory | ✅ |
| **Scale** (earlier run, same builder) | 155 facts | Injected block 1,478 chars (budget ~2,200) | ✅ |

Observed and not fixed (model behavior, not the feature): after accepting
"money in thousands with one decimal", a later answer rendered $481.45 as
"$481.5K" — a wrong application of the user's own instruction, the same as if
they had typed it in Custom instructions. Also pre-existing: answers with
several `$` amounts render as inline math (markstream-vue).

## Findings fixed along the way

1. The agent didn't save facts on its own → a code-driven `<memory_hint>` next
   to the ask when the message carries fact or rule signals.
2. "Got it" replies to corrections → the hint says to redo the answer.
3. Two facts merged into one entry → `edit_memory` refuses appending a
   different fact (`memory.one_fact_per_entry`).
4. Memory text leaked into admin-visible context snapshots → scrubbed before
   persisting.
5. A turn-end write through a second session self-deadlocked → the trace is
   set on the run's own execution object.
6. **Review:** memory had absorbed style/format rules. Reworked to the one
   rule above: sections dropped, memory refuses rules on every write, rules
   are offered as personal instructions, the UI is one list with search and
   tag chips.
7. A refused memory call showed "Couldn't update memory" in the report →
   failed memory calls are hidden there (kept in the trace).

## Not done / follow-ups

- `docs/design/agent-checkins.md` doesn't exist, so check-ins aren't wired;
  machine turns already get `<memory>` without the tools.
- No user data export exists to extend.
- `search_memory` is lexical; a paraphrase with no shared words can miss.
- `Membership.memory` stays read-only for one release; drop it after.
- Pre-existing, unrelated: `tools/agent/seed_org.py --invite` 422;
  `test_sharepoint_onprem_client.py` / `test_discovery_progress_contract.py`
  fail on the base commit too.

## UI evidence (`media/pr/ai-brave-cori-37ujt9/`)

| | |
|---|---|
| Before: one free-text memory box | `01-before-profile-memory.png` |
| Flow: rule → one-click instruction, fact → memory | `02-flow-rule-to-instructions-fact-to-memory.gif` |
| One turn: suggestion card + memory status line + redone answer | `03-rule-card-and-fact-in-one-turn.png` |
| Suggestion accepted | `04-suggestion-accepted.png` |
| The accepted rule in Custom instructions | `05-custom-instructions-after-accept.png` |
| Memory: one flat list with dates and tags | `06-memory-flat-list.png` |
| Search | `07-memory-search.png` |
| Tag filter | `08-memory-tag-filter.png` |
| Hebrew (RTL) | `09-memory-list-he-rtl.png` |
| Trace, owner view | `10-trace-owner-view.png` |
| Trace, admin viewing a member (private) | `11-trace-admin-private.png` |
| Org setting | `12-org-setting.png` |
