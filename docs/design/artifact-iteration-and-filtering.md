# Robust artifacts — one authoring architecture, hard boundary contracts

Status: phases 1-2 implemented and verified e2e; PR #1030 review findings
addressed (hard-blocking gate on edits, tightened coverage, string-safe
codemod, v3 alignment, rewrite-capable coder prompt); F3 shipped
(`add_parameter` — in-place retro-parameterization); phase-3 step 1 shipped
(`apply_artifact_edit` — planner-authored mechanical edits, no inner LLM).
Remaining: F2 (report-level parameters), phase-3 steps 2-3 (slides
script-as-source; planner-authored creates, then delete the coder LLM).
Verification: docs/feedback-loops/artifact-iteration-filtering.md
Trigger: customer complaints — iterating an artifact regresses other areas of
the page, and dashboards lack Tableau-style filters/parameterized queries.
(Speed was examined and explicitly deprioritized: slow-but-correct is
acceptable; wrong output is not.)

Product constraint settled during review: **no structured page model** — no
section manifest, no v0-style multi-file split, no fixed grid. Artifacts stay
free-form (the LLM owns layout, styling, composition end to end). Robustness
must come from the *boundaries* (data binding, filter semantics) and from
*verifiable edits*, never from constraining the design surface.

## Where the failures actually come from (evidence)

- **Full rebuilds masquerading as edits.** The planner routes any change
  "too large for surgical diffs (~>30% of code)" to `create_artifact`
  (`app/ai/agents/planner/prompt_builder.py:186`), which regenerates the whole
  page **without being shown the existing code** — every untouched panel comes
  back different.
- **Positional data binding.** Artifact code addresses data as `viz[0]`,
  `viz[1]`… ordered by `content.visualization_ids`
  (`ArtifactFrame.vue:1955-1965`). Any viz add/remove re-indexes the array and
  the model must hand-renumber every reference
  (`edit_artifact.py:415-421`, auto-merge shifts at `:840-860`). One miss
  silently renders the wrong dataset — the worst regression class because it
  looks plausible.
- **The editor has no memory.** `edit_artifact` runs a *second* LLM (a coder)
  whose prompt deliberately omits conversation history and the accumulated
  spec (`edit_artifact.py:369-378`, cut because prompts grew 38K→63K tokens).
  The planner has the history; the mind writing the diff does not. Decisions
  from earlier turns ("keep KPIs on top") get silently undone.
- **Filters are implemented, not declared.** A complete server-side parameter
  system exists end-to-end (`param_schema.py` → `query_params.py` →
  `query_service.run_query_viewer` → `useParams()` → `StepUserResult` cache),
  but it's opt-in per query at `create_data` time and the planner is told not
  to use it by default (`schemas/create_data.py:95`). Users mostly get
  client-side `filterRows` over a pre-fetched snapshot
  (`artifact-globals.js:158-235`), which silently no-ops for any viz that
  doesn't project the filter column. A filter that only filters 2 of 5 charts
  renders fine today.
- **Validation is a screenshot.** The main post-edit gate is a headless
  render; a page that renders but shows wrong data passes.

## North star architecture: the planner is the author; tools are dumb

Claude Artifacts' robustness comes from one property: **one mind**. The
conversation model authors the artifact content itself — `update` tool calls
carry literal `old_str`/`new_str`; the tool is a mechanical string-replace.
History, prior decisions, and the artifact are all in the same context by
construction. BOW's planner→coder handoff was a token optimization whose costs
(amnesia, blind inner repair loop, a planner that can't see its own
deliverable) are now the customer complaints, while the thing it saves —
context tokens — got cheap (static reference material is cacheable; the
dynamic part is bounded by artifact size).

**Target state — no hybrid, one architecture across all modes:**

- The **planner authors all artifact source text** (create: full source;
  edit: `[{find, replace}]` string edits) with the runtime/codegen reference
  moved into its cacheable static system prompt and compact viz profiles
  (schema + few sample rows, keyed by uuid) in context while an artifact is
  active.
- The **artifact tools become mechanical**: apply edits atomically → run
  deterministic gates → render/execute → return errors, closest-match hints,
  and previews as the tool observation. The planner's own agentic loop is the
  repair loop (replacing the blind in-tool repair loops). `read_artifact`
  shrinks to session-resume hydration.
- The coder LLM inside `create_artifact`/`edit_artifact` is **deleted**, not
  wrapped. `edit_doc` already works exactly this way (surgical string edits,
  no coder) and is the in-product proof.

### Per-mode: same loop, three validators

| Mode | Planner-authored source | Validation | Output |
|---|---|---|---|
| doc | markdown (`{{viz:<uuid>}}`) | placeholder/viz checks (exists today) | rendered doc; md/PDF/docx |
| slides | **python-pptx script** — the deck *is* PPTX, not an HTML deck with a pptx export | script executes & saves; slide count; viz-uuid refs; per-slide preview images (`PptxPreviewService`) | the .pptx + previews |
| page | JSX | parse; viz-ref set; filter/param wiring; render | live dashboard iframe; HTML/PDF export |

Slides note: PPTX is a zipped binary — the editable source of truth must be
text, and that text is the python-pptx script (as Claude's own pptx flow
works). The HTML slide deck path stops being a second source of truth: it
becomes a preview surface or is retired for slides. `_fix_pptx_code`'s repair
folds into the planner loop; export-time mechanics stay a backend service.

## Hard boundary contracts (required regardless of the authoring switch;
## with dumb tools they are the *only* safety net, so they come first)

1. **Id-keyed data access.** `vizById("<uuid>")` replaces positional
   `viz[N]`; compat shim in `artifact-globals.js` keeps old artifacts
   rendering. Post-edit gate: the set of viz uuids referenced in code must
   equal `visualization_ids`; a violating hunk is rejected with a precise,
   fixable error. Kills the silent wrong-dataset class; costs zero design
   freedom.
2. **Filters as enforced runtime primitives.** The model may place and style
   filter controls anywhere, but a control *declares* the param/field it
   drives; actual row selection happens in the runtime (server-side param
   re-run, or host-applied `filterRows`), never in hand-written `.filter()`
   chains per chart. Gate (hard, not prompt guidance — extend
   `params_wiring_errors`, `create_artifact.py:390-428`): every declared
   filter/param is bound by every viz on the page or explicitly exempted.
   "Filter silently skips 3 charts" becomes a build error.
3. **Server-side params by default for dashboards.** For page artifacts with
   a filter-shaped contract, parameterize the shared dimensions (date range +
   1–3 dimensions) across the dashboard's queries via the existing
   filter-space pattern (`prompt_builder.py:145`); add a small
   retro-parameterization path (`add (:name IS NULL OR col = :name)` to an
   existing query + one re-run) so adding a filter never requires regenerating
   queries. Client-side filtering remains for cheap within-snapshot
   interactions only.
4. **Deterministic gates before pixels.** Parse → ref-set → wiring checks run
   before any browser render; the screenshot loop becomes the last gate, not
   the only one.

Explicitly rejected alternatives, for the record: section/manifest page model
and per-viz declarative objects (limit design freedom — rejected by product);
v0-style multi-file projects (complexity a dashboard doesn't need); hybrid
smart+dumb edit tools (two architectures to maintain; rejected in favor of the
full switch).

## Backwards compatibility

Two existing properties make this cheap: artifact versions are immutable rows
(edits insert, never rewrite), and every artifact carries its full source as
data. Governing principles: stored rows are never rewritten; mechanical
upgrades happen lazily at edit time; semantic upgrades happen only on user
request; the shared runtime only ever grows.

- **Runtime is additive — and is the main landmine.** `artifact-globals.js`
  is shared by every artifact ever created (not pinned per artifact), so all
  changes are retroactive. `vizById()` is added; `viz[N]` ordering and
  `useFilters`/`filterRows` are kept forever. Alongside this work, version the
  injected lib bundle and stamp `content.runtime_version` on new rows so
  future runtime changes cannot silently break old dashboards.
- **`viz[N]` → `vizById` is a deterministic codemod, no LLM**: `viz[3]` ≡
  `visualization_ids[3]`. Run it as a pre-step the first time a legacy
  artifact is *edited* (new version row; gates apply from then on). Never
  migrate on view. Reverting to a pre-migration version is fine — the codemod
  reruns on the next edit.
- **Gates are generation-aware.** Full enforcement on new-generation artifacts
  (post-codemod or newly created); viewing legacy artifacts is ungated. The
  filter-wiring gate never retro-fails old hand-rolled filter code — filter
  semantics upgrade only when the user asks for filter changes on that
  artifact (auto-rewriting logic can change behavior; renaming an index
  cannot).
- **The dumb-tool switch doesn't touch stored artifacts.** It changes who
  authors edits, not the stored format; legacy JSX is equally editable via
  planner-authored find/replace.
- **Slides is the one format break — don't migrate it.** Old decks stay
  HTML-source (renderer + existing pptx-export transcode kept alive; still
  editable as text); new decks are pptx scripts; `content.source_format`
  distinguishes them. Moving an old deck to the new pipeline is an explicit
  user-chosen rebuild, never automatic.
- **No DB migration.** All markers ride in the existing `content` JSON;
  absent fields = legacy.

## Sequencing

1. **Prompt-level fixes (cheap, current system):** drop the ~30% → rebuild
   rule (edit-first, many hunks per edit; rebuilds get prior code +
   "preserve everything not named"); interim pinned-decisions digest in the
   edit prompt (~1–2K tokens, distilled from the accumulated spec already
   persisted at `edit_artifact.py:1400`) until the one-mind switch makes it
   mostly redundant.
2. **Boundary contracts:** id-keyed viz + ref-set gate; filter wiring gate;
   params-by-default + retro-parameterization. These de-risk the current
   system *and* are prerequisites for trusting dumb tools.
3. **The switch, mode by mode:** doc (done — already dumb) → slides
   (script-as-source, execution is validation) → page (behind a flag:
   planner-authored diffs through the mechanical tool; then planner-authored
   creates; then delete the coder).

## Success measures

- Regressions: per-edit diff blast radius (hunks touching un-mentioned
  regions), viz-ref gate rejection rate trending to zero, "wrong data"
  reports.
- Filters: % of dashboard vizzes bound to each page filter (target: 100% or
  explicit exemption), share of dashboards using server-side params.
- Iteration: user turns per accepted change (the real "time consuming"
  metric — fewer re-asks, not faster calls).
