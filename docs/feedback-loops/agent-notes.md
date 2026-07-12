# Feedback Loop — agent notes (per-report scratchpad)

Validates the agent-notes feature end to end (design: `docs/design/agent-notes.md`):
the agent keeps a per-report scratchpad via `create_note` / `edit_note`, the notes
are injected back into the planner (and knowledge harness) every iteration, gated by
the `enable_agent_notes` org setting, and surfaced read-only in the report UI (chat
tool cards, TraceModal, Summary, and a detail modal).

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/test_agent_notes.py -v
# Regression: doc tools still pass (shared find/replace helper untouched)
uv run pytest tests/unit/test_doc_markdown.py tests/e2e/test_doc_artifacts.py -q
```

Observed: 10 agent-notes e2e tests PASS (create persists; edit_note surgical
find/replace flips a `- [ ]` → `- [x]` atomically; ambiguous match leaves the note
unchanged; full-content fallback; cross-report + unknown-id guards; `GET
/reports/{id}/notes` route; catalog gating). 32 doc tests still PASS.

Frontend production build (`cd frontend && yarn build`) PASSES with the note tool
cards, Summary section, and detail modal.

## Loop B — live confirmation (real LLMs; ANTHROPIC key via env only)

Anthropic provider with BOTH models: `claude-sonnet-4-6` (main) and
`claude-haiku-4-5-20251001` (small). Music Store (Chinook) demo installed. Deep-mode
prompt asks the agent to keep a checklist note and record findings.

Observed (2026-07-12):
- **Sonnet run** — the planner's FIRST action was `create_note` with a `## Checklist`
  (four `- [ ]` items) + an empty `## Key Findings`; it then ran 4× `create_data`
  (revenue over time, top countries/genres/customers); it finished with a single
  `edit_note` using **surgical find/replace** to flip every `- [ ]` → `- [x]` and fill
  in the findings. The LLM judge scored the response 5/5, explicitly crediting the
  "checklist upfront, completed all four" behavior.
- **Haiku run** — same shape: `create_note` checklist → analysis → `edit_note` checking
  off both steps with a key finding each. Confirms the feature works across model tiers
  (and Haiku also served as the small model in the Sonnet run).
- **Injection** — the `<notes>` block is rendered near `<last_observation>` in the v3
  (main) and v2 (harness) builders, framed as agent memory (not user instructions), and
  gated: with `enable_agent_notes` off the tools are stripped from the catalog and the
  block is skipped (unit-asserted).
- **UI evidence** — `media/pr/agent-notes/*.png`: CreateNoteTool card (initial plan),
  EditNoteTool card (find/replace diff flipping checkboxes), TraceModal (note tool
  blocks + decision timeline), Summary Notes section, NoteDetailsModal (Sonnet + Haiku).

## What this proves / notes

- Notes exercised at every layer: tool contract, HTTP route, context injection
  (both planners), org gating, and two live model tiers taking notes autonomously.
- `edit_note` reuses `apply_find_replace_edits` from the doc feature (shared, unchanged).
- Environmental quirks hit during setup (unrelated to this feature): the
  `POST /api/llm/models` route references a missing `LLMService.create_model`, and a
  raw model-row copy must clear `is_small_default` to avoid a duplicate small-default;
  neither is touched by agent-notes.
- Known cosmetic: the chat/markdown renderer mangles `$`-prefixed numbers into LaTeX
  in note previews — pre-existing renderer behavior, not specific to notes.
