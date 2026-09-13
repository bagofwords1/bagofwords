# Feedback loop — a separate, simple Agent Card

The prior sign-in view reused AgentConnectionsModal and mixed user access with connection management and discovery logs. The new AgentCardModal is a separate read-only overview with personal sign-in actions.

## Layout and data

The header shows the agent's icon, title and lifecycle stage. An optional full-width description follows it above Connections; the card omits instruction totals. Desktop connection rows align name, formatted resource counts and access; mobile places counts beneath the name. Sign-in is the only per-connection action. The existing management dialog remains behind its existing settings entry point.

`KnowledgeExplorer.vue` opens the card from Sign in for one or many pending connections and now preserves `reliability_status` from the API (previously dropped by its list mapper, which would incorrectly render Training as Production).

Available counts use the embedded connection payload. If an unsigned connection already exposes a completed shared discovery summary, the card can show that count with a shared-catalog explanation. It never fetches privileged catalog contents. Monday catalogs are labeled boards; file/tool shapes use their own nouns. Instructions use the existing viewer-scoped count response. All new labels are in all ten catalogs.

The backend currently provides last-used/last-checked timestamps, not a sign-in timestamp. These are deliberately not displayed as sign-in dates. The component supports a genuine `user_status.signed_in_at` when supplied; current records without that field show Signed in without a date. No backend or timestamp migration is included.

## Runnable verification

With the frontend development server running on port 3100:

```sh
cd frontend
node tests/instructions/agent-card.mjs
node tests/instructions/knowledge-multi-signin.mjs
SINGLE=1 node tests/instructions/knowledge-multi-signin.mjs
node tests/data_sources/connection-signin-status.mjs
```

Run preview scripts sequentially because Nuxt regenerates routes. They mount the real KnowledgeExplorer on temporary preview pages, intercept API boundaries with synthetic data, and clean up the pages afterward. No real provider login or customer credentials are used.

Before capture: `BEFORE=1 node tests/instructions/agent-card.mjs` on pre-card code. Baseline shows the old management dialog. New assertions initially caught the missing Training field in the list mapper. After the fix: stage, description, instruction placement, number formatting, shared board counts, no fabricated dates, exact OAuth target, Hebrew and mobile checks pass. Shared status regression continues to pass.

Evidence: `media/pr/agent-card/{before,after,loading,he,mobile}.png` and `flow.gif`. Vue script/template compilation and `git diff --check` pass.

Compact typography refinement: 512px maximum width, 16px title, 13px connection names, 12px descriptions/status, smaller icons and tighter row/header padding. A synthetic two-connection preview measured 576×292px before and 512×232.5px after at the same 1200×850 viewport. Desktop, Hebrew, and 390px mobile captures are in `media/pr/agent-card-compact/`; mobile has no horizontal overflow. This is a presentation-only change; access and sign-in behavior are unchanged.

Sign-in styling follow-up: subtle top/bottom row dividers group each connector with a pale-blue sign-in action; the connector name also describes its button for assistive technology. ConnectionDetail renders failed shared/personal status labels red while leaving last-checked metadata neutral. Synthetic browser verification confirmed authorization targets the chosen Power BI connection, the error label resolves to red, and the Hebrew/mobile layouts fit. Before/after captures: `media/pr/connection-polish/`. The connection-status regression still passes.

Description refinement: the description is a separate, full-width 13px block with 20px line height and darker neutral text. Removed the instruction-count row. The existing agent-card browser regression checks this layout and sign-in behavior. Matched before/after and Hebrew/mobile captures are in `media/pr/agent-card-description/`.
