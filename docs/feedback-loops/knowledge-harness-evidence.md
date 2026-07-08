# Feedback Loop — "AI suggested by Joe" shows no evidence in the Knowledge Explorer

When the Knowledge Harness (agent v2, `mode="knowledge"`) or Training mode
(prompt builder v3) suggests instruction changes, the Knowledge Explorer's
per-hunk review only showed **that** it was AI-suggested and **who** ran the
session ("AI suggestion · Joe"). The `evidence` string the model already emits
in every `create_instruction` / `edit_instruction` call was dropped on the
floor (harness: stitched only into the build description; training: not
persisted at all), so reviewers had no idea **why** a change was proposed.

## Root cause (validated)

- `evidence` exists on the tool inputs
  (`backend/app/ai/tools/schemas/create_instruction.py:56`,
  `edit_instruction.py:61`) but was never written to any row: the tools call
  `instruction_service.create_instruction(...)` /
  `version_service.create_version_from_data(...)` without it.
- `InstructionVersion` (`backend/app/models/instruction_version.py`) had no
  column to hold it, so `review_hunks`
  (`backend/app/services/instruction_service.py:1373`) had nothing to return,
  and the hover card in
  `frontend/components/instructions/InstructionTrackedChanges.vue:60` rendered
  only source + author.
- The only survivor was the harness's build *description*
  (`backend/app/ai/agent_v2.py:1160-1168`) — build-level, mixed across all
  instructions in the build, and not shown per hunk.

## Loop A — deterministic reproduction (no external services)

Fresh sandbox, stubbed LLM boundary (no `ANTHROPIC_API_KEY` needed — the loop
drives the real tools directly, which is exactly what the harness/training
loop calls):

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
# seed an instruction + pending AI suggestion build (created_by = "Joe"),
# same injection shape as tests/e2e/test_instruction.py::_inject_suggestion_build
uv run python <scratch>/seed_ai_suggestion.py
curl -s .../api/instructions/<id>/review-hunks   # suggestion has NO evidence field
```

**Observed FAIL (before):** the hunk hover card shows only
"AI suggestion · Joe"; `review-hunks` suggestions carry no evidence.
Screenshot: `media/pr/knowledge-harness-evidence/before-hovercard.png`.

Regression tests (fail on pre-change code, pass after):

```bash
cd backend && TESTING=true uv run pytest tests/e2e/test_instruction_evidence.py -q
# 4 failed on pre-change code → 4 passed after
```

## The fix

1. **Persist per staged version** — `instruction_versions.evidence` (Text,
   NULL for user/git versions; excluded from `content_hash`). Migration
   `e7f8a9b0c1d2`. Both tools clamp to ≤280 chars (`clamp_evidence`,
   `create_instruction.py`) — over-long evidence is truncated, never rejected.
2. **Surface it**:
   - `review_hunks` returns `evidence` per suggestion (one extra bulk query,
     same O(1)-queries shape as the base-text/trace lookups).
   - Instruction detail (`GET /instructions/{id}`) returns the current
     version's `evidence` — covers NEW AI instructions, which have no diff
     hunks (the whole text is the suggestion).
   - UI: hover card in `InstructionTrackedChanges.vue` and the diff-view card
     in `KnowledgeExplorer.vue` show the evidence under "AI suggestion · <user>";
     the detail meta row shows it under source + author
     (locale key `agentsPage.evidenceTip` in all 10 catalogs).
3. **Keep it brief** — tool schema descriptions now demand ONE short sentence
   (<150 chars) naming source + fact; the harness prompt
   (`prompt_builder.py` `_build_knowledge_prompt` RULES) and the training
   prompt (`prompt_builder_v3.py` training block) instruct the same; the
   metadata examples model the target length.

**Observed PASS (after):** same seed + evidence stamp → hover card reads
"AI suggestion · Joe" plus *"inspect_data: invoices.status includes
'cancelled' and 'refunded' rows that inflated revenue in this session."*
Screenshots: `media/pr/knowledge-harness-evidence/after-hovercard.png`,
`after-detail.png`. Live confirmation: running the REAL
`CreateInstructionTool.run_stream` against the sandbox DB persists the
evidence and the detail API returns it.

## What this proves / regression notes

- Evidence flows tool → version → API → UI for both flows (harness + training
  use the same two tools) and for both suggestion shapes (edit-hunks and
  new-instruction drafts).
- User-authored instructions/versions keep `evidence = NULL` (asserted in
  `test_edit_instruction_evidence_surfaces_in_review_hunks`).
- `tests/e2e/test_instruction.py` + `test_build.py` (64 tests) and
  `rbac/test_rbac_instructions.py` pass unchanged.
- Pre-existing quirk (not touched): instructions created by the AI tools have
  `source_type='user'`; the "AI suggestion" badge derives from
  `build.source='ai'`.
