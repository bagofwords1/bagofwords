# Doc Artifacts — markdown documents as a first-class artifact type

Status: proposed
Scope: `create_doc` / `edit_doc` tools, backend persistence, and frontend viewing/editing.
Out of scope (deliberately): the agent scratchpad (separate design), block-based storage,
real-time collaboration, per-paragraph comments, a doc-writer sub-agent.

## Summary

Add a third artifact mode — `doc` — alongside `page` (dashboards) and `slides`.
A doc is a **markdown document authored directly by the planner** (no designer/codegen
hop), stored in the existing `artifacts` table, rendered by a new `DocViewer` in the
right-hand artifact panel, listed in the artifact selector and the Summary tab, and
editable in place by the report owner via TipTap.

Design principle settled during review: **markdown is the source of truth; structure by
convention** (`{{viz:<uuid>}}` placeholders, ` ```mermaid ` fences, `:::columns`
containers). Blocks exist only as TipTap's runtime representation — never in storage.
This matches how Claude artifacts (`text/markdown` type) and ChatGPT Canvas store
documents, keeps authoring LLM-native, and keeps export (`.md`/PDF/docx) trivial.

## Data model

No migration. Reuse `artifacts`:

- `mode = 'doc'` (column is already a free string)
- `content = { "markdown": str, "visualization_ids": [str] }`
  (`visualization_ids` extracted server-side from placeholders — same key dashboards
  use, so existing viz-data/query filtering paths work unchanged)
- Versioning as today: new row per edit, `version` incremented, newest row wins.
- One doc *lineage* is NOT enforced — a report may have multiple docs, same as
  dashboards; the selector lists them all.

## Backend

### B1. Tool schemas (`app/ai/tools/schemas/create_doc.py`, `edit_doc.py`)

```
CreateDocInput:  title: str, markdown: str
CreateDocOutput: doc_id, title, version, visualization_ids
EditDocInput:    doc_id: str, markdown: str        # v1: full replacement
EditDocOutput:   doc_id, title, version, visualization_ids
```

Validation (both tools):
- Extract `{{viz:<uuid>}}` placeholders **skipping fenced code blocks** (a doc quoting
  a placeholder inside ``` must not hydrate it).
- Every referenced viz must exist, belong to the report, and have a successful step
  (same rule `create_artifact` applies). Unknown ids → field error so the planner
  retries with corrected ids.
- Size cap (~50k chars) with a clear error message.

Full-replacement edits are acceptable v1 because docs are written mostly once, near the
end of a run; if long docs make replacement costly later, add `append` /
`replace_section` ops (heading-anchored, as designed for the scratchpad) — the tool
shape allows it without breaking changes.

### B2. Implementations (`app/ai/tools/implementations/create_doc.py`, `edit_doc.py`)

Follow `CreateArtifactTool`'s structure (Tool base + ToolMetadata) minus the designer:
- `category="action"`, `tags=["artifact", "doc"]`, `allowed_modes=["chat", "deep"]`.
- Writes the `Artifact` row directly (status `completed`); no second LLM call, no JSX,
  no sandbox screenshot in v1.
- Observation returned to the planner: `doc_id`, title, heading outline, viz count —
  enough to reference and edit later without re-reading.
- `edit_doc` resolves `doc_id` to the lineage and inserts the next version row
  (mirrors `edit_artifact`'s versioning, not its coder pipeline).

Registration is automatic via `implementations/__init__.py` auto-import; export the
schemas from `schemas/__init__.py`.

### B3. Planner prompt (`prompt_builder_v3.py`)

- Terminal-deliverable routing rule: "dashboard/monitor/track" → `create_artifact`;
  "report/analysis/writeup/document" → `create_doc`; ambiguous → dashboard + written
  summary in the final message (status-quo behavior, so ambiguity never regresses).
- Doc authoring guidance: embed live charts with `{{viz:<uuid>}}` (ids from create_data
  results), diagrams with ` ```mermaid `, columns with `:::columns` / `:::` and
  `::: col` dividers; cite tables/columns for claims; don't rebuild data that exists —
  reference it.
- One line added to `create_artifact`'s description: "for a written report/document,
  use create_doc instead." `create_artifact` itself does not change.

### B4. Mode-filter audit (the landmine list)

Every "latest artifact for report" consumer currently assumes dashboards. Each needs a
`mode` filter or doc-aware branch — this ships WITH the feature, not after:

| Call site | Fix |
|---|---|
| `agent_v2.py:481` `_get_active_artifact` | exclude `mode='doc'` (planner dashboard-continuity routing must never bind to a doc) |
| `report_pdf_service.py` `generate_for_report` (latest artifact → PDF) | keep selecting dashboards; doc PDF is a separate path (below) |
| `thumbnail_service.py` | dashboards only (or doc-specific thumbnail later) |
| `query_service.py:124`, `report_service.py:1172/1286/1310` | verify behavior — these read `content.visualization_ids`, which docs share; confirm intended results for docs |
| `ai/tools/mcp/app_tools.py:140`, `mcp/edit_artifact.py` | exclude docs |
| `routes/artifact.py` `/report/{id}/latest` | add optional `?mode=` param; default excludes docs |
| frontend `artifact_modes` (home cards) | include `doc` with a doc icon |

Acceptance test: create a dashboard, then a doc; the report's PDF export, thumbnail,
active-artifact context, and rerun targeting must all still resolve to the dashboard.

### B5. API for user edits

Reuse artifact routes. `POST /artifacts` (already `update_reports`-guarded) accepts the
new version from the TipTap save; add owner check for doc edits. `GET
/artifacts/report/{report_id}` already lists all modes — ensure `mode` is in
`ArtifactListSchema` for the frontend to badge docs. Public sharing works unchanged
(`allow_public=True` paths already exist at artifact granularity).

### B6. Viz hydration

No new endpoint: `DocViewer` fetches viz/step data the same way the dashboard path does
(report queries filtered by the artifact's `visualization_ids` —
`report_service.py:1172` already implements exactly this given an `artifact_id`).

## Frontend

### F1. `DocViewer.vue` (components/dashboard/)

Read-only renderer. Pipeline:

1. Split `content.markdown` on `{{viz:<uuid>}}` **outside fenced code** → alternating
   segments `[md, viz, md, ...]`.
2. Markdown segments: `markdown-it` with `html: false` (no raw HTML — reliability and
   XSS posture in one decision), GFM tables, task-list plugin,
   `markdown-it-container` for `:::columns` (rendered as CSS grid, stacking on mobile).
   Output passed through DOMPurify as belt-and-suspenders.
3. ` ```mermaid ` fences: async mermaid.js render; on parse failure fall back to the
   plain code block (never a broken page).
4. Viz segments: mount the existing `RenderVisual` / `RenderTable` / `RenderCount`
   components (chosen by view type — same dispatch `ToolWidgetPreview.vue` uses), each
   wrapped in an error boundary with a skeleton loading state and a quiet failure card.
5. Typography: single readable column (~70ch), Tailwind prose styling, generous
   whitespace, print stylesheet. Minimal chrome — the doc IS the page.

Reliability rules: the renderer never throws (every dynamic element is isolated);
unknown placeholders render as a subtle "visualization unavailable" card; the raw
markdown is always recoverable via the version history.

### F2. `ArtifactFrame.vue` — third mode branch

- `mode === 'doc'` → `DocViewer` (page → iframe and slides → SlideViewer unchanged).
- Toolbar per mode: docs keep Refresh (reruns viz queries) and Share; hide
  PPTX/polish/dashboard-only actions; add Export (.md download v1, PDF next) and an
  **Edit** button (owner only).
- Selector options get a small mode icon (doc/dashboard/slides) so mixed lists scan
  cleanly.

### F3. Summary tab (`ChatSummary.vue`)

`artifactList` already renders artifacts and emits `openArtifact` — include docs with
the doc icon; clicking opens the right panel with that doc selected (existing event
path). No structural change.

### F4. Report page (right panel)

`pages/reports/[id]/index.vue` hosts ArtifactFrame in the Dashboard view — docs appear
there through the selector with no tab changes in v1. (If docs earn a dedicated tab
later, that's additive.)

### F5. TipTap editing (owner)

`nuxt-tiptap-editor` is already a dependency. Editor config:
- StarterKit + Table + TaskList/TaskItem + `tiptap-markdown` for md serialization.
- Custom nodes: **VizEmbed** (atom node; node-view mounts `RenderVisual`; serializes to
  `{{viz:<uuid>}}`), **MermaidBlock** (code block with preview toggle; serializes to
  the fence), **Columns** (maps the `:::columns` container).
- Load markdown → edit → serialize markdown → `POST /artifacts` new version →
  optimistic swap in the viewer. Version history is the undo story.
- **Editing is locked while an agent run is active on the report** (run state already
  streams over SSE) — the v1 answer to write conflicts.
- CI guard: round-trip test (md → TipTap → md) must be idempotent on the supported
  markdown subset; anything outside the subset must pass through unmodified rather than
  be dropped.

### F6. Chat tool components

`CreateDocTool.vue` / `EditDocTool.vue` in `components/tools/` (pattern:
`CreateArtifactTool.vue`) — compact card with title, status, and an "Open doc" action
targeting the right panel.

## Testing

- Backend unit: placeholder extraction (incl. fenced-code skip), viz validation
  errors, version lineage on edit, and the full mode-filter audit (esp.
  `_get_active_artifact` returning the dashboard when a doc is newer).
- Frontend unit: segment parser, mermaid fallback, viz error boundary.
- E2E: agent creates doc with 2 vizs + mermaid → renders; owner edits in TipTap →
  new version; dashboard-based flows unaffected by the doc's existence.

## Rollout

1. **Phase 1 (read path)**: tools + validation + prompt routing + mode-filter audit +
   DocViewer + selector/Summary integration + `.md` export.
2. **Phase 2 (write path)**: TipTap owner editing + run-lock + round-trip CI guard +
   PDF export (via existing Playwright/Chromium `report_pdf_service` pointed at
   DocViewer).
3. **Phase 3 (reach)**: docx export (pandoc + per-element chart screenshots),
   "Open in Google Docs" only if/when a Google OAuth integration exists.

## Alternatives considered (and why not)

- **Styled-JSX "doc-looking" dashboards via create_artifact prompting**: content
  trapped in generated code — kills TipTap editing, md/docx export, and content
  fidelity (designer paraphrase hop). Fine as a demo, wrong as architecture.
- **`mode='doc'` on create_artifact instead of new tools**: disjoint arg shapes
  (prompt+viz_ids+codegen vs direct markdown) in one schema confuse tool-calling
  models and validation. Same table, sibling tools.
- **Block-based storage**: optimizes for a collaborative editor we aren't shipping;
  markdown→blocks is a cheap one-way migration later; the reverse investment is
  wasted if never needed.
- **New `documents` table**: duplicates versioning/sharing/permissions that artifact
  rows already provide (public sharing verified present at artifact granularity);
  the cost of reuse is the bounded mode-filter audit above.
