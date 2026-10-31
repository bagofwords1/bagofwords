# Feedback Loop — "The user just sent an image" (sent 9 turns earlier)

A user uploaded a header-row screenshot on turn 6 of a conversation. On turn 15
("אחלה צור את הרשימה") the planner's reasoning opened with *"The user just sent
an image showing a header row…"*. The claim validated here: a picture from an
earlier turn was re-sent with **every** later turn, on the newest user message,
so it read as part of that message.

## Root cause (validated)

- `AgentV2._load_images_as_input` (`backend/app/ai/agent_v2.py`) attached the
  current turn's uploads, and when a turn had none it fell back to
  `_followup_image_files()`: the 2 newest images on the whole report, with no
  limit on how old they were.
- That channel (`planner_input.images`) is rendered onto the **newest** user
  message with `<images>N image(s) attached…</images>`
  (`backend/app/ai/agents/planner/prompt_builder_v3.py`), so nothing told the
  model the picture came from an earlier turn.
- The trace's saved context for turns 13–15 still listed `image.png`, and the
  turn-14 reasoning also worked from "the image columns".

Earlier images remain reachable without this fallback. History tags each user
message with what it carried, e.g. `[attached: zebra.png (image/png, id=…)]`
(`message_context_builder.py`, `_attachments_by_completion`), and `read_file`
shows the image again.

## Loop A — deterministic (no external services)

```bash
cd backend
TESTING=true uv run pytest tests/unit/test_vision_image_retention.py -q
```

`test_earlier_uploads_are_not_reattached_to_a_turn_without_its_own` (1, 3 and
9 earlier turns) and `test_a_turn_attaches_exactly_its_own_uploads`.
Against the old code (fix stashed):

```
E       AssertionError: assert ['img0'] == []
E       AssertionError: assert ['img0', 'img1'] == []
3 failed, 8 passed
```

With the fix: `30 passed` (this file + `test_connector_file_cache.py`).

## Loop B — live, Claude Haiku 5.5 (real Anthropic key)

The scripts are in `docs/feedback-loops/image-followup-reattach/`:

- `count_proxy.py` sits in front of api.anthropic.com via
  `ANTHROPIC_BASE_URL`. It logs, for each request, how many image blocks it
  carries and in which message.
- `start.sh` resets the DB, starts the proxy and the backend, and seeds an org.
- `drive.py` adds an Anthropic provider, makes `claude-haiku-5-5` the default,
  uploads an orange PNG reading `CODE: ZEBRA-7341` on turn 1, then sends 4
  text-only turns.

The key is read from a file passed as an argument. It never goes in the repo
or the logs.

```bash
docs/feedback-loops/image-followup-reattach/start.sh /tmp/proxy.jsonl
cd backend && uv run python -I ../docs/feedback-loops/image-followup-reattach/drive.py \
  run <key-file> /tmp/bow-agent/backend.log /tmp/out.json /tmp/proxy.jsonl
```

Before (old code):

```
TURN 1 upload    | requests: 3 | with image: 1 [(0, 'user')] | 'CODE: ZEBRA-7341'
TURN 2 no-upload | requests: 3 | with image: 1 [(0, 'user')] | 'Paris'
TURN 3 no-upload | requests: 3 | with image: 1 [(0, 'user')] | 'Octopuses have three hearts…'
TURN 4 no-upload | requests: 3 | with image: 1 [(0, 'user')] | 'ATTACHED: no. …'
TURN 5 no-upload | requests: 3 | with image: 1 [(0, 'user')] | 'Orange'
```

After (fix):

```
TURN 1 upload    | requests: 3 | with image: 1 [(0, 'user')] | 'CODE: ZEBRA-7341'
TURN 2 no-upload | requests: 3 | with image: 0 []            | 'Paris'
TURN 3 no-upload | requests: 3 | with image: 0 []            | 'Octopuses have three hearts…'
TURN 4 no-upload | requests: 3 | with image: 0 []            | 'ATTACHED: no. …'
TURN 5 no-upload | requests: 4 | with image: 1 [(0, 'user')] | tools: ['read_file'] | 'Orange'
```

`(0, 'user')` means the newest message. On turn 5 after the fix, the image
arrives inside the `read_file` tool result, which the model asked for itself.

## The fix

`_load_images_as_input` attaches only the images uploaded with the current
message. `_followup_image_files` and `_FOLLOWUP_IMAGE_LIMIT` are removed. When
the lookup can't be scoped (no head completion, or the DB query fails), it
attaches nothing; before, it sent every image on the report.

## What this proves / regression notes

- The re-send is gone: turns without an upload send no user image.
- An earlier image is still answerable. The model reads the history tag and
  calls `read_file` (turn 5), at the cost of one extra request.
- Haiku 5.5 did **not** reproduce the "you just sent an image" wording in this
  short run: it answered "ATTACHED: no" on both builds, guided by the
  `[attached: …]` history tag. The original report was a longer run on a
  different model. The fix removes the mislabeled input either way.
- Pre-existing failures that also fail with the fix stashed (unrelated):
  `tests/unit/test_sharepoint_2010_email.py` (4) and
  `tests/unit/test_obo_background_database.py` (2).
