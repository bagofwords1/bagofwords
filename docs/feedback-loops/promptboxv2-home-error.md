# Feedback Loop — "started from home / promptbox and it shows `Error: HTTP error! status: 400`, then I continue and it works"

Starting a chat from the **home page** PromptBoxV2 makes the very first
assistant reply fail with `Error: HTTP error! status: 400`. Sending a second
message in the same report then works. This loop validates the root cause
(before the change) and the fix (after).

## Root cause (validated)

The home box and the in-report box build the completion payload differently for
the **Auto** model:

- The in-report submit maps the `'auto'` sentinel back to `null` before sending:
  `modelIdForPayload` (`frontend/components/prompt/PromptBoxV2.vue:1095`), used
  by `buildSubmitPayload` (`:1369`).
- `createReport()` — the home-page path — forwarded the **raw** selection into
  the report URL: `model_id: selectedModel.value || ''`
  (`frontend/components/prompt/PromptBoxV2.vue:1723`, pre-fix). When the org's
  Auto router is on, `selectedModel` defaults to the `'auto'` sentinel
  (`:1123`), so the home box navigated to
  `/reports/{id}?…&model_id=auto`.

The report page reads that query param and submits the first completion with
`prompt.model_id='auto'` (`frontend/pages/reports/[id]/index.vue:4623`). On the
backend, `_resolve_completion_models` treats a non-empty `model_id` as an
explicit pick and looks it up
(`backend/app/services/completion_service.py:272`). `'auto'` is not a real model
id, so `get_model_by_id` returns `None` (`llm_service.py:212`), and the stream
handler raises **400 "No default LLM model configured"**
(`completion_service.py:2026`). The retry works because it goes through the
in-report path, which sends `model_id=null` and lets the router/default resolve.

Backend log at the moment of failure (Loop B):

```
ERROR completion_service: HTTP Exception in create_completion_stream:
400: No default LLM model configured. Please go to Settings > LLM and set a default model.
POST /api/reports/{id}/completions HTTP/1.1 400 Bad Request
```

## Loop A — deterministic reproduction (no external services)

The 400 is a pure model-resolution contract failure — no LLM needed. This test
reproduces it and locks in the general invariant (any unknown `model_id` → 400;
omitting it → the org default resolves and the run starts). The agent is stubbed
at `AgentV2.main_execution`.

```bash
cd backend
TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db" \
  uv run pytest tests/e2e/test_completion_model_id_resolution.py -q --db=sqlite
```

- `test_completion_rejects_unknown_model_id[auto|<uuid>|garbage]` — the exact
  sentinel the home box leaked, plus a well-formed-but-unowned id and arbitrary
  garbage, all 400. This is *why* the sentinel must never leave the frontend.
- `test_completion_resolves_default_when_model_id_omitted` — the corrected
  home-page payload (sentinel → null → field omitted) resolves the org default
  and starts the run (**200**, no 400).

Observed: **4 passed**. (These guard the backend contract; the backend rejected
`'auto'` both before and after — the defect was the frontend sending it.)

## Loop B — live confirmation (real Anthropic credentials + real UI)

Full stack + real model reproduces the user's exact screen and proves the flip.

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo   # Music Store demo
# configure an Anthropic model (provider via API; models seeded — the
# POST /api/llm/models route is broken, see regression notes)
cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node ../tools/agent/repro_home_auto_400.mjs
```

The harness enables the org's Auto router, opens `/`, types `hi`, and submits.

- **Before the fix:** it navigates to `…?model_id=auto`, the first
  `POST …/completions` returns **400**, and the chat shows
  `Error: HTTP error! status: 400` — `REPRO CONFIRMED` (exit 2). Screen:
  `media/pr/promptboxv2-home-error/before/2-report-first-message.png`.
- **After the fix:** it navigates to `…?model_id=` (empty), the first
  completion returns **200** and streams the real greeting, timeline is
  `user:success, system:success` — `FIX VERIFIED`. Screen:
  `media/pr/promptboxv2-home-error/after/3-first-message-success.png`.

## The fix

`frontend/components/prompt/PromptBoxV2.vue`, `createReport()`: send the
payload-mapped model id instead of the raw selection, so the `'auto'` sentinel
becomes `''` (→ `null` on the report page) and the router/default engages —
identical to what the in-report submit already does.

```diff
-                    model_id: selectedModel.value || '',
+                    // Map the 'auto' sentinel back to '' (→ null on the report page)
+                    // so the backend router engages; sending the raw 'auto' string
+                    // is not a real model id and 400s the first completion.
+                    model_id: modelIdForPayload.value || '',
```

## What this proves / regression notes

- The first message from the home page now starts on the same model the retry
  would have used; there is no first-turn 400 when Auto is selected.
- Loop A generalizes past the reported `'auto'` value: it also covers an unowned
  UUID and garbage, so it guards the invariant, not the anecdote.
- Pre-existing, unrelated: `POST /api/llm/models` 500s
  (`LLMService.create_model` missing — also noted in
  `promptboxv2-queue-steering.md`); models for Loop B were seeded directly.
