# Feedback Loop — report reading proportions and streaming jumps

The report conversation uses a short opacity fade for appended text. Stream following must also respond to child layout growth while leaving a reader who scrolls upward in control.

## Root cause and observed baseline

`frontend/pages/reports/[id]/index.vue` previously used a 672 px outer column, 13 px body text, and 24 px top-level headings. The installed renderer's unconfigured text entrance duration was 900 ms.

The scroll scheduler queued an animation frame followed by a fixed 40 ms timer and an explicit layout read. Token events requested scrolling, but later child layout growth had no content-size observer. An upward wheel gesture with no available upward movement could also leave following enabled because no scroll-position change occurred.

The baseline browser recording measured a **316 px bottom gap** after a late child grew. A subsequent upward gesture followed by more growth moved the viewport from **0 to 516 px**. These are observed layout/follow failures; this loop does not measure model delivery latency.

## Deterministic loop

Use a fresh local test database and the real frontend/backend. No model provider or customer data is needed.

```bash
tools/agent/boot_stack.sh --dev
cd backend
uv run python ../tools/agent/seed_org.py
cd ../frontend
# Set BOW_TEST_EMAIL and BOW_TEST_PASSWORD to the throwaway seeded credentials.
node tests/reports/reading-stream-evidence.mjs after
```

The macOS verification used the existing backend virtual environment, a separate SQLite database at `/tmp/report-reading-evidence.db`, migrations, and `uvicorn main:app` on port 8000. The frontend ran on port 3000. The boot helper's Linux process-launch commands were replaced with direct local launches.

The browser runner creates an empty demo report through the real API, then supplies a deterministic transcript and word-sized deltas through the production event handler. This tests rendering and scrolling independently of network chunking and model behavior. It also adds a child that grows without a new token event.

Run `before` against the unchanged source to record the baseline. Run `after` against the implementation to enforce the general invariants: late growth stays within 4 px of the bottom while following; growth leaves a detached reader within 2 px of their chosen position; finalizing a block preserves its rendered text; reduced motion leaves appended text fully visible; mobile has no page overflow; Hebrew remains RTL; compact prose stays 13 px; and no browser errors occur.

The same transcript is captured at 1440 × 960 for light, dark, and Hebrew, with additional narrow-pane and 390 × 844 mobile captures. Evidence lands in `media/pr/report-reading-stream/`; credentials remain in environment variables and are not written to evidence.

## Changes and observed pass

- Typography and proportions (column width, text and heading sizes, split-pane widths) are unchanged; an earlier revision of this PR changed them and was reverted.
- Appended text and node entrance opacity fade: **900 → 180 ms**.
- Scroll requests share one pending animation frame after the render commit. Content and viewport resize observation handles deferred layout growth. An upward wheel gesture that reaches the conversation releases following immediately and cancels a queued forced scroll; one consumed by a nested scroller (it can still scroll up, or blocks chaining with `overscroll-behavior`) leaves following on. The position-based scroll handler remains the fallback for other inputs. `node tests/reports/wheel-ownership.mjs` checks nested scrolling, a nested top boundary with and without containment, and upward scrolling directly over the conversation.
- Reduced-motion users get appended text without the fade.

Final browser output: **late bottom gap 0 px**, detached position **14 → 14 px**, mobile overflow **false**, reduced-motion animation **none** and opacity **1**, compact font **13 px**, Hebrew direction **rtl**, browser errors **0**. The existing six kickoff-stream regression checks also passed.

`yarn build` completed successfully. A separate production smoke supplied completed transcript data through the completions API boundary, confirming (before the typography revert) the minified equivalent `.18s` fade, a 0 px late bottom gap, a detached position of 180 → 180 px, and zero browser errors. Those measurements are saved in `production.json`; the settled dark-mode capture is `production-dark.png`. Detailed token playback uses development component inspection and is intentionally run against the dev server.

Implementation entry points: `:3061` (scroll coordinator), `:3107` (resize observation), and the `.markdown-content` styles (fade timing). Activity presentation lives in `frontend/components/BlockGroupTicker.vue`.

## Evidence

| Before | After |
| --- | --- |
| ![Before](../../media/pr/report-reading-stream/before-en.png) | ![After](../../media/pr/report-reading-stream/after-en.png) |

![Streaming preview](../../media/pr/report-reading-stream/after.gif)

Additional matching screenshots, full recordings, and measured results are saved beside these files. The demo transcript is injected for verification and is not persisted as a generated report answer.

## Sent turn pinned near the top

Sending a prompt eases it to 48 px below the top (a line of the previous answer stays visible) of the conversation (380 ms ease-out; the bubble rises in over 260 ms) and the answer streams below it without the view chasing the bottom. If the prompt is already visible with at least 35% of the view free below it, nothing moves. The room the prompt needs is a `min-height` on the transcript, not a spacer element: a spacer is only resized after layout, so the brief collapse while an answer's first block mounts clamped the scroll position to 0. The floor keeps 64 px of slack (the view grows when the composer's "Working" row disappears) and only shrinks when the reader scrolls. Wheel/touch, the jump pill or the next send end the hold; a reload lands at the bottom without pinning.

Verified against a local OpenAI-compatible stub that streams a short or long answer, at 1280 × 800, with the side panel open, and at 390 × 844 (mobile): no scenario moved the view on its own; the held prompt stayed at its position for the whole stream and the view did not move when the answer finished; prompts with room below did not scroll; reload landed at the content bottom.

| Before | After |
| --- | --- |
| ![Before](../../media/pr/report-reading-stream/pin-before.gif) | ![After](../../media/pr/report-reading-stream/pin-after.gif) |

