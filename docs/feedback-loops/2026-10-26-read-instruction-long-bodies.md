# Feedback Loop — "The catalogue came back cut off"

A customer's agent read a long instruction (`devices.yaml`, an app-event
catalogue) with `read_instruction`, then ignored it and went to the warehouse,
scanning `GL_APP_EVENTS_V` with `ILIKE '%connect%' / '%pair%' / '%weight%'`
(~678M rows, ~47s). Its own explanation: "the catalogue came back cut off, so it
only saw part of the events".

## Root cause (validated)

- `read_instruction` returned the whole body in one observation
  (`backend/app/ai/tools/implementations/read_instruction.py`, before this change).
- The transcript cuts every tool result at 100,000 chars
  (`backend/app/ai/context/transcript.py:327`, `:375`), appending only
  `… [truncated at 100,000 of N chars]`.
- The cap is measured on the JSON-encoded observation
  (`backend/app/ai/agents/planner/transcript_bridge.py:122`, `json.dumps` with
  the default `ensure_ascii=True`): newlines cost 2 chars and non-ASCII 6.
- `read_instruction` had no offset or grep, so the agent had no way to reach
  the rest.

## Loop A — deterministic reproduction (no external services)

The e2e tests in `backend/tests/e2e/test_read_instruction_tool.py` build a YAML
event catalogue (2,500 events, a quarter with Hebrew descriptions) and check
what the model receives by rendering the observation through the real
transcript (`build_transcript(...).to_model_messages()`).

```bash
cd backend
export TESTING=true ENVIRONMENT=production
uv run pytest tests/e2e/test_read_instruction_tool.py -m e2e --db=sqlite -q
```

Old code, same catalogue: 315,932 raw chars → 423,509 encoded; the model
received the first 100,000 chars, ending at event 594 of 2,500:

```
… description: Fired when the user completes connect step 594 in the app\n    table: G
… [truncated at 100,000 of 423,509 chars]
```

With the new tests and the old tool: `10 failed, 13 passed`.

## The fix

`read_instruction` now reads long bodies in windows:

- A plain read returns the whole body only while it fits a 40k-char budget
  measured the way the transcript measures it (JSON-encoded). Longer bodies come
  back as the opening lines plus a line-numbered outline, with
  `truncated: true` and `next_offset`.
- `offset`/`limit` reads a line range; `grep_pattern` (+ `before`/`after`,
  case-insensitive by default) returns matching lines. Both are verbatim, so
  `edit_instruction` can anchor on them, and both stay inside the budget.
- Range/grep/outline helpers moved from `read_artifact` into
  `backend/app/ai/tools/implementations/_text_window.py`; `read_artifact`
  keeps its behavior (its unit tests pass unchanged).

After: `23 passed`. Invariants covered: following `next_offset` from line 1
reproduces the body exactly (3 window sizes); grep returns exactly the matching
lines, in order, verbatim, with correct context; no read is ever cut by the
transcript; windowed reads keep the report's data-source scope.

## Loop B — live confirmation (real LLM)

`backend/tests/ai/test_read_instruction_long_live.py` hides three
`scale_pair_*` events ~1,900 entries into a 2,400-event catalogue (past char
100,000), asks "Which app events track smart-scale pairing?" without naming the
tool, and asserts the answer lists all three.

```bash
ANTHROPIC_API_KEY_TEST=... uv run pytest -s -m ai --db=sqlite tests/ai/test_read_instruction_long_live.py
```

Not yet run to completion: the sandbox key returned "credit balance is too
low". Run it with a funded key before relying on agent behavior.

## Regression notes

- Short instructions read exactly as before (no new fields on a full read).
- Not addressed here: the transcript's generic 100k cut still applies to other
  tools that return whole payloads, and an aged-out `read_instruction` result
  still decays to its summary line.
