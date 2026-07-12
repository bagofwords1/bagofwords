# Feedback Loop — doc artifacts (create_doc / edit_doc + DocViewer + TipTap editing)

Validates the doc-artifacts feature end to end (design: `docs/design/doc-artifacts.md`):
the planner authors markdown documents as `mode='doc'` artifact rows, they render with
live visualizations/mermaid in the app and on the public share page, docs never hijack
dashboard routing, and the report owner can edit them in TipTap with an exact markdown
round-trip.

## Loop A — deterministic (no external services)

Backend contracts (tools, validation, atomic edits, routing filters, doc_edit route
with run-lock):

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_doc_markdown.py tests/e2e/test_doc_artifacts.py -v
# Regression guard for the mode filters touching rerun:
uv run pytest tests/e2e/test_report_rerun_artifact.py -v
```

Observed: 19 unit + 13 e2e + 5 rerun-regression tests PASS.

Frontend DocViewer rendering (markdown + GFM table + live viz embed + `::: columns`
grid + mermaid + fence-aware placeholder parsing), fully route-intercepted:

```bash
tools/agent/boot_stack.sh --dev
cd frontend
npx playwright test tests/reports/doc-artifact.spec.ts
```

Observed: 1 passed. Asserts `.bow-doc` renders (and the JSX iframe does NOT), the
table cell/columns/mermaid SVG are visible, the live placeholder is replaced by a
chart card, and a placeholder quoted inside a code fence stays literal text.

## Loop B — live confirmation (real LLM; ANTHROPIC_API_KEY via env only)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo   # installs Music Store (Chinook)
# Configure an Anthropic provider (claude-sonnet-4-6 default) via POST /api/llm/providers
# with credentials from $ANTHROPIC_API_KEY — never hardcode the key.
# Create a report on the Music Store source and POST a completion:
#   "Investigate what's driving the change in revenue over time ... deliver your
#    findings as a written root-cause analysis document with charts embedded,
#    citations for every claim, and a causal diagram."
```

Observed (2026-07-11, claude-sonnet-4-6):
- Planner ran 4x `create_data` (annual trend, country/year, genre/year, top customers)
  then routed the written deliverable to **create_doc** (not create_artifact).
- Produced "Music Store Revenue Trend: Root-Cause Analysis (2021–2025)": executive
  summary, hypothesis sections (confirmed/ruled-out), 4 live `{{viz:...}}` embeds,
  a markdown table, a mermaid causal diagram, and per-claim citations
  (`Invoice.Total`, `InvoiceLine.UnitPrice × Quantity`, time ranges).
- A follow-up prompt ("add a Methodology section, fix the truncated bullet") routed to
  **edit_doc**: the planner used read_artifact to quote exact text, applied surgical
  find/replace ops (`diff_applied: true`), creating v3 with all embeds intact.
- Owner editing in TipTap: v1 → edit → save produced v2; diffing v1/v2 markdown showed
  all 4 viz placeholders, the mermaid fence and tables preserved (only benign
  normalizations: `~` escaped, table separator style, emphasis re-nesting).
- UI evidence: `media/pr/doc-artifacts/*.png` (report panel, selector, Summary tab,
  /dashboards badge + Docs filter, /r/[id] public page, TipTap editor before/after).

## What this proves / regression notes

- Doc creation/editing is exercised at every layer: tool contract, HTTP route
  (owner-only + 409 run-lock), rendering, and the real planner loop.
- The mode-filter audit holds: with a newer doc present, /latest, report rerun,
  thumbnails, report-level PDF and the planner's active-artifact slot all still bind
  to the latest dashboard (asserted in tests).
- Known environmental quirk: `optimizeDeps` — `tiptap-markdown` must stay excluded
  from Vite pre-bundling (prosemirror duplication) while its CJS dep
  `markdown-it-task-lists` must be *included* (`tiptap-markdown > markdown-it-task-lists`
  in `nuxt.config.ts`); removing that include breaks dev with "does not provide an
  export named 'default'".
