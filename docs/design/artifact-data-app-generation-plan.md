# PR #1129: generate polished, connected data apps

Status: implemented locally in `/private/tmp/bow-pr1129` on `codex/review-pr1129`.
See [the runnable feedback loop](../feedback-loops/artifact-data-app-generation.md)
for changes, live examples, original/generated-versus-reviewed evidence and checks.

Authoring, custom composition, typed parameter context, runtime compatibility and
the bounded refinement mechanism are implemented. Three apps and a legacy fixture
run on localhost:3000. Validation used one generation model; the original two-model
comparison and external screenshot-to-model refinement round trip remain unverified.
The latter was rejected by automatic approval review, so observed corrections were
made locally. The checklist below retains the original acceptance targets.

PR: https://github.com/bagofwords1/bagofwords/pull/1129
Reviewed baseline: `3e32c09db2e88815bd2b9910db84ddd71d145d7f`.

## Outcome

Generate a purpose-built data app whose visual quality, interaction design, and composition fit the user's task. It should feel intentionally designed, use real Bag of Words data, and support working query parameters, filters, and detail exploration when the request calls for them. A dashboard is one possible view of such an app.

For example, a music catalog request can open on a searchable catalog with a selected album's details. A sales exploration request can center on a chart, a period selector, and regional drill-down. Neither needs a giant revenue statement or a row of invented KPIs above the working interface.

The quality target is the polish the user sees in Sites and Claude Artifacts. That is an acceptance target, not a claim about their internal architecture. More themes alone will not achieve it.

## Hard requirement: existing dashboards remain compatible

Existing saved dashboards must continue to open, render, filter, execute their supported parameterized queries, share, export, and accept ordinary edits without regeneration or migration by the user. Preserve their appearance, numeric meaning, data bindings, and supported interactions. Improved generation applies to new apps; redesigning an existing dashboard requires an explicit redesign request.

- Select compatibility behavior from persisted runtime metadata before loading version-dependent code/styles. Missing metadata means the historical legacy contract, never an automatic upgrade to the newest runtime. Preserve explicitly versioned artifacts on their declared contract.
- Preserve existing runtime globals, component props/defaults, formatting semantics, parameter messages, and payload fields used by saved code. New metadata is additive and optional for old artifacts. Where contracts differ, use version-scoped behavior or compatibility adapters rather than changing old semantics globally.
- Ordinary edits retain runtime identity and styling. New authoring requirements must not reject otherwise valid legacy code solely because it lacks themes, new metadata, or new composition conventions. Any existing positional-to-ID repair must preserve the exact dataset mapping and historical versions.
- Opening, rerunning, sharing, thumbnail generation, and exporting must not rewrite stored source or runtime metadata. Existing non-page dashboard rendering paths remain supported and receive smoke coverage where shared code changes touch them.
- Establish representative legacy fixtures and baseline screenshots/interaction results before changing shared runtime code. Include unversioned artifacts, positional references, old kit components, custom CSS, percentage formatting, filters, and parameterized queries; also cover already versioned themed artifacts.

**Release gate:** these fixtures must retain their established appearance and behavior across the live viewer, shared viewer, preview, thumbnail, and supported export formats. Exercise ordinary edits and query reruns as well as initial rendering. A compatibility regression blocks shipping #1129; users must not be asked to rebuild old dashboards.

## Keep the existing architecture

Keep planner-authored React/JSX, the sandboxed iframe, the existing data bridge, mechanical create/edit tools, and artifact versioning. Keep the technical `mode="page"` identifier. Build on `useArtifactData`, ID-based visualization bindings, `useParams`, `useParamOptions`, `useFilters`, and existing provenance support.

The intended flow is:

```text
User task + available data + supported interactions
    → compact app/design brief
    → planner authors JSX using the runtime contract
    → parse/data-contract checks + render
    → existing screenshot reviewed by the same planner, when available
    → at most one focused visual refinement
    → saved data app connected through the existing host bridge
```

Do not introduce a second generation agent, a new frontend framework, a rigid page-layout schema, or a separate app-building service.

## 1. Replace the dashboard recipe with data-app authoring guidance

**Files:** `backend/app/ai/tools/implementations/create_artifact.py`, `backend/app/ai/agents/planner/artifact_authoring.py`, `prompt_builder.py`, `prompt_builder_v3.py`, and `backend/app/ai/tools/schemas/create_artifact.py`.

- Rewrite `_build_page_system_prompt()` around a compact brief: user task, primary working surface, needed views, data bindings, interactions, visual direction, and important states. Keep this in the existing build-spec field; do not add a required planning tool call.
- Remove mandatory hero selection, finding-as-page-title, KPI counts, subject-to-theme assignments, and the instruction that every app must look different by subject. Use a stable app title; put findings and summary metrics where they help the task.
- Permit table-first, chart-first, search/detail, comparison, and monitoring compositions. Tabs, sidebars, cards, and multi-view navigation are choices justified by the task, not requirements for every app.
- Replace the dominant full-page KPI example with short, varied interaction examples. Demonstrate a parameterized chart, a searchable collection with detail selection, and a compact monitoring view. These illustrate contracts rather than prescribe entire layouts.
- Teach deliberate visual decisions: a coherent type scale, readable numerals, useful density, aligned controls, restrained surfaces, consistent chart colors, and responsive composition. Avoid forcing a display font, eyebrow, rounded card, or hero onto every app.
- Require meaningful loading, empty, error, selection, and focus states. Every visible action must have an implemented, supported behavior; omit pretend buttons.
- Keep slide authoring and mechanical edit contracts intact. Update conflicting page-generation instructions in every entry point so the planner is not simultaneously told to follow the old recipe.

**Default skill:** revise `backend/app/ai/skills/library/dashboard-theme.md`. For this unmerged PR, replace its newly introduced default with a `data-app-design` catalog entry and task-based guidance; retain dashboard composition as a conditional subsection. Check `complex-dashboard.md` for conflicting universal instructions and keep it scoped to actual dashboard requests.

Catalog defaults are copied into organization instructions; changing the Markdown does not rewrite existing installations. New installs should receive the new guidance. Do not overwrite customized organization instructions or re-enable disabled skills. If a prerelease environment already installed `dashboard-theme`, handle that environment explicitly using the existing catalog lifecycle rather than introducing an indiscriminate migration.

**Acceptance:** the catalog and exploration briefs can produce useful primary workspaces without a KPI row or numeric headline; a monitoring brief can still produce an excellent dashboard.

## 2. Make the design kit optional and custom composition first-class

**Files:** `backend/app/ai/tools/implementations/_sandbox_context.py`, `_artifact_refs.py`, `frontend/public/libs/artifact-globals.js`, and `artifact-tailwind.js` where needed.

- Separate required runtime documentation from optional presentation guidance. One shared authoring reference should serve planner and tool paths without copied, conflicting rules.
- Required contracts cover available globals, data shape, parameter semantics, provenance, sandbox constraints, and output syntax. Themes, `PageHeader`, `KPICard`, `SectionCard`, and other kit components are optional conveniences.
- Allow plain React markup, custom components, Tailwind, and CSS supported by the current iframe. Document a small example using custom markup with `data-bow-viz`; do not require a new build pipeline or arbitrary imports.
- Remove rejection based on missing `setTheme()` or a count of raw color classes. Give new apps usable default styling when no theme is explicitly selected. Maintain parse validation and data-reference checks.
- Scope kit typography to the kit so global `#root` heading rules do not override an app's authored hierarchy. Preserve explicit component overrides.
- Keep themes useful as coherent starting points. Custom colors must remain readable; theme-aware styling matters when an app actually supports theme switching. An aesthetic heuristic is not proof of correctness.
- Bind included datasets by stable IDs. Data used in a detail panel or another tab does not need to appear in the initial viewport. Do not weaken protection against unknown IDs or silent positional mismatches.

**Acceptance:** a fully custom, source-backed app renders through create/edit without using kit cards or calling `setTheme`. Existing themed apps still work. The default result remains styled and readable.

## 3. Give the author accurate parameter and capability context

**Files:** `backend/app/ai/tools/implementations/create_artifact.py`, `_sandbox_context.py`, `backend/app/services/artifact_payload.py`, `frontend/utils/artifactIframe.ts`, and `frontend/components/dashboard/ArtifactFrame.vue` as required for payload consistency.

- Normalize the existing parameter declarations into a compact authoring manifest: exact name, type, default/current value, allowed/static or dynamic options, relevant query IDs, and affected visualization IDs. Reuse existing declaration semantics; do not create a competing parameter system.
- Populate the corresponding runtime parameter context in create previews and shared server-side payload assembly. The create preview currently builds a separate payload and must not silently omit the context the generated hooks expect.
- Document the three state categories explicitly:
  - Query parameters use `useParams`/`useParamOptions` and re-execute the participating backend queries.
  - Snapshot filters operate only on loaded rows and supported fields. They cannot claim to filter data that was not fetched.
  - UI state owns tabs, selections, expanded details, display preferences, and local sorting.
- Let generated custom controls call the existing hooks. Expose their current values, pending/loading state, and errors without prescribing a particular filter-bar component.
- Render controls for user-adjustable parameters. Keep identity-controlled parameters owned by the host/backend; never generate an editable substitute for an authorization constraint.
- Express whether data is complete, limited, or aggregated when known. Derive only supported metrics; do not compute global totals from a partial visible result or invent a comparison to fill a card.
- Distinguish live-host query execution from static preview/export behavior. Static surfaces may display known values/options and filter their snapshot; unavailable server actions must not pretend to rerun queries. Preview construction must not fabricate option rows or execute new queries just to fill a screenshot.
- Keep support limited to capabilities the app already exposes. Search, selection, local comparison, and query-driven exploration are valid; backend write actions require a separate capability design.

**Acceptance:** one custom period/region control drives the intended queries and views, presents pending/errors, and never implies it updated an unrelated dataset. The same declaration has the same meaning in authoring context and the live host.

## 4. Preserve app state and fix runtime parity

**Files:** `frontend/public/libs/artifact-globals.js`, `frontend/utils/artifactIframe.ts`, `frontend/components/dashboard/ArtifactFrame.vue`, `frontend/pages/r/[id]/index.vue`, `backend/app/services/{artifact_payload,thumbnail_service,html_export_service}.py`, and page create/edit entry points, including `backend/app/ai/tools/mcp/{create_artifact,edit_artifact}.py` where required.

- Resolve runtime compatibility deterministically before version-dependent formatting/components run. The reviewed implementation freezes its legacy flag before late-injected preview/thumbnail data arrives. Supply version metadata before initialization or use an explicit initialization contract; avoid partially switching an already rendered app.
- Preserve runtime metadata on edits and carry it to every renderer. New page artifacts authored against the v11 reference need the matching runtime stamp, including MCP creation. This does not require migrating the entire MCP authoring architecture.
- Preserve legacy formatting, chart defaults, and typography for saved legacy artifacts. An unrelated edit must not implicitly redesign an existing artifact.
- Retain the existing iframe/data-message update pattern instead of remounting the app for query responses. Test rapid parameter changes so stale responses do not replace the latest state.
- Teach generated apps to preserve valid tabs/selections across updates using stable record IDs. When a selected record leaves the result, deliberately clear or explain the selection; do not show stale details as if they belong to the current filter.

**Acceptance:** the same artifact/data/version produces consistent formatting and composition in the live viewer, shared viewer, preview, thumbnail, and export. Static outputs are not expected to gain live query execution. Legacy fixtures retain their baseline behavior.

## 5. Use the existing screenshot for a bounded visual review

**Files:** `backend/app/ai/agents/planner/artifact_authoring.py`, relevant planner prompt builders, and create/edit observations. If enforcement needs orchestration state, keep it in the existing agent execution/state layer.

- Replace the instruction to finish immediately after successful creation with: inspect the returned screenshot when available, compare it to the task/design brief, and make one focused edit if a material visual problem is visible.
- Review whether the primary task gets enough space, controls align, type and density are coherent, charts are legible, and the actual viewer width works. This must evaluate visual quality, not merely absence of overflow.
- Limit automatic aesthetic refinement to one per requested generation/revision. Track the budget in execution state rather than relying solely on wording. Preserve existing functional error handling and loop guards.
- Continue respecting `allow_llm_see_data` and model vision support. A missing screenshot must not block generation or trigger a new permission flow.
- Correct the current guidance that a control “works” if its code maps options and sets state. Static screenshots and code inspection cannot certify an interaction; development acceptance must exercise it in a browser.

**Acceptance:** there is a demonstrated generation where one screenshot-guided edit improves a visible issue, and tests show it cannot spiral into repeated aesthetic rewrites. Vision-disabled generation completes normally.

## 6. Demonstrate quality with generated apps and real interactions

Use fixed data and the same briefs before and after. Keep original generated output and any automatic refinement separate in the evidence. Do not hand-polish only the showcased final output.

| Brief | Expected product shape | Required evidence |
| --- | --- | --- |
| Music catalog exploration | Searchable collection with useful detail selection; task-led layout | Search → select → details → back; stable selection behavior; empty results |
| Sales analysis | Query-driven exploration with period/region controls | Backend re-execution affects the intended views; loading/error; rapid changes; source-correct totals |
| Representative performance monitoring | Compact overview with meaningful metrics and drill-down | Inspect a representative, change scope, return; supported comparisons and consistent units |

- Capture before/after screenshots at approximately 400px, 960px, and 1440px, including at least one non-default interaction state. The embedded app width is a primary target.
- Repeat each brief with the two model configurations used for the supplied comparison. Record generation success, supported-control behavior, refinement count, latency, and token cost. Judge visual quality separately; more components or colors is not a quality score.
- Visually review working-surface priority, typography, spacing, density, chart composition, and interaction states. A technically passing app can still fail this review.
- Add targeted coverage to existing artifact design-system, reference, parsing, parameter-wiring, feedback-loop, and loop-guard tests. Remove tests whose sole purpose was enforcing aesthetic mandates. Do not replace them with prompt-string snapshots that claim to measure beauty.
- Add browser contract fixtures for custom styling, version initialization timing, parameter messages, out-of-order updates, and state preservation. Include a live backend query flow; synthetic postMessages alone cannot prove parameterized execution.
- Treat current `params_wiring_errors` string matching as a limited diagnostic. Adjust false rejections of supported custom controls, including batch setters, but do not claim static text checks prove every control works. A general JavaScript analyzer is outside this PR.
- Cover provenance for custom markup, null/empty data, readable errors, keyboard access, and no page-level horizontal overflow. Follow the repo's UI evidence and sandbox feedback-loop procedures; read backend test guidance before test changes and localization guidance before adding product strings.

## Delivery order

0. **Compatibility baseline:** capture saved-dashboard fixtures and their current rendering/interaction behavior before shared runtime changes. Carry this release gate through every subsequent step.
1. **Authoring and design freedom:** revise shared instructions and default skill; remove aesthetic gates; scope kit styles. Generate the three briefs early to check that the approach actually changes the output.
2. **Connected interactions:** align parameter context and runtime payloads; document supported capabilities; verify custom controls and app state through the existing bridge.
3. **Parity and refinement:** fix version/bootstrap regressions, preserve legacy behavior, and integrate the bounded screenshot review.
4. **Acceptance evidence:** run focused regression tests and live workflows; capture all before/after evidence; update #1129's design documentation and PR description to describe the final data-app scope using the repo's PR authoring standard.

The compatibility baseline and all four implementation steps are part of the proposed revision to #1129. Run evidence as each part lands; do not postpone visual judgment until the end. Implementation and validation evidence are recorded in the feedback loop linked above.

## Outside this PR

- A general backend action/writeback SDK, new authorization model, or arbitrary endpoint access.
- A new router, multi-file build system, external dependency installer, or full app IDE.
- Additional theme packs as a substitute for better generation.
- A separate designer/judge agent or unbounded visual optimization loop.
- Sweeping renames of dashboard directories, persistence models, or `mode="page"`.
- Unrequested rewrites of saved artifacts or organization-authored design instructions.

## References

- Existing PR review and reproduction evidence: `docs/feedback-loops/pr1129-review.md` and `docs/feedback-loops/pr1129-review/`.
- User-provided visual comparison: `/Users/yochze/Downloads/artifactbeforeafter.html`.
- Existing planner/mechanical-tool architecture: `backend/app/ai/AGENTS.md` and `docs/design/artifact-iteration-and-filtering.md`.
- Skill installation semantics: `backend/app/services/skill_catalog_service.py` (`ensure_defaults_for_org` installs unseen keys; it does not rewrite installed instructions).
