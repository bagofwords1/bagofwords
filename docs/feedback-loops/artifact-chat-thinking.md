# Artifact chat thinking: stream separately, display only on request

PR #1532 follow-up. Baseline `9f38afdb0`. The user requested thinking in the
conversation API/SDK and chatbot skill guidance to keep it hidden by default.

## Validated gap

`backend/app/services/artifact_conversations.py` accepted only `content` in
its stream translator and omitted block reasoning from history.
`frontend/public/libs/artifact-globals.js` neither accumulated thinking events
nor rendered a thinking section. A working indicator and tool steps were not
reasoning streaming.

## Deterministic loop

Use the repo's Python 3.13 environment and worktree-local frontend dependencies.
No external provider or Entra credentials are required.

```sh
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true python -m pytest \
  tests/unit/test_artifact_conversation_stream.py \
  tests/unit/test_artifact_chat_components_reference.py \
  tests/unit/test_skill_catalog_library.py -q --disable-warnings
cd ../frontend
# Set PW_CHROMIUM_PATH if the installed Playwright browser is elsewhere.
node --test tests/unit/artifactChatThinking.mjs \
  tests/unit/artifactChatComponents.mjs tests/unit/artifactUseConversation.mjs
```

Before implementation: backend failed with `KeyError: 'thinking'`; the browser
contract test timed out because answer text arrived but thinking did not.
After implementation: **81 backend tests passed; all three frontend files
passed**. The tests cover separate text/reasoning channels, multiple block
append/replacement, empty replacement, stored history, omitted showThinking,
collapsed opt-in display, live updates while expanded, and hiding again.
Existing conversation tests cover stop and completion races.

The first SDK reference edit exceeded its 10,500-character budget. Shortened
wording, preserved the budget, and reran the reference tests successfully.

## Contract

- `thinking_delta {block,text}` appends to that reasoning block;
  `thinking {block,text}` replaces it (including an empty replacement).
- `bow.conversations.send` forwards these additive events through the existing
  SDK transport. Custom event consumers should ignore unrecognized types.
- `useConversation` exposes `message.thinking` separately from `text` and
  narration `steps`. History carries the same ordered reasoning text.
- `ChatMessages` and `ChatMessage` require `showThinking={true}` to render a
  collapsed section. Omitted/false renders no thinking content. Plain-text
  rendering escapes markup. `labels.thinking` localizes its heading, matching
  the existing artifact label contract (independent of Vue locale catalogs).
- The chatbot skill keeps this off unless requested. The display prop neither
  enables model reasoning nor guarantees a provider emits it.
- Hidden UI is not a disclosure restriction: authenticated viewers still
  receive thinking. Existing viewer/ownership checks remain in force.
- No migrations or new model-generation controls; no changes to Stop.

## Browser evidence

A local component preview uses real vendored React and artifact globals with
explicitly synthetic text. To reproduce, serve a temporary directory containing
`tools/agent/embed/thinking-preview.html` as `index.html` and a `libs` symlink to
`frontend/public/libs`, using `python -m http.server 4180 --bind 127.0.0.1`.
Open localhost:4180. The preview initially requests thinking display to expose
the before/after difference; click Hide thinking to inspect the default UI.

Native browser checks confirmed collapsed/expanded/hidden states and Hebrew
label/text direction. Screenshots are in `media/pr/erp-embed-entra/thinking`.
The GIF is a sequence of captured UI states, not a live-token video.

This verifies event translation, persisted reasoning and the SDK/component
contract. It does not claim a new live-provider/OBO run, production browser
matrix or that every provider emits reasoning. Earlier ERP verification is in
`erp-embed-entra-customer.md`.
