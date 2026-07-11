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

EditDocInput:    doc_id: str
                 edits: Optional[List[{find: str, replace: str}]]   # surgical, preferred
                 markdown: Optional[str]                            # full rewrite fallback
                 title: Optional[str]
EditDocOutput:   doc_id, title, version, visualization_ids, diff_applied: bool
```

`edit_doc` mirrors `edit_artifact`'s surgical-diff-with-fallback contract, minus the
coder LLM (the planner authors replacement text itself, so the tool applies string
edits directly):
- Each `find` must match the current markdown **exactly once**; ambiguous or missing
  matches fail that op with a field error naming it (planner retries with corrected
  context — the doc's current text is available via the create/edit observation).
- The `edits` list applies **atomically** — all ops succeed or none are applied, so a
  partial edit can never corrupt a doc.
- `markdown` (full rewrite) is the fallback for restructures, exactly like
  `edit_artifact`'s rewrite path; `diff_applied` in the output reports which path ran.
- Exactly one of `edits` / `markdown` must be provided.
- Edits are additive/continuous by default (same continuity language as
  edit_artifact): preserve title unless asked, `visualization_ids` re-extracted from
  the resulting markdown after every edit.

Validation (both tools):
- Extract `{{viz:<uuid>}}` placeholders **skipping fenced code blocks** (a doc quoting
  a placeholder inside ``` must not hydrate it).
- Every referenced viz must exist, belong to the report, and have a successful step
  (same rule `create_artifact` applies). Unknown ids → field error so the planner
  retries with corrected ids.
- Size cap (~50k chars) with a clear error message.

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
- Doc mechanics: embed live charts with `{{viz:<uuid>}}` (ids from create_data
  results), diagrams with ` ```mermaid `, columns with `:::columns` / `:::` and
  `::: col` dividers; don't rebuild data that exists — reference it.
- **Authoring guidelines (analytical writing standards)** — a dedicated prompt section,
  mirroring the tone of the existing ANALYTICAL STANDARDS block:
  - **Citations everywhere a claim is made**: every number, trend, or conclusion names
    its source — the table/column queried, the embedded viz it comes from, and the time
    range covered. Findings without a source don't go in the doc. Distinguish
    "data shows X" from "inferred X"; state confidence and data limitations.
  - **Structure follows the analytical genre.** The doc adapts its skeleton to the
    ask rather than one fixed template. Named patterns the prompt teaches:
    - *Root-cause analysis*: Symptom (with the viz showing it) → Hypotheses considered
      → Evidence per hypothesis (cited, incl. ruled-out paths) → Root cause →
      Recommended actions. Mermaid is encouraged for causal/flow diagrams.
    - *Deep-dive report*: Executive summary (3-5 bullets, numbers inline) → Findings
      (one section per finding: chart + prose + citation) → Methodology (tables used,
      definitions applied, caveats) → Next questions.
    - *Executive summary / memo*: the answer first, one supporting viz, caveats
      footnoted — brevity is the feature.
    - *Data audit / quality review*: scope → checks performed → issues found (each
      with evidence query) → severity and recommended fixes.
  - Charts carry the evidence; prose carries the interpretation — never restate a
    chart's rows in text beyond the headline numbers.
  - Same-language rule: the doc is written in the user's language (matching existing
    title guidance in create_artifact).
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
new version from the TipTap save. **Doc edits are owner-only**: enforce
`owner_only=True` on the doc-edit path (the permission decorator already supports it —
see the artifact GET routes); non-owners get read-only rendering with no Edit
affordance in the UI and a 403 on the API. `GET /artifacts/report/{report_id}` already
lists all modes — ensure `mode` is in `ArtifactListSchema` for the frontend to badge
docs. Public sharing works unchanged (`allow_public=True` paths already exist at
artifact granularity).

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

### F4b. `/dashboards` index page

`pages/dashboards/index.vue` lists reports with artifacts (`has_artifacts: 'yes'`,
My/Shared tabs). Docs join this surface:
- Report cards whose artifacts include docs get a doc badge (reuse the
  `artifact_modes` field the home cards already read; add `'doc'` to it).
- Add a type filter (All / Dashboards / Docs) next to the existing tabs.
- Doc cards open the report with the doc selected in the right panel (same
  `openArtifact` path as the Summary tab). Doc card preview: title + first heading
  outline v1; rendered thumbnail is a later nicety.
- Consider renaming copy from "Dashboards" to something inclusive later; v1 keeps the
  route and adds docs to it.

### F4c. `/r/[id]` public share page

`pages/r/[id]/index.vue` currently branches: slides-with-previews → `SlideViewer`,
artifacts → iframe (`buildArtifactIframeHtml`), legacy layout → legacy view. Add the
doc branch: shared artifact with `mode='doc'` → `DocViewer` (read-only, never
editable on the public page). Requirements:
- Viz hydration must work through the `allow_public=True` permission paths (the same
  public data access the iframe dashboards already rely on).
- Print/export affordances only (no Edit button, no version selector).
- If a report has both a dashboard and a doc shared, the public page shows the same
  artifact the sharer selected (respect existing share semantics; no new picker in v1).

### F5. TipTap editing (owner)

`nuxt-tiptap-editor` is already a dependency. Editor config:
- StarterKit + Table + TaskList/TaskItem + `tiptap-markdown` for md serialization.
- Custom nodes: **VizEmbed** (atom node; node-view mounts `RenderVisual`; serializes to
  `{{viz:<uuid>}}`), **MermaidBlock** (code block with preview toggle; serializes to
  the fence), **Columns** (maps the `:::columns` container).
- Load markdown → edit → serialize markdown → `POST /artifacts` new version →
  optimistic swap in the viewer. Version history is the undo story.
- **Owner-only**: the Edit button renders only for the report owner; the API enforces
  it independently (B5). Everyone else gets the read-only DocViewer.
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

1. **Phase 1 — agent write path + read everywhere** (the feature exists):
   `create_doc`/`edit_doc` (find/replace + fallback) with validation; planner prompt
   (routing rule + authoring guidelines/genres + citations); mode-filter audit;
   `DocViewer` (markdown + mermaid + live viz + columns/tables); ArtifactFrame doc
   branch + selector icons; Summary tab entries; chat tool cards; `.md` export.
   Acceptance: a deep run produces a cited RCA doc with live charts, visible in the
   right panel, Summary, and selector — and no dashboard flow regresses.
2. **Phase 2 — sharing surfaces**: `/r/[id]` public doc rendering (read-only, public
   viz hydration verified); `/dashboards` doc badges + type filter + open-to-doc;
   PDF export (existing Playwright `report_pdf_service` pointed at DocViewer).
3. **Phase 3 — owner editing**: TipTap (StarterKit + Table + custom
   VizEmbed/MermaidBlock/Columns nodes), owner-only gating (UI + API), run-lock,
   md round-trip CI guard, save-as-new-version.
4. **Phase 4 — reach**: docx export (pandoc + per-element chart screenshots), doc
   thumbnails for cards, "Open in Google Docs" if/when a Google OAuth integration
   exists.

Phases 2 and 3 are independent of each other (both depend only on Phase 1) and can be
reordered or parallelized; editing is sequenced after sharing here because the agent +
share loop delivers user value sooner, but flip them if owner editing is the more
requested capability.

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
