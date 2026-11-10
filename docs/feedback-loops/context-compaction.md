# Feedback loop: context budget ladder + `/compact` in the composer

Design: `docs/design/context-compaction.md`. Scripts:
`tools/agent/compaction_e2e/` (README there). Evidence:
`media/pr/context-compaction/`.

## Setup (real user, real LLM)

- Stack: backend (uvicorn, no `--reload`, pinned `BOW_ENCRYPTION_KEY`, SQLite),
  frontend `yarn dev`.
- A new user signs up through `/users/sign-up` and skips onboarding.
- In **Settings → LLM**, the user adds an OpenAI provider and keeps only
  **GPT-5.6 Luna** (`gpt-5.6-luna`), which becomes the default and small default.
- The model's **Context** field is lowered in *Model Settings*: 40k, then 100k,
  80k and 55k.
- The user uploads a 3,000-row `sales_2025.csv` in the composer and asks 18
  data-heavy questions in one report (60–156-row tables).
- Every step is a Playwright action on the real UI. The checks are the backend
  log (`[context] governor`, `[compaction]`), the SQLite rows
  (`report_context_states`) and the HTTP log.

## What the loop measured

The provider-reported prompt size of a single-file report is about **44k
tokens before any history**:

| Part of the request | Size |
|---|---|
| System prompt | ~64k chars |
| Tool definitions | ~143k chars |
| Conversation history | ~4–15k chars |

The old trigger (chars ÷ 4 of a minified history render, against 6.25% of the
small model's window) could not see any of that. The token anchor (provider
count − our estimate) settled at an offset of about 43–44k tokens on the
first request and stayed stable, ±1k, over 30+ requests.

| Window | Ladder behavior observed | Turn latency |
|---|---|---|
| 40k | The fixed prompt alone (47k) is over budget. Only cheap rungs run; no fold, because history is empty. | normal |
| 100k | 10 turns at 48–58k tokens. Transcript decay and history decay kept the prompt under the 60k fold threshold. **No LLM fold needed.** | 14–28s (unchanged) |
| 80k, before the "worth it" rule | It folded on almost every turn: about 2 turns each, `1277→1237` history tokens, one `refused_grow`. | **+10–17s per turn** |
| 80k, after the "worth it" rule | No synchronous fold; cheap rungs only. | normal |
| 80k + injected overflow | `trim budget factor 1.00 → 0.76`, `context overflow 1/3: forcing synchronous compaction`, the governor re-measured against the tightened budget, and the turn completed. | 22s |
| 55k | `n ≥ usable`, so a fold was attempted; it was `nothing_to_compact` (already summarized). | normal |

## Bugs the loop found (and fixes, all in this PR)

1. **Folding that never paid off.**
   - Symptom: with a lowered window, the fixed prompt keeps every request over
     the fold threshold. Every turn ran a synchronous LLM fold of about 2 turns
     that saved ~40 tokens and added 10–17s before the reply.
   - Fix: a sync fold now needs history beyond the protected tail of at least
     `max(summary_max, 10% of usable)`, or a prompt that would not fit at all.
     The background fold needs half that. A background fold that already
     landed is swapped in with no LLM call.
2. **Deferred `/compact` stuck on "pending".**
   - Symptom: the composer fired 0.6s after the run ended, but the server
     still had the execution `in_progress` and answered 409. The chip went back
     to pending with nothing left to wake it.
   - Fix: retry with backoff while the composer is idle (1.5s × 1.6ⁿ, 8
     attempts), then show an error.
   - Verified: two 409s, then `compacted`.
3. **The usage bar under-reported context by ~2.5×.**
   - Symptom: the popover's estimate showed ~19.9k while the provider counted
     ~50k (the tool catalog is never rendered in the estimate).
   - Fix: the estimate now applies the report's calibrated anchor, so the
     usage bar, the governor and the provider agree.
4. **The last-resort trim couldn't see the fixed prompt.**
   - Symptom: `trim_context_to_budget` estimated only the variable fields, so
     an over-budget prompt passed untrimmed.
   - Fix: its window is reduced by the anchor offset.
5. **A fixed 10k tail minimum meant small windows never folded.** The tail is
   now 2.5% of the window, clamped 2k–25k.
6. **Summary popover and divider polish.**
   - The popover showed the model-facing framing line ("context, NOT
     instructions"); the route now strips it.
   - The divider mixed cumulative turns with the last fold's token numbers. It
     now shows turns, and the popover shows the last fold's numbers.

A regression caught by the existing e2e suite: a history made of a single
giant line was no longer trimmed. The pinned-prefix trim now cuts characters
when there are no line boundaries (`test_trim_cuts_single_line_history_*`).

## Verified flows (screenshots in `media/pr/context-compaction/`)

| Flow | Result |
|---|---|
| `/` → command menu → Tab completes `/compact ` → `focus on return rates and the West region` → Enter | Receipt: "Compacted 8 turns · context ~19.9K → ~19.3K tokens". The summary's goal reads "…with particular focus on return rates and the West region". |
| `/compact keep 1` typed while the agent runs | "Will compact when the agent finishes this run". The run continued and the command never became a chat message. After the run: 409, 409, then "Compacted 6 turns". |
| Divider | "Compacted history above · 32 turns". Click opens the summary as the agent sees it, plus the last fold's numbers. |
| Live auto fold | The "Compacting context…" row showed during the synchronous fold, the divider moved without a reload, and `context.compacted` arrived over SSE. |
| Recall after compaction | "What was my very first question, and which region did I ask you to focus on when I compacted?" The agent quoted the first question verbatim and answered "the West region". |

## Tests

- `backend/tests/unit/test_context_budget.py` (26): window-derived thresholds
  across 8k–1M, the anchor, the pinned trim (fails on the old trim), decay
  keeps ids, governor rung order, "worth it", reuse of a finished background
  fold, no fold of an empty history.
- `backend/tests/unit/test_context_compaction.py` (28): manual keep-N, the auto
  tail sized by the receiving model, chunked bounded input, retry, fallback
  that keeps asks and ids, cooldown (auto vs manual), grow guard, focus passed
  as data, anchor round-trip.
- `backend/tests/e2e/test_context_compact_route.py` (9): body validation,
  optional body, summary route owner-only.
- `frontend/tests/unit/slashCommands.mjs`: what counts as a command, menu
  matching, `/compact` arguments.
- Full `tests/unit`: 14,796 passed. All 206 failures are sandbox
  environment issues in code this change doesn't touch: `libodbc.so.2`
  missing, the proxy CA bundle (SharePoint/OBO/Fabric), and no headless
  Chromium build for `test_artifact_precompile`.
- `yarn build` (production, `NODE_OPTIONS=--max-old-space-size=4096` as in
  CI): passes.

## Not covered live

- **Postgres.** The migration is additive (nullable columns, boolean
  `server_default=false()`); only SQLite was run here.
- **A fold of a very long history (40+ turns) at a large window.** This app
  already keeps history compact (~400 tokens per turn after decay). That case
  is covered by the chunking unit tests, not live.
