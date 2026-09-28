# Feedback Loop — User memory: facts, not rules

Replaces the single 2,000-char `Membership.memory` document with one row per
fact about a user (`memory_entries`), three agent tools
(`create_memory` / `edit_memory` / `search_memory`), a tiered, fixed-budget
`<memory>` block, a simple profile UI, owner-only memory lines in the trace,
and one org setting (`enable_user_memory`). The old column, the
`update_user_memory` tool and `<user_memory>` are removed outright (never
released, so there is no migration of old text).

## The one rule

**Is it a rule for how to answer or compute, or a fact about the user?**

| | Where it goes | Who writes it |
|---|---|---|
| Fact about the user (work, projects, dates, what they follow, their own shorthand) | **Memory** | The agent, when it notices; the user in their profile |
| Rule said in chat (format, units, length, a definition, a filter) | **Nowhere** — the agent applies it for the rest of that conversation, starting with the current answer | — |
| Rule that should last for everyone | Org instructions, through the existing flow (`create_instruction` in training/knowledge mode → draft → admin review) | Unchanged |
| Rule that should last for one user | Their Custom instructions (`Membership.note`) | Only the user, by typing it; the agent never writes there |

Memory never holds a rule, so it can't conflict with instructions, and only a
person creates a rule that persists. Nothing the agent writes to memory is
permanent: a dated fact ends after its date; an undated fact goes stale 90
days after it was last seen unless the user added or edited it (confirmed it).

Trade-off, on purpose: a style correction made in chat does not carry into
the next conversation unless the user puts it in Custom instructions.

## How it is stored

One row per fact in `memory_entries` (org + user scoped): `seq`/`handle`
(`m7`), `text` (≤280), `tags`, `aliases`, `event_start`/`event_end`,
`expires_at`, `source` (`user` | `agent`), `evidence` (report, completion,
the user's quote), `seen_count`/`last_seen_at`, `status` (`active` |
`forgotten`). An edit changes the row in place (same handle); forget blanks
the content; expiry is computed on read; 200 active entries per user, the
weakest agent entry is evicted first, never a user one.
`agent_executions.memory_context_json` records what each run injected, for
the trace.

## What changed (map)

| Area | Where |
|------|-------|
| Model + migration | `backend/app/models/memory_entry.py`, `backend/alembic/versions/usrmem01_add_memory_entries.py` (adds `agent_executions.memory_context_json`, drops `memberships.memory`) |
| Pure rules: rule-vs-fact test, expiry, normalization, secrets, fact/rule signals | `backend/app/services/memory_rules.py` |
| Write/read path: facts only, dedupe, in-place edit, forget, cap, search | `backend/app/services/memory_service.py` |
| Tiered context builder | `backend/app/ai/context/builders/memory_context_builder.py`; shared matcher `backend/app/ai/context/keyword_match.py` |
| Tools | `backend/app/ai/tools/implementations/{create,edit,search}_memory.py`, `_memory_common.py`; schemas `backend/app/ai/tools/schemas/memory.py` |
| Agent wiring | `backend/app/ai/agent_v2.py` (`_build_memory_block`, `_memory_hint`, catalog gating, trace stamp) |
| Prompt rules | `backend/app/ai/agents/planner/prompt_builder_v3.py`; boundary text in every memory tool and in `create_instruction` |
| User API | `backend/app/routes/user_memory.py` (`GET/POST/PATCH/DELETE /api/users/me/memory[/{id}]`) |
| Privacy | `backend/app/services/memory_privacy.py` (snapshot scrub), `backend/app/serializers/completion_v2.py` (payload redaction) |
| Trace | `ConversationTurnSchema.memory`, `ConsoleService._turn_memory_sections`, `frontend/components/console/TraceModal.vue` |
| UI | `frontend/components/profile/UserMemoryPanel.vue` (flat list, search, tag chips), `MemoryEntryForm.vue`, `frontend/components/tools/MemoryTool.vue` |
| i18n | all 10 catalogs: `profile.memory.*`, `tools.memory.*`, `traceModal.memory.*`, `errors.memory.*` |

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
  tests/unit/test_memory_service.py tests/unit/test_memory_tools.py \
  tests/unit/test_memory_privacy.py tests/unit/test_prompt_builder_v3_user_profile.py \
  tests/unit/test_batch_observation.py tests/e2e/test_memory.py -q
# Postgres (no Docker here → local PG 16 via --db=external)
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bow_test BOW_DATABASE_URL=$TEST_DATABASE_URL \
uv run pytest --db=external tests/unit/test_memory_service.py tests/unit/test_memory_tools.py \
  tests/e2e/test_memory.py -q
```

Observed: **210 passed** on SQLite, **59 passed** on Postgres. The migration
round-trips (upgrade → downgrade → upgrade) on both.

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
- **Dedupe / edit / forget / cap / concurrency** — variants strengthen one
  entry, a date given later merges in, shared aliases merge, an edit keeps the
  row and handle (a user edit confirms the fact; a date can be cleared),
  the 201st write evicts the weakest non-user entry, 6 concurrent writers all
  persist.
- **Builder** — budgets at 0/10/200 facts; dated facts in the window first,
  then the strongest undated ones; alias and object-tag matches; index line
  counts what's hidden; header says there are no rules in memory.
- **E2E** — setting off removes block, tools and API (403), entries survive;
  own-memory-only API; edits in place; membership removal deletes memory; the
  agent saves a fact with evidence and the next turn sees it; a style
  correction makes the hint say "apply it for the rest of this conversation,
  never save it to memory"; machine turns get the block but no tools; trace
  text only for the owner.

Every guard's test fails when that guard is removed (checked one at a time,
then restored): rule check, catalog gating (setting and machine turns),
injection gate, dedupe, cap, owner-only trace text, user-entry protection,
entry scoping, snapshot scrub, payload redaction, rule hint.

## Loop B — live (`gpt-6-luna`, real stack, Playwright as a user)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo   # + members via the API
```

Sandbox notes: set a stable `BOW_ENCRYPTION_KEY` before booting, and when
restarting the backend kill uvicorn's spawned workers too.

| Scenario | What the user did | Observed | |
|---|---|---|---|
| **Rule** | "Way too long. From now on: the number first, then one line of context, and money in thousands…" | Answer redone ("USA — $0.5K …"); memory empty; Custom instructions still empty; no extra tool | ✅ |
| **Same conversation** | Next question: "And the top 3 genres by revenue?" | "$0.8K — Rock · $0.4K — Latin · $0.3K — Metal" — the rule still applies | ✅ |
| **Fact** | "By the way, I'm leading the APAC launch, which goes live on October 20." | `create_memory` → "Leading the APAC launch…", date 2026-10-20, tags apac/launch | ✅ |
| **New conversation** | "What was total revenue by year?" | Plain answer — the chat rule did not carry over (the intended trade-off) | ✅ |
| **Rule + fact in one message** (GIF) | "Too long — numbers first… Also, I'm leading the APAC launch, going live October 20." | Fact saved with its date; rule applied, not saved | ✅ |
| **Edit in place** | Profile → edit the APAC fact, move the date to Oct 27 | Same id and handle (`m2`), new text and date, source becomes `user` | ✅ |
| **Definitions (org rules)** | 5 reports opening with a definition ("Active customers are…", "Revenue means…", "Always exclude…", …) | 0 memory writes | ✅ |
| **No over-capture** | 10 one-off questions | 0 memory writes | ✅ |
| **Privacy** (earlier run, unchanged code) | Admin opens a member's conversation trace | Handles/tiers only; none of the member's fact texts in any admin payload | ✅ |
| **Scale** (earlier run, same builder) | 155 facts | Injected block 1,478 chars (budget ~2,200) | ✅ |

Observed and not fixed (model behavior, not the feature): the first redone
answer kept the list shape ("USA — $0.5K") rather than strictly "number
first"; the next answer did lead with the number. Pre-existing: answers with
several `$` amounts can render as inline math (markstream-vue).

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
6. **Review:** memory had absorbed style/format rules. Reworked to facts
   only: sections dropped, memory refuses rules on every write, the UI is one
   list with search and tag chips.
7. A refused memory call showed "Couldn't update memory" in the report →
   failed memory calls are hidden there (kept in the trace).
8. **Review:** an interim version offered rules as one-click personal
   instructions (a new tool, card and endpoint) and migrated old rule lines
   into Custom instructions — out of scope. Removed: rules said in chat apply
   to that conversation only; lasting rules stay with the existing
   instruction flows; the unreleased old column is dropped with no migration;
   edits update the row in place instead of versioning.

## Not done / follow-ups

- `docs/design/agent-checkins.md` doesn't exist, so check-ins aren't wired;
  machine turns already get `<memory>` without the tools.
- No user data export exists to extend.
- `search_memory` is lexical; a paraphrase with no shared words can miss.
- Pre-existing, unrelated: `tools/agent/seed_org.py --invite` 422;
  `test_sharepoint_onprem_client.py` / `test_discovery_progress_contract.py`
  fail on the base commit too.

## UI evidence (`media/pr/ai-brave-cori-37ujt9/`)

| | |
|---|---|
| Before: one free-text memory box | `01-before-profile-memory.png` |
| Flow: rule applied (saved nowhere), fact → memory | `02-flow-rule-applied-fact-to-memory.gif` |
| Rule applied: the answer redone, nothing saved | `03-rule-applied-answer-redone.png` |
| Fact saved: memory status line | `04-fact-saved-status-line.png` |
| Memory: one flat list with dates and tags | `05-memory-flat-list.png` |
| Search | `06-memory-search.png` |
| Tag filter | `07-memory-tag-filter.png` |
| Edit in place | `08-memory-edit-in-place.png`, `09-memory-after-edit.png` |
| Hebrew (RTL) | `10-memory-list-he-rtl.png` |
| Trace, owner view | `11-trace-owner-view.png` |
| Trace, admin viewing a member (private) | `12-trace-admin-private.png` |
| Org setting | `13-org-setting.png` |
