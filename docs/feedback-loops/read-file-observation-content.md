# Feedback Loop — "file-read results come back to me as collapsed previews … so I re-issued the same read, over and over"

An agent asked to review 24 JSON files got stuck re-reading the same files:
each `read_file` succeeded (the UI showed full previews), but the agent
narrated that it only received "collapsed previews (just the first line and a
character count)" and kept re-issuing reads instead of moving on. Claim being
validated: the planner never receives the file content at all — for
whole-file **and** windowed reads.

## Root cause (validated)

The planner's only channel is the tool **observation**, not the tool output:

- `backend/app/ai/agent_v2.py:2664-2665` — `PlannerInput` gets
  `last_observation=observation` and
  `past_observations=…tool_observations`. The `output` dict (where the file
  text lives) is persisted for the UI (`result_json`) but is never handed to
  the model.
- `backend/app/ai/tools/implementations/read_file.py` (`_finalize`, and the
  windowed branch) — read_file's observation is
  `{"summary": "Read <id> — text …", "success": true}`. No content, no
  excerpt. Windowed reads likewise: `"Read window 0–N of M bytes"` with the
  window text only in `output`.
- Cross-turn history digest,
  `backend/app/ai/context/builders/message_context_builder.py:495-500` —
  renders a past read as `"{N} chars"` + a 200-char snippet: literally the
  "first line and a character count" the agent described.

So a whole-file text/JSON read delivers **nothing** the model can read, and
the re-read loop is the model's rational response. (Contrast: `inspect_data`
ships its stdout via `observation.details`, and `grep_files` was fixed the
same way in #651 — read_file has the identical defect.)

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_read_file_observation_content.py` — a stubbed file
client returns a JSON body; the test runs `ReadFileTool.run_stream` and
asserts the observation contains a recognizable excerpt of the content.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run --extra dev pytest tests/unit/test_read_file_observation_content.py -q
```

Observed FAIL on main (`9c3b4789`):

```
AssertionError: read_file observation carries no file content — the planner
only sees: {"summary": "Read rules.json — text", "success": true}

AssertionError: windowed read_file observation carries no window content —
paging is useless to the model; it only sees:
{"summary": "Read window 0–None of 106 bytes from big.log (eof)", "success": true}
```

The failure message IS the bug: that summary line is everything the model
ever sees of a read file.

## The fix

Pending. Proposed (mirrors the `inspect_data`/`grep_files` pattern):

1. read_file observation gains `details` — a bounded excerpt (~4k chars) of
   text/csv, gated on `allow_llm_see_data`, with an honest trailer naming the
   `session_file_id` and how to get the rest (inspect_data / windowed reads).
   Windowed reads put the window text in `details` (that's the point of
   paging).
2. `observation_context_builder.py` gains a read_file compaction case (like
   the existing inspect_data one at line 67-71): superseded observations
   collapse `details` to `"N chars"`, so 24 reads don't stack 24×4k in
   context.
3. The tool description ("Text and JSON are returned inline") is made true.

## What this proves / regression notes

Proves the model-facing channel for read_file carries zero content today, for
both read modes — the re-read loop needs no LLM misbehavior to explain it.
The tests survive as regression tests: they assert the general invariant
("content reaches the observation"), not the incident's specifics.
