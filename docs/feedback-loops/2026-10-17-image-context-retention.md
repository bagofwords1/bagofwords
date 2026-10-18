# Feedback Loop — "prompt is too long: 1001304 tokens > 1000000 maximum" while filling a list from scanned PDFs

A list fill over a SharePoint library of ~50 scanned PDFs (Bedrock, Opus 4.8,
1M window, Hebrew files) stopped partway through one turn with
`Conversation too long for this model`. The claim checked here: every page
image `read_file` rendered stayed in the prompt for the rest of the turn, the
newest pages were sent twice, and the budget that should have trimmed the
prompt did not count images at all.

## Root cause (validated)

1. **Images were never released within a turn.** `read_file` renders scanned
   pages to images (`read_file.py` ~591, `render_pdf_pages_images`, up to 8
   pages per read, 1568 px long edge). `AgentV2._buffer_transcript_part` →
   `build_result_part` copies them onto the transcript's `ToolResultPart`, and
   `Transcript.to_model_messages` added every part's images to every later
   request, whatever the part's tier (DIGEST/DROPPED included).
2. **The newest pages were sent twice.** `_VISION_IMAGE_RETENTION_LOOPS = 3`
   only governed the separate `planner_input.images` channel
   (`_collect_vision_images`). By the time it removes `observation["images"]`
   the transcript already holds its own copy, and PlannerV3 sends both
   (`planner_v3.py:252` `images=v3_input.images` next to the transcript
   messages). Live, before the fix: requests carrying 30 and 48 transcript
   images also carried 30 and 48 side-channel copies.
3. **Images were not in the budget.** `ToolResultPart.tokens` counted text
   only, so `fit_to_budget` and `_handle_context_overflow` shrank text and
   left every image in place. A retry after an overflow sent the same images.
4. **The estimate undercounted Hebrew.** `estimate_tokens` was
   `len(text) // 4`. With tiktoken a Hebrew paragraph measured 1.6x
   (o200k_base) to 3.8x (cl100k_base) that number. Tool results are
   `json.dumps`'d with `ensure_ascii=True`, so Hebrew reaches the model as
   `\uXXXX` escapes at ~3.7 tokens per letter, against 1.5 from chars/4.
5. **The "already enumerated" note fired on every folder-scoped listing.**
   `note_enumeration` (`_file_tool_common.py`) keyed only on the connection, so
   folders 2…N of a folder-by-folder walk were each told "already enumerated,
   stop re-listing". Live, before the fix: 8 of 8 folder listings got it.

Cross-turn history was already image-free (`durable_transcript.py` builds
results without images). That is unchanged.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
TESTING=true ENVIRONMENT=production uv run --extra dev pytest -q --db=sqlite \
  tests/unit/test_transcript_image_budget.py tests/unit/test_file_tools.py
```

On `main` (a827680), with the new tests copied in (`image_tokens` stubbed to
2464 and the retention constants defined, because `main` has neither), 16
tests fail for the reasons above:

```
FAILED test_long_scan_run_keeps_images_only_for_recent_steps[40-1|50-4|60-8]
FAILED test_one_huge_step_is_capped_newest_kept
FAILED test_released_image_is_never_sent_again
FAILED test_transcript_size_includes_image_cost          (746 - 714) >= 3 * 2464
FAILED test_fit_to_budget_shrinks_what_is_actually_sent[4|8]
FAILED test_context_overflow_recovery_brings_image_heavy_prompt_under_the_limit
FAILED test_non_latin_text_is_not_estimated_at_english_rates[hebrew|hebrew-long|arabic|cyrillic]
FAILED test_json_escaped_hebrew_is_charged_for_its_escapes
FAILED test_transcript_path_sends_tool_images_once_and_keeps_uploads
FAILED test_folder_walk_is_not_told_it_already_enumerated
```

With the fix, both files pass in full.

## Loop B — live run (OpenAI `gpt-6-luna`, network_dir, API-driven)

Backend only, driven through the API. The key was read from a file into the
provider config at runtime and never written to disk or logs. The setup:

* `network_dir` connection over 8 case folders (`Case-1001…1008`), each with
  `contract.pdf` + `appendix.pdf`, 3 image-only pages each (48 pages; half
  the cases are Hebrew). Generated with Pillow, no text layer.
* An agent list "Case summaries" (key `case_folder`, plus client, document
  type, amount, sign date).
* Prompt: *"Go folder by folder: list the folder, read every scanned page of
  each PDF in it, and save one row per case folder…"*
* Measurement: a launcher wraps `LLM.inference_stream_v2` and records, for
  each planner request, the image blocks inside the messages, the
  side-channel images, and the provider-reported input tokens. The same
  launcher ran both trees. No app code changed for this.

| Run | Planner calls | Max images in one request | Side-channel duplicates | Max provider input | Input on the last call | Total input | Rows | "already enumerated" on folder listings |
|---|---|---|---|---|---|---|---|---|
| `main`, window 64k | 19 | **96** (48 + 48 dup) | 144 | **251,970** | 165,266 | 2.80M | 8/8 | 8 of 8 |
| fix, window 64k | 48 | 24 | 0 | 100,653 | 77,732 | 3.45M | 8/8 | 0 of 8 |
| fix, real window (1.05M) | 36 | 24 | 0 | 121,385 | 76,895 | 2.70M | 8/8 | 0 of 8 |

On `main`, every image ever read stayed: from the first read onward each
request carried all 48 pages (~107k tokens), and the newest batch rode twice.
With the fix, requests hold at most 24 pages, only from the last 3 steps.
Older images drop out as the run moves on, so late requests carry 0–3
images and the prompt stops growing with the number of pages read. The
customer's run read ~50 PDFs; on `main` that growth is linear in pages read
and reaches 1M. With the fix it is bounded by 24 pages plus text.

The estimate tracks what the provider bills. At the first image-heavy request
in the 64k run the transcript estimate was 58,130 tokens. Add ~42k for the
system prompt and tools (call 0 billed 43.6k against a 1.5k transcript) and
the total is ~100k, against 97,356 provider-reported. The Anthropic
28×28-patch formula (2,240 tokens per page here) is close to what OpenAI
billed for these pages (~2.1k).

**Re-reading released pages works.** After pages aged out, the model re-read
them as the note says: `read_file` with the same `file_id` and
`page_range="1-3"` (or `"1"`), 20 such calls in the real-window run. Then it
saved the rows.

**Behaviour change to know about:** when one parallel step reads more than 24
pages (here 16 PDFs × 3 = 48 in one batch), the cap releases the rest at once.
The model then goes back and re-reads those files page-range by page-range.
That costs extra planner calls: 36 vs 19 at the real window, with about the
same total input (2.70M vs 2.80M) because each call is smaller. In the 64k run
the budget (32k for the transcript) also released the previous steps' pages
on every request (`[transcript] decayed … images_released=N`). The model saw
only its newest step's pages and re-read more: 48 calls, 3.45M.

### Overflow recovery, live (fault injection)

Same setup with the model window set to 120k, plus
`BOW_AGENT_LOOP_FAULTS=1 BOW_AGENT_LOOP_FAULT_KIND=context
BOW_AGENT_LOOP_FAULT_MIN_INDEX=6`, which injects one Anthropic-style
`prompt is too long` at loop 6. In both trees the injected overflow fired
early, while the transcript was still under the shrunk budget (≤9 images), so
it had nothing to decay. Live coverage of the overflow path is therefore weak.
The unit test `test_context_overflow_recovery_brings_image_heavy_prompt_under_the_limit`
covers the mechanism.

The two runs are still useful as a second sample. This time the model read
one page per step and wandered, and **both** trees hit the 100-step limit
(`planner_step_limit`) after saving all 8 rows:

| Run | Calls | Max images in one request | Side-channel duplicates | Max provider input | Total input | `read_file` calls | "already enumerated" notes |
|---|---|---|---|---|---|---|---|
| `main` | 99 | 75 | 216 | 260,745 | 16.7M | 24 | 44 |
| fix | 99 | 9 | 0 | 85,178 | 6.4M | 57 | 2 (both `search_files`) |

**Observed cost of step-based retention:** when the model reads one page per
step with list/note/submit steps in between, a page is out of context 3 steps
later. In the fix run the model re-read the same pages several times
(`Case-1001/contract.pdf` page 1 four times): 57 reads against 24 on `main`.
Input was still 2.6x lower in total and bounded per request. A window counted
in *image-bearing* steps, or simply the newest 24 images whatever the step,
would keep the last few pages alive across text-only steps. That is a
follow-up decision and is not part of this change.

### Follow-up 1 — retention by image count (shipped in 1eec3a1)

The 3-step window above was replaced: the newest `MAX_RETAINED_IMAGES` (24)
tool images stay whatever step they came from. Whole results only, newest
first, strict recency; the newest image-bearing step is always kept (even past
24) and is never decayed by `fit_to_budget`.

One-page-per-step A/B on gpt-6-luna. Prompt: "read each PDF one page at a
time, note after each PDF, save each folder with submit_case_summaries". The
previous commit (3-step window) ran against this change. One run each; both
runs drifted:

| Run | Calls | Rows | `read_file` calls | Repeat reads | Most reads of one page | Max images/request | Max input | Total input |
|---|---|---|---|---|---|---|---|---|
| 3-step window | 93 (finished) | 8/8 | 58 | 10 | 3 | 3 | 95,554 | 6.5M |
| newest-24 | 100 (step limit) | 4/8 | 51 | 15 | 6 | 24 | 140,797 | 10.4M |

An earlier pair (note after every page) also hit the step limit on both sides.
The 3-step run saved 4 rows with 4 repeat reads. The newest-24 run made no
repeat reads, but it searched MCPs for the list tool and saved nothing, even
though `submit_case_summaries` was registered.

**The new rule did not reduce re-reads in these samples.** Every repeat read
in the newest-24 run (15 of 15) happened while that page was **still in
context** under the 24-image rule. Several were the same page read
back-to-back up to 6 times. So these re-reads are model behaviour, not
retention. One hint: in an earlier run the model's note called a tool-read
page "visible in the user's attached screenshot". Tool images are hoisted into
the user turn next to the `tool_result`, so the model may not tie the picture
to the read. This was not investigated further. The new rule does cost more
per request (up to 24 pages held: 141k vs 96k max input), as designed.

### Follow-up 2 — OpenAI context-length recognition

A deliberately oversized request (~1.2M tokens) to `gpt-6-luna` returns
`code: context_length_exceeded` with wordings that the old markers missed:

* Responses API: `Your input exceeds the context window of this model. Please
  adjust your input and try again.`
* Chat Completions: `Input tokens exceed the configured limit of 922000
  tokens. Your messages resulted in 1200007 tokens. …`

1eec3a1 matched the structured code only together with an HTTP 400. On the
planner's real path that never holds. The Responses **stream** reports the
rejection as an `error` event. The SDK raises a bare `openai.APIError` (no
`status_code`, `code=context_length_exceeded`), and
`LLM.inference_stream_v2` re-raises it as `RuntimeError("LLM v2 streaming
failed …: <message>") from e`. The outer exception has no status and no
code, so the live classification after 1eec3a1 was still `unknown`, and
`_handle_context_overflow` did not run. fb9f621 walks the
`__cause__`/`__context__` chain for the structured code and accepts it
without a status.

Live, through the app's own `LLM`/`OpenAIResponsesClient`
(`real_overflow_harness`: a 30-result transcript plus a 4-page read, est
764k tokens, digit-dense so ~1.2M real):

```
first request rejected: RuntimeError
   chain: openai APIError status None code context_length_exceeded
  classify -> unknown          (1eec3a1)
  classify -> context_length   (fb9f621)
  _shrunk_context_factor -> 0.85   (Responses wording has no numbers: geometric)
  _handle_context_overflow: transcript 764113 -> 437517 est tokens; images 4 kept (newest step)
retry accepted, provider input_tokens 873284
```

The retry fit, but OpenAI billed 873k for a 437k estimate. Digit-dense text
runs ~2.2 chars/token, which chars/4 undercounts 2x; JSON-heavy tool results
are the same. A second overflow would still have converged (×0.85 per
attempt). An attempt to reach a real overflow through a full agent run
(24 Hebrew text reads) only reached 656k tokens. `read_file` caps
observation text at 4k chars, so text reads cannot fill a 1M window.

## The fix

* `app/ai/context/transcript.py` (as of 1eec3a1): `MAX_RETAINED_IMAGES = 24`,
  `retained_prefix()`. `enforce_image_retention()` runs on every
  `add_tool_results` and keeps the newest 24 images in whole results, newest
  first, strict recency; the newest image-bearing step is always kept.
  Decayed (non-FULL) results lose their images. Releases are permanent, on
  the live transcript. `to_model_messages`
  renders images only for FULL results. `fit_to_budget` gains two rungs:
  release images outside the protected tail first (oldest first), and as a
  last resort release images inside the tail, but never the newest step's,
  so the model always sees what it just read. `floor_tokens` counts what those
  rungs can free. Stats report `images_released`.
* `app/ai/context/parts.py`: `ToolResultPart.image_tokens()` returns
  ceil(w/28)·ceil(h/28) from the PNG/JPEG header, capped at a 1568 px edge,
  with a fallback of 3,136 when the size can't be read. `Turn.tokens()`
  includes it. `release_images(keep)` sets the image count and appends a note
  to `model_text()`: *"[N page image(s) from this result were removed from
  context to save space; the call itself completed. To look at them again,
  call read_file again with the same file_id and page_range.]"*.
  `estimate_tokens` is script-aware: ASCII stays at /4, each non-ASCII char
  costs 1, each `\uXXXX` escape costs +2. Text parts use it too.
* `app/ai/agent_v2.py`: `_planner_images` drops tool images from
  `planner_input.images` when the request renders the native transcript
  (PlannerV3 + transcript path). User uploads always stay. The legacy planner,
  `BOW_PLANNER_TRANSCRIPT=0` and knowledge mode keep tool images on that
  channel, now under the same newest-24 rule (one unit per step).
  `_handle_context_overflow` logs `images_released`.
* `app/ai/llm/errors.py` (1eec3a1 + fb9f621): structured
  `context_length_exceeded`, read from the exception or its cause chain, maps
  to `context_length`. The current OpenAI wordings are markers too.
  `agent_v2._CONTEXT_OVERFLOW_PATTERNS` parses the Chat Completions
  "configured limit … resulted in" numbers.
* `app/ai/tools/implementations/list_files.py` + `_file_tool_common.py`:
  `note_enumeration(…, scoped=True)` for a `folder_id` (other than root) or a
  `cursor` page neither notes nor records the listing. Repeated
  whole-connection listings are still flagged, and so is `search_files`.

## What this proves / regression notes

* Unit suite (fb9f621): 8455 passed. Earlier: 8436 passed. The 40 failures all fail identically on a clean
  `main` checkout in this sandbox: the 8 known ones, plus
  `test_obo_*` / `test_discovery_progress_contract`, which need `libodbc`.
  Nothing new fails.
* File e2e (`-m e2e`): network_dir, SharePoint 2010/on-prem, file browse,
  file upload, vision toggle — 48 passed.
* Not verified live: Anthropic/Bedrock (the customer's provider). Image cost
  is modelled on Anthropic's documented formula, and the overflow recognition
  for Anthropic messages is unchanged.
