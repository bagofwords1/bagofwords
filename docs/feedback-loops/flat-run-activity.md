# Flat run activity and streaming tool groups

Activity previously wrapped a tool's own header in another result summary,
repeating titles, durations and success badges. The run also offered a steps
control before any tool call existed.

## Implementation

- `frontend/composables/useRunActivity.ts`: consecutive tool calls form a stable
  group anchored to its first block. Commentary and steering break groups.
- `frontend/components/RunToolGroupRow.vue`: only multiple calls get a group row. A single call renders its own tool header directly.
  While streaming, title and icon follow the current running call; completed
  multi-call groups summarize their categories and result/query counts only when the run is confirmed finished. Progress prose does not trigger a summary. Between calls the specific title stays visible.
- `frontend/composables/useRunFold.ts`: live groups remain visible; opening a
  group reveals the tool components with their own disclosures.
- `frontend/components/RunActivityToggle.vue`: only a thinking circle before
  tools arrive; finished duration sits on its own line with the same typography and disclosure styling as tool groups.
- Report and shared-link transcripts remove the extra step wrapper and rail.
  Generated code stays collapsed behind its own disclosure; result metadata and the preview open with the tool.

## Manual reproduction and verification

With the local stack running, open a report using the Music Store demo source.
Ask for the top ten customers by total spend, then a question requiring several
consecutive tool calls.

1. Before the first tool call, expect a single dark thinking circle and no
   elapsed timer or steps control.
2. Consecutive calls update one group row. Its icon follows the latest call,
   and the label summarizes the categories used so far.
3. A single call opens its details directly. For multiple calls, expand the group: individual calls start collapsed. Open a data call to see
   execution details and ToolWidgetPreview at the bottom. Generated Code stays collapsed until explicitly opened.
4. Commentary separates groups and shares the answer's left edge.
5. At completion, duration sits on its own line below the smaller model label. The duration
   reopens activity; citations and result links remain available.
6. Repeat on a shared link and in Hebrew, including keyboard disclosure.

## Streaming presentation

Single data calls and group headers share icon sizing and label spacing. The
label is a direct text node with a brief opacity change when its text changes; it never fades to invisible. While running, a gradient is clipped
to the letters; it never paints a rectangular background. Hover and keyboard
focus pause the shimmer and render the normal assistant-text color. Reduced
motion disables shimmer and label animation.

## Verification status

The initial flattening was inspected locally before the grouping design was
revised. The final grouping and typography changes have not been tested or
browser-verified: the user explicitly requested manual verification instead.
No final before/after evidence or passing result is claimed for this revision.

## Hover disappearance correction

The removed label component used `:global(.activity-control:hover)` followed
by a descendant selector in scoped CSS. Vue's scoped-style compiler replaces
that entire selector with the argument of `:global`, applying the descendant's
`opacity: 0` to the whole hovered button. Its stylesheet could remain mounted
during hot reload even after removing the component. The current controls use
`bow-activity-control` and ordinary descendant rules in the global stylesheet,
so they cannot match that stale selector. No browser verification was run.

## File header consistency

Read-file headers use the same body-text token, 1.5 spacing gap, leading
book icon and trailing chevron as group rows. Running and completed states
share one header element; connector and format metadata live inside the
expanded details rather than changing the header dimensions on completion.
Data creation and direct query calls share the counted “Ran {count} queries”
summary category. This revision is left for manual verification.

## File row spacing and errors

A single file header previously received a 4px tool-container margin and an
8px body gap; becoming a group changed these to 0px and 4px. Both paths now
use the same container margin and body gap. The file component uses a flex
column so its inline header cannot introduce an extra text baseline gap.
File-read failures replace the leading book icon with a small warning icon.
The error message is inside the disclosure, and even failures without content
or arguments remain expandable. Verify these states manually; no tests run.

## Shared tool headers

All tool renderers and grouped activity now use `frontend/components/tools/ToolCallHeader.vue` for typography, icon spacing, disclosure, text shimmer, hover/focus, and warning icons. Individual tool components retain their specific expanded content. Unknown tool names use GenericTool on both report and shared pages. Approval and clarification forms remain visible when they require input, and cancel/stop actions remain reachable next to the header. Disclosure state is explicit and starts collapsed; stream updates do not open or close the details.

Manual verification requested by the user: compare list/read/search file calls, instructions, queries, browser and MCP calls; expand each to inspect its original content; check running-to-completed transitions, hover, grouped calls, and failed calls. Also check pending approvals and clarification forms. No tests, builds, or browser checks were run for this change.

### Shimmer and composer refinement

The shared tool-header shimmer now uses a soft, higher-contrast band with a 1.1-second sweep (previously 1.6 seconds). Hover/focus still restores solid text, and reduced-motion disables the shimmer. The default composer starts at one line, retains automatic growth and explicit row overrides, uses tighter internal padding, and sits 8px above the bottom instead of the previous 32px desktop padding. No tests or browser verification run; manual verification remains with the user.

## Markdown tables

The shared Markdown renderer now registers ChatTable and ChatTableView. Tables use content-sized columns, horizontal overflow within the chat width, a 400px maximum inline height, sticky headers, horizontal separators, and conditional edge fades. Copy exports the table Markdown; expand opens the same table in a larger modal. Inline cell content still uses the Markdown renderer, preserving formatting and links. Report/shared-page grid overrides were removed. New labels are supplied in all ten locale catalogs.

Manual checks: narrow and 20-column tables; long filenames; dates/amounts; many rows; scrolling while tokens arrive; copy, expand, close, and keyboard navigation; light/dark and RTL layouts. No tests, builds, or browser checks run, per user instruction.

## Answer typography

Report and shared-link prose now share `frontend/assets/css/chat-prose.css`: semibold emphasis, neutral charcoal text, 1.6 line height, 0.25em list-item spacing, and 0.9em paragraph spacing. Headings and code scale with the reading preference. Ordinary prose wraps normally; links/code and table filenames retain overflow handling. Assistant avatars are smaller and the report model label has less space beneath it. Tool hover and table body text use the same answer ink. No tests or browser checks were run; manual verification remains pending.
