# Feedback Loop — Mermaid diagram "DIAGRAM FAILED TO RENDER"

A report doc rendered a `flowchart` whose node labels contained unquoted
parentheses (e.g. `E[חישוב סך ההכנסות<br/>SUM(Invoice.Total)]`). The whole
diagram showed the **"DIAGRAM FAILED TO RENDER"** source fallback in the report
viewer instead of a diagram. This loop validates the cause and proves the fix
rescues the diagram.

## Root cause (validated)

Mermaid's flowchart grammar treats `(` and `)` as structural tokens — they
declare node shapes (`A(round)`, `A([stadium])`). When those characters appear
**unquoted inside a `[...]` label**, the parser thinks a new shape is opening
mid-label, the grammar no longer matches, and `mermaid.render()` throws. The
component catches that throw and shows the source (`DocMermaid.vue:14-17`), which
is the screenshot.

The label just needs quoting — `E["...SUM(Invoice.Total)"]` — which is the
canonical Mermaid fix. This is a frequent LLM authoring mistake, so the doc
markdown stored in the DB already contains hundreds of at-risk diagrams; a
generation-time prompt fix alone cannot rescue those.

Confirmed against the real Mermaid parser (v11.16, the version pinned in
`frontend/package.json`):

```
FAIL  : full diagram (screenshot) -> Parse error on line 3:
FAIL  : minimal: parens in [] label -> Parse error on line 2:
OK    : control: same label quoted
```

Only unquoted **node** labels break — edge labels (`-.text.->`, colons) parse
fine, so the repair is scoped to node labels only.

## Loop A — deterministic reproduction (no external services)

`tools/agent/verify_mermaid_repair.mjs` drives the real Mermaid parser over a
battery of shapes and asserts the flip: **raw fails, repaired parses**, and
valid diagrams stay valid + idempotent. It needs `mermaid` (a frontend dep) and
`jsdom` (Mermaid's parser pulls in DOMPurify, which needs `window`).

```bash
cd frontend
yarn install                       # brings in mermaid@^11
npm i --no-save jsdom              # parser needs a DOM
node --experimental-strip-types ../tools/agent/verify_mermaid_repair.mjs
```

Observed **before** the fix (`repairMermaid` returns the source unchanged):

```
— rescue: raw must FAIL, repaired must PARSE —
FAIL  report diagram (raw failed → repaired STILL FAILS)
...
FAIL
```

Observed **after** the fix:

```
— rescue: raw must FAIL, repaired must PARSE —
ok    report diagram (raw failed → repaired parses)
ok    rectangle + parens (raw failed → repaired parses)
ok    stadium + parens (raw failed → repaired parses)
ok    cylinder + parens (raw failed → repaired parses)
ok    circle + parens (raw failed → repaired parses)
ok    br + parens together (raw failed → repaired parses)

— keep: valid input stays valid, transform is idempotent —
ok    plain labels (parses, idempotent)
ok    already quoted parens (parses, idempotent)
ok    quoted stadium (parses, idempotent)
ok    br only, unquoted (parses, idempotent)
ok    edge pipe label (parses, idempotent)
ok    sequence diagram (untouched) (parses, idempotent)
ok    sequence diagram returned unchanged

PASS
```

## The fix

Two layers — a render-time rescue (fixes diagrams already in the DB) plus a
generation-time nudge (stops new ones):

1. **`frontend/utils/mermaidRepair.ts`** (new) — `repairMermaid(src)` quotes
   unquoted flowchart node labels. Scoped narrowly: flowchart/graph diagrams
   only; node labels only (edge labels untouched); shapes matched
   longest-delimiter-first so `([...])`/`[(...)]` keep their shape; the real
   closing delimiter is chosen at a statement boundary so labels that themselves
   contain the close characters (e.g. `((count(n)))`) are handled; already-quoted
   labels are a no-op (idempotent).
2. **`frontend/components/dashboard/DocMermaid.vue`** — on a render failure, run
   `repairMermaid()` and retry once (fresh render id) before falling back to the
   source box. A repaired-but-still-broken diagram falls back exactly as before,
   so the rescue can only help.
3. **`backend/app/ai/tools/schemas/create_doc.py`** and
   **`backend/app/ai/agents/planner/prompt_builder_v3.py`** — instruct the agent
   to quote flowchart labels containing punctuation.

## What this proves / regression notes

- The reported diagram (and every flowchart shape with unquoted parens) renders
  after the repair; valid diagrams and non-flowchart diagrams are returned
  unchanged.
- The repair is display-only: the stored markdown keeps its original (unquoted)
  source, so "edit diagram source" still shows what the author wrote. The prompt
  fix is what improves the stored source going forward.
- `verify_mermaid_repair.mjs` doubles as the regression test for the repair
  helper — it asserts the invariant (raw-fails / repaired-parses across shapes),
  not the single reported string.
