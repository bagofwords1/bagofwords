# Feedback Loop — User memory: entries, tools and context builder

Replaces the single 2,000-char `Membership.memory` document with one row per
durable, personal fact (`memory_entries`), three agent tools
(`create_memory` / `edit_memory` / `search_memory`), a tiered, fixed-budget
`<memory>` context block, a self-service profile UI and API, owner-only memory
lines in the trace, and one org setting (`enable_user_memory`) that gates it
all. Memory is personal context (style, role, shorthand, dated events, focus,
preferences) — never business logic, which stays in instructions.

The claim this loop validates: the agent saves personal facts **on noticing**
(not only when asked), routes definitions away from memory, keeps the injected
block within budget however many entries exist, never loses a write under
concurrency, and never shows one user's memory to anyone else.

## What changed (map)

| Area | Where |
|------|-------|
| Model + migration | `backend/app/models/memory_entry.py`, `backend/alembic/versions/usrmem01_add_memory_entries.py` (also adds `agent_executions.memory_context_json`) |
| Legacy migration (idempotent) | `backend/app/services/memory_migration.py` |
| Pure rules (normalize, tags, expiry, secret + definition heuristics, signals) | `backend/app/services/memory_rules.py` |
| Write/read path (dedupe, supersede, forget, cap, search) | `backend/app/services/memory_service.py` |
| Privacy scrub for persisted payloads | `backend/app/services/memory_privacy.py`, `backend/app/serializers/completion_v2.py` |
| Tiered context builder | `backend/app/ai/context/builders/memory_context_builder.py`; shared matcher `backend/app/ai/context/keyword_match.py` (instruction builder now delegates to it) |
| Tools (replace `update_user_memory`) | `backend/app/ai/tools/implementations/{create,edit,search}_memory.py`, `_memory_common.py`, schemas `backend/app/ai/tools/schemas/memory.py` |
| Agent wiring | `backend/app/ai/agent_v2.py` (`_resolve_user_profile`, `_build_memory_block`, `_memory_hint`, catalog gating, trace stamp) |
| Prompt rules + `<memory>` block | `backend/app/ai/agents/planner/prompt_builder_v3.py`; `create_instruction` description carries the boundary |
| Org setting | `enable_user_memory` in `backend/app/schemas/organization_settings_schema.py` |
| User API (own memory only) | `backend/app/routes/user_memory.py` — `GET/POST/PATCH/DELETE /api/users/me/memory[/{id}]` |
| Trace | `ConversationTurnSchema.memory` (`agent_execution_trace_schema.py`), `ConsoleService._turn_memory_sections`, `frontend/components/console/TraceModal.vue` |
| UI | `frontend/components/profile/UserMemoryPanel.vue`, `MemoryEntryForm.vue`, Memory tab in `UserProfileModal.vue`, `frontend/components/tools/MemoryTool.vue` |
| i18n | `settings.aiSettingsPage.features.enable_user_memory`, `tools.memory.*`, `profile.memory.*`, `traceModal.memory.*`, `errors.memory.*` in all 10 catalogs |

## Environment

```bash
cd backend && pip install uv && uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

## Loop A — deterministic (no real LLM)

Stubs only at the boundaries (LLM via `AgentV2.main_execution`, the clock via
explicit `now=`). Seeding goes through `tests/fixtures/*` and the real API.

```bash
cd backend
uv run pytest tests/unit/test_memory_rules.py tests/unit/test_memory_context_builder.py \
  tests/unit/test_memory_service.py tests/unit/test_memory_tools.py tests/unit/test_memory_migration.py \
  tests/unit/test_memory_privacy.py tests/unit/test_prompt_builder_v3_user_profile.py \
  tests/unit/test_batch_observation.py tests/e2e/test_memory.py -q
# Postgres leg (no Docker in this sandbox → local PG 16 via --db=external):
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bow_test \
BOW_DATABASE_URL=$TEST_DATABASE_URL \
uv run pytest --db=external tests/unit/test_memory_service.py tests/unit/test_memory_tools.py \
  tests/unit/test_memory_migration.py tests/e2e/test_memory.py -q
```

Observed: **175 passed** on SQLite; **48 passed** on Postgres
(service, tools, migration and every e2e test).

What the suites pin (contracts, not incidental output):

- **Dedupe** — normalization variants strengthen one entry (`seen_count`, merged
  tags); a different section inserts; a vocabulary alias match merges.
- **Expiry** (computed on read) — timed events hide 24h after their end, a
  date-only event lasts its day plus one; ranges stay visible for their whole
  duration; focus expires 30 days after last seen and returns when seen again.
- **Cap** — the 201st write evicts the lowest-`seen_count`, oldest non-user
  entry; user-authored entries are never evicted (a cap full of them refuses).
- **Concurrency** — 6 concurrent writers from separate sessions all persist with
  unique handles (seq unique constraint + savepoint retry); two reports'
  parallel `create_memory` calls both persist (e2e).
- **Context builder** — budgets hold at 0/10/200 entries; always tier = style,
  role, preferences + events in [−2d, +21d]; alias match → matched tier with
  its reason; `agent:<id>` object tags match only when that agent is in the
  turn and outrank keyword matches; the index line counts exactly what was left
  out and lists tags in use; handles are stable across renders; the header says
  memory holds no definitions or rules.
- **Tags** — `Board Deck`, `board_deck`, ` board--deck ` → `board-deck`.
- **Tool validation** — event without a date, >4 tags, >280 chars, missing tags,
  secrets, unknown handle, editing a user-typed entry without a direct request,
  appending a different fact to an entry (`memory.one_fact_per_entry`).
- **Boundary** — 12 definition/rule phrasings refused with a pointer to
  instructions (refusal recorded for the trace); 14 personal phrasings,
  including `when I say my region I mean EMEA`, accepted.
- **Migration** — multi-line text → N `preferences` entries (`source=migration`,
  no tags); re-running adds nothing; definition-like lines are logged for review.
- **E2E** — setting off: no `<memory>` block, tools absent, API 403
  `memory.disabled`, entries survive re-enable; own-memory-only API (another
  member and an org admin get 404 on someone else's entry; "forget everything"
  only touches the caller's); membership removal deletes its entries; the agent
  saves a style correction with `source=agent` + evidence and the next turn's
  block contains it; `search_memory` excludes injected entries and returns
  source links; wait / check-in / scheduled runs receive `<memory>` but no
  memory tools; the admin's trace shows handles/sections/counts but never text,
  the owner's shows text.

**Every test can fail** (rule 6). Mutations applied one at a time, each turned
the named tests red and was reverted:

| Mutation | Tests that failed |
|---|---|
| remove memory-tool payload redaction in the serializer | trace privacy (e2e) |
| drop the setting check from catalog gating | setting-off (e2e) |
| drop the machine-turn check from catalog gating | machine turns (e2e) |
| drop the setting check from `<memory>` injection | setting-off (e2e) |
| disable dedupe | 5 service tests |
| disable the definition/rule heuristic | 4 boundary tool tests |
| disable cap eviction | cap test |
| show text to every trace viewer | trace privacy (e2e) |
| disable user-authored entry protection | 2 tool tests |
| drop user/org scoping from the entry lookup | cross-user API (e2e) |
| disable the context-snapshot scrub | 4 privacy tests |

## Loop B — live (real LLM `gpt-6-luna`, real stack, Playwright as a user)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo   # + a member "Dana Levi"
# drive flows in Chromium: login → prompt box → report page → profile → trace
```

Setup notes (sandbox-only): set a stable `BOW_ENCRYPTION_KEY` before booting
(otherwise a backend restart makes stored LLM credentials undecryptable), and
when restarting kill uvicorn's spawned workers too, not just the parent — a
surviving worker keeps serving the old code.

All flows below were driven in Chromium through the real UI (sign-in → prompt
box → report page → profile → trace), against `gpt-6-luna`, on the final code
with a freshly restarted backend. Evidence: `media/pr/ai-brave-cori-37ujt9/`.

| Scenario | What the user did | Observed (final run) | Verdict |
|---|---|---|---|
| **Style** | Asked "top 5 countries by revenue", then "Way too long… number first, then one line of context", then "show money in thousands with one decimal, like $2.3K" | Turn 2: `create_memory` ("Noting your concise style") + the answer redone number-first. Turn 3: a second, separate `create_memory` ("Saving your currency format") + the answer redone in $K. Both entries in the profile with "from <report>" links. New report "total revenue by year?" → "2021 $0.4K · 2022 $0.5K …" | ✅ saves without being asked; next report follows both |
| **Events** | "Board meeting next Thursday, and I'm off the whole week after" (+ a question) | Two `events` entries with absolute dates: Thu 2026-10-01, and 2026-10-05 → 10-11. Dates kept out of the text | ✅ |
| **Event expiry** | (Loop A with a controlled clock) | Hidden 24h after a timed end / the day after a date-only event; ranges visible throughout | ✅ |
| **Vocabulary + matching** | Admin: "When I say my region, I mean Germany…" then, in a new report, "revenue in my region by year?" | Entry saved on first mention with alias "my region". New report: trace shows `[m3] matched vocabulary` (owner view, with text); SQL filtered to Germany | ✅ |
| **Boundary** | 5 new reports opening with a definition/rule: active customers (90 days), revenue excl. tax, "always exclude USA from averages", long track > 5 min, "Europe includes UK and Norway" | 0 memory entries, 0 memory tool calls; each definition applied to that answer. Run twice (10 statements total): 0 leaks | ✅ 0 leaks |
| **Scale** | 150 varied entries seeded for the member, then a new question | 155 entries → injected block **1,478 chars** (11 always + 1 matched, 143 summarized in the index line); profile preview 1,382 chars | ✅ within budget |
| **search_memory** | A hidden entry "Monthly finance pack layout: headline figure, main driver, biggest risk"; asked "revenue by country, laid out the way I usually want it for the CFO" | `search_memory` ("Checking your CFO format") found it; answer used **Headline / Main driver / Biggest risk** | ✅ (an earlier run with a different query missed it — lexical) |
| **No over-capture** | 10 one-off questions in 10 new reports | **0** entries created, 0 memory tool calls | ✅ target ≈ 0 |
| **Privacy** | Member said "I'm the head of FP&A and I present to the CFO every month…"; admin opened that conversation's trace | `create_memory [m326]` shown to the admin as handle/section only; the admin's conversation trace, every per-turn trace (incl. context snapshots) and the admin's own memory API contain **none** of the member's 5 entry texts | ✅ (after fixing finding 5) |
| **Migration** | Legacy 4-line memory document for the member | 4 `preferences` entries (source "migration"), shown in the always tier and the profile; the definition-like line logged for review | ✅ |

### Metrics

| Metric | Target | Observed |
|---|---|---|
| Precision (agent entries a human would keep as-is) | ≥ 80% | Final code: 6/6 (100%). Across all iterations: 3 of 21 agent writes merged two facts into one entry (fixed by the one-fact refusal) |
| Boundary leaks (definitions/rules in memory) | 0 | 0 of 10 |
| Entries per 10 one-off turns | ≈ 0 | 0 |
| Repeated style corrections after capture | → 0 | 0 — the next report applied both preferences without a reminder |
| Injected memory size | within budget | p50 ≈ 275–635 chars (1–5 entries); 1,478–1,509 chars with 154–155 entries (budget ~2,200) |

Wrong captures observed (all in earlier iterations, before the fixes below):
merged facts (`edit_memory` appended "formats money in $K" to the "number
first" entry, 3×); event text repeating the date ("Has a board meeting on
2026-10-01") — both addressed.

### Final prompts and tool descriptions

- **Rules** (`prompt_builder_v3.py`, COMMUNICATION): `<memory>` is personal
  context, not business logic; apply its style/preferences to every answer;
  save on noticing (corrections, role, shorthand, dated events with absolute
  ISO dates, current focus); never save definitions/rules, one-off details,
  data values, other people, secrets or health details; one fact per entry,
  `edit_memory` only the entry that says the same thing; `search_memory` when
  the user refers to something personal that isn't shown; declarative facts,
  never imperatives.
- **`<memory_hint>`** (code, next to the ask) — only when the user's own message
  carries personal signals (`memory_rules.personal_signals`: style correction,
  role, shorthand, dated event, focus, explicit request): save now, one fact per
  entry, and still redo the answer in the corrected style.
- **`<memory_apply>`** (code, next to the ask) — names the injected style /
  preference handles so the final answer applies them.
- **Tool descriptions** carry the boundary sentence in all three tools and in
  `create_instruction`; `create_memory` refuses definition-like text with a
  pointer to instructions; `edit_memory` refuses user-typed entries without a
  direct request and refuses appending a different fact; `search_memory`
  suggests browsing by section/tag when a query finds nothing that fits.

## Findings fixed during the loop

1. **Agent never saved on its own** (first Loop B run: two style corrections,
   zero `create_memory`). The rule alone is not enough for a small model. Fix:
   code-driven `<memory_hint>` next to the ask when the message carries
   personal signals. After: saves on the first correction every run.
2. **"Got it" instead of an answer** after a style correction. Fix: the hint
   says a correction means redoing the previous answer in that style. After:
   turn 2/3 answers are the reshaped numbers.
3. **Two facts merged into one entry** (`edit_memory` appended the currency
   preference to the "number first" entry). Fix: prompt/tool wording plus a
   deterministic refusal (`memory.one_fact_per_entry`) that sends the agent to
   `create_memory`.
4. **Remembered style not applied in a new report** on some runs. Fix:
   "apply `<memory>` to every final answer" rule plus the `<memory_apply>` line.
5. **Privacy leak through context snapshots** — the admin's per-turn trace
   (`head_context_snapshot.warm.observations`) carried `create_memory` inputs.
   Found by the Loop B privacy scan, not by reading code. Fix: memory tool
   records are scrubbed before any snapshot is persisted
   (`memory_privacy.scrub_context_snapshot`), unit-tested with the scrub
   disabled to show it fails.
6. **Self-deadlock stamping the trace** — writing `memory_context_json` through
   a second session at turn end waited on the run's own open transaction
   (SQLite write lock; a row lock on Postgres) and stalled the backend for
   minutes. Fix: set it on the run's own execution object so
   `finish_agent_execution` commits it.

## Not done / follow-ups

- `docs/design/agent-checkins.md` does not exist in the repo, so the §11
  check-in planner/judge input could not be wired. Check-in runs are machine
  turns (`trigger_source`) and already receive `<memory>` without memory tools
  (covered by the e2e machine-turn test with `trigger_source="checkin"`).
- There is no user data export in the codebase to extend; entries are
  available to their owner through `GET /api/users/me/memory`.
- The knowledge harness only fires on its own trigger conditions
  (clarify→create_data, failures, explicit corrections, …), so a definition
  stated inline with a question is applied to the answer but not proposed as
  an instruction automatically. Memory never stores it (0 leaks).
- `search_memory` is lexical; a paraphrase with no shared words misses (see
  Loop B). Embeddings only if this shows up in real use (plan §14).
- `Membership.memory` is kept read-only for one release; drop it after.
- Pre-existing, unrelated: `tools/agent/seed_org.py --invite` posts without
  `organization_id` (422); `tests/unit/test_sharepoint_onprem_client.py` and
  `test_discovery_progress_contract.py` fail on the base commit too.
- Model reasoning text ("Thought for 2s") can paraphrase what it is saving;
  that text is visible wherever the conversation itself is.

## UI evidence (ui-evidence skill)

Captured with Playwright against the live stack (1440×900). Files in
`media/pr/ai-brave-cori-37ujt9/`:

| | |
|---|---|
| Before: one free-text memory box under "Instructions & Memory" | `01-before-profile-memory.png` |
| After: Memory tab, same (migrated) data as entries | `02-after-profile-memory-migrated.png` |
| Flow: ask → style correction → single-line memory status → entry in profile | `03-flow-style-correction-to-profile.gif` |
| Report: two corrections, two memory status lines, reshaped answers | `04-style-corrections-saved.png` |
| New report applies the remembered style | `05-new-report-applies-style.png` |
| Agent-saved events with dates, tags, source links | `06-events-profile.png` |
| Trace, owner view: matched tier with text | `07-trace-owner-matched-tier.png` |
| Trace, admin viewing a member: handles and sections only | `08-trace-admin-private.png` |
| search_memory finds a hidden preference and the answer follows it | `09-search-memory-finance-pack.png` |
| Hebrew (RTL) Memory tab, user-added event | `10-profile-memory-he-rtl.png` |
| Org setting in AI settings | `11-org-setting.png` |
| "Forget everything" confirmation | `12-forget-everything-confirm.png` |
