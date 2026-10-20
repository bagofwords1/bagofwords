# Feedback Loop — "Smart instructions shouldn't go full content and context but like skills"

Smart (`load_mode='intelligent'`) instructions were supposed to load only when
relevant. In practice almost all of them landed in the prompt in full: one
shared word with the user's message was enough, and a build with no message
filled every free slot (up to 50) with the most-used ones. The claim being
validated: smart instructions should behave like skills. Show a catalog
entry by default and inline the full text only when there's a strong reason.

## Root cause (validated)

- `backend/app/ai/context/builders/instruction_context_builder.py` (`_load_from_build`,
  and the same logic in `_build_legacy`) loaded every intelligent instruction
  with keyword score `> 0`. The score comes from `keyword_match.score_text`,
  which counts exact words, stems and substrings anywhere in the body. So a
  body that mentions "revenue" once was inlined for any revenue question.
- When there were no query keywords, the same code fell back to
  `ranked[:remaining_slots]`, which filled capacity by usage. Query-less
  callers therefore inlined up to 50 smart instructions:
  - `agent_v2.py` `_build_planner_prompt_text` (the token estimate)
  - the mid-run re-scope build
  - `agent_focus_common.render_agents_full`
  - `create_data`'s visualization instructions
- Catalog entries showed the title, a description and tables. They didn't
  show non-table references (linked instructions, resources).

## Loop A — deterministic reproduction (no external services)

The LLM is stubbed at `LLM.inference_stream_v2` / `inference_stream` /
`inference`. The stub records every prompt. Routes, services, context
building and the agent loop run for real.

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run pytest tests/e2e/test_instruction_catalog.py \
              tests/e2e/test_smart_instructions_planner_prompt.py \
              -m e2e --db=sqlite -q -W ignore
```

Before the fix (`git stash push -- app`):

```
FAILED test_instruction_catalog.py::test_no_query_inlines_no_intelligent_instructions
  AssertionError: query-less build must not inline intelligent rules
FAILED test_instruction_catalog.py::test_body_only_match_is_advertised_not_inlined[refunds this quarter]
FAILED test_instruction_catalog.py::test_body_only_match_is_advertised_not_inlined[show refunded orders]
FAILED test_instruction_catalog.py::test_catalog_entry_carries_description_and_references
FAILED test_smart_instructions_planner_prompt.py::test_planner_prompt_inlines_strong_matches_and_advertises_the_rest
  AssertionError: body-only match must not be inlined
  assert 'WEAK8995e491' not in "SYSTEM\nMode: Chat ..."
5 failed, 5 passed
```

The last test runs a full `POST /reports/{id}/completions`. In the captured
planner prompt, the instruction titled "Net figures", which mentions
revenue only in its body, was inlined in full for "What was revenue last
month?".

## The fix

The rule is now **catalog by default, inline only on a strong match.**

- `instruction_context_builder.py`
  - New `_is_strong_match`: a query keyword must hit the instruction's
    **title, labels or referenced table names**. Only strong matches are
    inlined, still capped by `max_instructions_in_context`.
  - A body match still raises the score, but that only moves the
    instruction up the catalog.
  - With no query keywords, no intelligent instruction is inlined; all of
    them go to the catalog.
  - The build path and the legacy path both follow this rule.
- Catalog entries now carry non-table references as `kind:name`, for example
  `refs="instruction:Revenue definition"`. They come from
  `references_json` on a build, or from `InstructionReference` rows on the
  legacy path. Description and tables were already there.
- Callers:
  - `agent_v2._build_planner_prompt_text` reuses the query-aware section from
    `prime_static()`, so the token estimate matches the real prompt.
  - The mid-run re-scope rebuild passes `context_hub.last_instruction_query`,
    so strong matches stay inlined after an agent is added.
  - `create_data` passes the user prompt into the visualization-instructions
    build.
  - `render_agents_full` renders the catalog, since the planner reads that
    output and can call `read_instruction`.
- Unchanged:
  - `always` instructions
  - skills
  - instructions linked by an explicit reference to an inlined one
  - `describe_tables` (still returns the full text of instructions tied to the
    tables the agent picked)

After the fix:

```
10 passed in 15.03s
```

Wider regression run: 258 passed for all instruction / skill / catalog /
focus / roster / sub-agent e2e files, and 139 passed for the related unit
tests.

## What this proves / regression notes

- The planner prompt carries the full text of title-, label- or table-matched
  smart instructions. Every other smart instruction appears only as an
  `<instruction_ref short_id=…>` with its description and references.
- Catalog descriptions fall back to the body's **first line**, capped at 160
  characters, when an instruction has no description. So a one-line rule is
  still effectively visible in the catalog, by design. The tests put their
  marker text on the second line.
- Behavior change to review: an untitled smart instruction with no labels or
  table references can no longer be inlined. It is reachable only through the
  catalog snippet and `read_instruction`.
- Behavior change to review: the visualization coder (`create_data`) used to
  get up to 50 smart instructions by usage. It now gets only the strong
  matches for the user's prompt. Visualization rules that must always apply
  should be set to `always`.
