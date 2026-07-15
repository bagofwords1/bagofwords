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

1. `read_file.py`: observation gains `details` — a bounded excerpt
   (`_OBS_DETAILS_MAX_CHARS = 4000`) of text/csv, gated on
   `allow_llm_see_data`, with an honest trailer naming the `session_file_id`
   and how to page the rest. Windowed reads ship the window text
   (`_OBS_WINDOW_DETAILS_MAX_CHARS = 8000`) with lossless-paging guidance;
   page-range document reads reuse the same rendering.
2. `observation_context_builder.py`: read_file (and grep_files) history
   compaction — superseded observations collapse `details` to
   `"N chars (already read — do not re-read…)"`.
3. The tool description now truthfully says content is shown, tells the model
   to trust it, and never to re-issue an identical read.

Loop A re-run: **2 passed** (`test_read_file_observation_content.py`).

## Live confirmation (Loop B, Anthropic API)

Full stack + `network_dir` source over seeded files; real Claude Haiku runs:

- "read pricing_rules.json, give the verification_code" → agent answered with
  the exact code buried at char ~8k of the file. Tool trace shows the fix
  working as designed: `read_file` → excerpt boundary → `read_file(offset=4000)`
  → `read_file(offset=7817)` → answer. **Deliberate forward paging, no
  re-read loop.**
- grep + PDF page scenarios in the sibling doc also confirm observation
  details end-to-end.

## What this proves / regression notes

Proves the model-facing channel for read_file carried zero content, for both
read modes — the re-read loop needed no LLM misbehavior to explain it — and
that with content + honest trailers in the observation, a real model pages
forward instead of looping. The tests survive as regression tests: they
assert the general invariant ("content reaches the observation"), not the
incident's specifics.

Pre-existing unrelated failure (reproduces on clean main with these changes
stashed): `test_file_tools.py::TestResolveFileClientIdResolution::
test_rejects_unrelated_id_with_helpful_error` ("'coroutine' object has no
attribute 'first'").
