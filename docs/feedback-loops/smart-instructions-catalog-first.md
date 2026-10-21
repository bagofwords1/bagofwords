# Feedback Loop — "Smart instructions shouldn't go full content and context but like skills"

Smart (`load_mode='intelligent'`) instructions were supposed to load only when
relevant. In practice almost all of them landed in the prompt in full: one
shared word with the user's message was enough, and a build with no message
filled every free slot (up to 50) with the most-used ones.

The decision: treat them exactly like skills. Never auto-load a smart
instruction. List every one in a catalog and let the LLM decide which to read.

## Root cause (validated)

- `backend/app/ai/context/builders/instruction_context_builder.py`
  (`_load_from_build`, and the same logic in `_build_legacy`) loaded every
  intelligent instruction with keyword score `> 0`. `keyword_match.score_text`
  counts exact words, stems and substrings anywhere in the body, so a body
  that mentions "revenue" once was inlined for any revenue question.
- With no query keywords it fell back to `ranked[:remaining_slots]`, which
  inlined up to 50 smart instructions by usage. Every query-less caller hit
  this: the token estimate, the mid-run re-scope, `render_agents_full`, and
  `create_data`'s chart instructions.
- Catalog entries had no non-table references (linked instructions,
  resources), which made it harder to choose an entry without opening it.

## Design

| Consumer | Gets |
|---|---|
| Planner (can call `read_instruction`) | `always` instructions in full, plus the `<available_instructions>` catalog of **every** smart instruction (title, description, `tables=`, `refs=`, `tokens=`). Keyword matches only order the list: a hit on the title, labels or tables puts the entry first, marked `matched="title\|labels\|tables"`. |
| Tool-less steps (coder, chart step, other `static.instructions.render()` callers) | `always` instructions, plus the full text of the smart instructions the agent **read** in this report (`InstructionsSection.read_items`). They never see the catalog, because they can't open it. |

- `read_instruction` calls `context_hub.record_instruction_read(item)` for
  smart instructions it returns. Skills and `always` instructions are not
  recorded.
- `ContextHub._load_prior_instruction_reads` seeds the list from this report's
  `on_demand` usage events (latest 10, skills excluded) when a run starts. So a
  follow-up turn's coder still gets a rule the planner read earlier, without
  the planner reading it again.
- The hub keeps its own copy of the section (`model_copy`), because the built
  section can be shared through `_INSTRUCTIONS_CACHE`.
- The planner render (`include_catalog=True`) leaves `read_items` out, since
  the planner already has the text from its own read.
- Smart instructions are no longer pulled in through a reference from a loaded
  instruction.
- Unchanged: `always` instructions, skills, and `describe_tables` (which still
  returns the full text of instructions tied to the tables the agent picked).
- The "Max instructions in context" description now says what it does: smart
  instructions are listed, not loaded. Updated in all locales.

## Loop A — deterministic reproduction (no external services)

The LLM is stubbed at `LLM.inference_stream_v2` / `inference_stream` /
`inference`. It records every prompt and, for planner calls (calls offered
the `read_instruction` tool), scripts tool calls.

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run pytest tests/e2e/test_instruction_catalog.py \
              tests/e2e/test_smart_instructions_planner_prompt.py \
              -m e2e --db=sqlite -q -W ignore
```

- `test_smart_instructions_planner_prompt.py::test_planner_gets_catalog_only_with_matches_first`
  runs a full completion. The planner prompt must not contain the text of a
  title-matched or body-matched rule. The title match must be listed first,
  marked `matched="title"`.
- `test_smart_instructions_planner_prompt.py::test_instruction_read_earlier_in_the_report_reaches_the_coder`
  runs two turns against the Chinook test database. In turn 1 the planner
  calls `read_instruction`. In turn 2 it calls `create_data` without reading
  again. The coder prompt must contain the read rule, and must not contain an
  unread one.
- The builder-level tests in `test_instruction_catalog.py` cover: never
  inlined for any query, ordering and flags, references, table matches, no
  reference pull-in, and `read_items` going to tool-less steps only.

Against the code before this change:

```
FAILED test_smart_instructions_are_never_inlined[revenue by region last quarter]
FAILED test_strong_matches_listed_first_and_flagged
FAILED test_table_scoped_instruction_matches_table_name
FAILED test_smart_instruction_not_pulled_in_by_reference
FAILED test_read_items_go_to_tool_less_consumers_only
FAILED test_planner_gets_catalog_only_with_matches_first          a title match must not be inlined
FAILED test_instruction_read_earlier_in_the_report_reaches_the_coder   the coder must get the rule read earlier in this report
```

With the change: **11 passed**.

Wider run: 375 passed (instruction / skill / catalog / focus / roster /
sub-agent / settings / create_data / artifact e2e), plus 190 related unit
tests.

## What this proves / regression notes

- Within a single run the coder already saw a read rule through "Past
  Observations" (the planner's tool results). The new hand-off is what carries
  it into **later turns**, and into the chart step, which gets no
  observations. The first version of the coder test passed on old code for
  exactly this reason, so it was rewritten as a two-turn test.
- Catalog descriptions fall back to the body's first line when an instruction
  has no description. A one-line rule is therefore still visible in the
  catalog. The tests put their markers on the second line.
- Not verified: answer quality with a real LLM. The open question is how often
  the model reads the entries it should. Run the evals before relying on
  this. Rules that must never be missed belong in `always`.
- Out of scope: `describe_tables` has no cap and doesn't skip instructions
  already read. The 8 unit-test failures on `main` are unrelated.
