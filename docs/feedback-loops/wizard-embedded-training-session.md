# Feedback Loop — Create Data Agent wizard: embedded training session in "Set Context"

The wizard's last step used to offer only a manual instruction editor. The
feature under validation: when the org has an LLM (and the agent keeps "Use
LLM to learn agent" on), step 3 instead offers a guided **training session** —
a training-mode report titled `Training "<agent>"`, embedded live in the modal
(`/reports/{id}?embed=1` in an iframe), driven by a hidden kickoff brief:
friendly intro → `describe_tables` + `inspect_data` (main tables, max 10) →
a "Data overview" note → a `clarify` round → instructions from the answers →
a short wrap-up pointing at the saved session. Without an LLM the step falls
back to the classic instruction box.

## Pieces

- `backend/app/services/training_session_service.py` — preview (table count +
  small-model domain guess) and session start (report + hidden trigger
  completion, `trigger_source='training_session'`, the `machine_turn.py` idiom).
- `backend/app/routes/data_source.py` — `GET .../training_preview`,
  `POST .../training_session` (both `data_source: manage`).
- `frontend/components/NewAgentWizardModal.vue` — CTA card → embedded iframe →
  "Continue in full session" / Finish; skip link to the manual editor.
- `frontend/pages/reports/[id]/index.vue` + `frontend/layouts/default.vue` —
  `?embed=1` mode: no sidebar/header/split-screen, compact type, training
  event strip.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
TESTING=true BOW_DATABASE_URL="sqlite:///db/test_app.db" \
  uv run pytest tests/e2e/test_training_session_wizard.py -m e2e -q
```

Covers the API contract: preview degrades without an LLM (`llm_available:
false`, no domain hint, 400 on session start), session start creates a
`mode='training'` report named `Training "<agent>"` pinned to a model, the
kickoff brief is hidden from `GET /reports/{id}/completions` while the
`role='external'` event strip and agent turn remain, and the hidden row
carries the training-mode brief. Fails before the feature exists (404 on the
routes).

## Loop B — live confirmation (real LLM, full UI)

Boot the stack, register a small default model (Haiku), create sqlite
connections (Chinook + a deliberately messy 15-table DB), then drive the
wizard end-to-end with Playwright: step 1 → step 2 (select all) → step 3 CTA
("I found 11 tables — looks like music store data…") → Start → live embedded
session (intro, describe/inspect batch, note, clarify chips) → answer →
instructions + wrap-up → "Continue in full session" opens `/reports/{id}` →
Finish lands on the agent page.

Observed on the messy DB: the overview note flagged mixed date formats,
currency-string amounts, null-heavy columns, a `signup date` column with a
space, and ended with a "To review later" section listing the 6+ tables beyond
the 10-table review cap — with a clarify question asking which of them matter.

Fallbacks verified live: org LLM disabled → manual editor; "Use LLM to learn
agent" off → manual editor; "Prefer to write instructions yourself?" → manual
editor.

## Regression notes

- Swapping `UModal`'s `ui.width` while the modal is open re-runs the panel
  transition and strands it at the enter-from state (`opacity-0 scale-95`) —
  an invisible but clickable dialog. The wizard therefore keeps one width for
  its whole lifetime; the embed is sized to the 2xl panel. (Found via
  headless + Xvfb-headed Playwright, both reproduced.)
- The kickoff prompt must go through the `trigger_source` filter, not a new
  flag: `get_completions_v2` already hides `role='user'` rows with a trigger
  source, and the event strip/`role='external'` row renders the visible
  "Training session started" line.
