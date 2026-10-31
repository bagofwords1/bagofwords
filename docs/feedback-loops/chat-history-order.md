# Feedback loop — tools move below the answer after history loads

A published chatbot displayed completed tools below its answer even though the saved completion blocks put the tools first. Read-only production inspection confirmed the order: tools at 21:11:12, 21:11:15 and 21:11:20 UTC, then the final answer at 21:11:23. The deployed backend returns `timeline`; the app uses the shared `ChatMessages` component.

## Validated cause

`frontend/public/libs/artifact-globals.js:2340` (`_chatNormalize`) copied legacy message fields but omitted `timeline`. Initial loads, post-stream reconciliation and older-page loads all use that normalizer. The renderer at line 2237 therefore fell back to legacy text-then-tools rendering. Streaming event state already retained order.

The previous timeline regression had two holes: it compared string positions without proving the tool existed (`-1 < answerPosition` can pass), and its history fixture omitted the legacy tools array. It also needed to await actual history reconciliation, rather than just the end of sending.

## Reproduce and verify

Requires the usual frontend dependencies and vendored runtime libraries. Set `PW_CHROMIUM_PATH` to an installed Playwright Chromium if the default browser is unavailable.

```sh
ONLY='fresh history' CHAT_SCREENSHOT=media/pr/chat-history-order/before.png \
  node frontend/tests/unit/artifactChatTimeline.mjs
```

With the added regression and original normalizer, this fails:

```
Every block must exist once, in server order
actual:   ['Here are the instructions', 'Search repository']
expected: ['Checking the source', 'Search repository', 'Here are the instructions']
```

The fix adds the array-valued `timeline` to normalization. Both page and MCP app library URLs advance to `revision=chat-ui-4` so deployment invalidates the old library cache. No API, database, authentication or execution-order change is required.

```sh
CHAT_SCREENSHOT=media/pr/chat-history-order/after.png \
  node frontend/tests/unit/artifactChatTimeline.mjs
node frontend/tests/unit/artifactConversationReopen.mjs
node frontend/tests/unit/artifactConversationPaging.mjs
node frontend/tests/unit/artifactConversationAnswering.mjs
node frontend/tests/unit/artifactSdkVersion.mjs
```

The timeline suite covers streaming and reconciled history, a fresh conversation load, older-page insertion, legacy messages without a timeline, and custom labels. Assertions compare the complete rendered text/tool sequence, proving no missing or duplicate blocks. Browser transport/provider boundaries are stubbed; the real SDK, hook and shared renderer run in Chromium. The optional `CHAT_VIDEO_DIR` harness setting captures the reconciliation flow.

Before/after images and the flow GIF are under `media/pr/chat-history-order/`. This verifies the client fix locally; production was inspected read-only and has not been deployed by this task.

Observed final results: timeline **4 passed**; reopen **10 passed**; paging **11 passed**; answering/stop **13 passed** — **38 browser scenarios, 0 failures**. SDK URL/hash checks also passed (3 assertions). The isolated fresh-history regression failed before the normalizer fix and passed afterward. No backend change was made, so backend tests were not rerun.
