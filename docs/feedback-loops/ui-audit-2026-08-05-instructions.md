# UI audit — Instructions feature (2026-08-05)

Control-level + code-level audit of the **instructions** feature across every viewing/editing
surface, plus the backend authority, versioning, and prompt-loading paths.

**Surfaces covered**
- `/agents` → `KnowledgeExplorer.vue` (the "global instructions" main view: tree, primary-instruction panel, detail/edit pane, review feed) — the primary home.
- `/settings/instructions` → `ConsoleInstructions.vue` (a **second, still-live** full management view).
- `/reports/[id]` → report side-pane (`ReportAgentPanel.vue`) + in-chat tool cards (`CreateInstructionTool`, `EditInstructionTool`, `ReadInstructionTool`, `SearchInstructionsTool`).
- Modals: `AllInstructionsModal`, `InstructionModalComponent`, `InstructionDetailsModal`, `InstructionsListModalComponent`, `BuildExplorerModal`, `Instruction{Global,Private}CreateComponent`, label/learning modals.
- Backend: `instruction_service`, `instruction_version_service`, `instruction_sync_service`, `instruction_reference_service`, `permission_resolver`, routes, and the prompt path (`agent_v2`, `prompt_builder_v3`, `instruction_context_builder`, `instructions_section`).

**Method** — expected behaviour was derived from handler/route code (not the rendered UI), then compared against actual code paths. Findings are code-verified; a handful marked UNCLEAR need a live sandbox click to settle.

**Two headline reassurances (the user's top two worries):**
1. **No user can edit another user's instruction.** Update authority is backstopped by `_determine_update_type` → owner or org-level admin only; everything else 403s. Verified against `test_rbac_instructions.py` / `test_global_instruction_authority.py`.
2. **No cross-agent / cross-user instruction leaks into the prompt on the main build path.** Data-source scoping + main-build membership hold; covered by `test_instruction_roster_scope.py`. (Two narrower leak/scoping gaps remain — see INS-009, INS-026, INS-027.)

**Severity key** — **P1** data loss / crash / security (user acts beyond permission); **P2** control broken or silent data/version loss; **P3** works-but-wrong (stale, wrong copy, swallowed error, inconsistent across components, permission drift); **P4** cosmetic / dead code / copy.

Each issue has a stable ID (`INS-NN`) for 1:1 mapping to Linear. Issues needing more detail have an **[Annex]** block below.

---

## P1 — security / data loss

- **INS-001** [P1] `InstructionText` renders instruction bodies with `markdown-it({ html:true })` + `v-html`, **unsanitized**, on the `/agents` primary-instruction view — stored-HTML/XSS (`<img onerror=…>`) authored by anyone with `manage_instructions` or by the AI. *(InstructionText.vue:40,123; rendered from KnowledgeExplorer.vue:425)* **[Annex]**

## P2 — broken controls / silent data & version loss

- **INS-002** [P2] Edit draft is seeded from the **truncated** light-list `preview`; if the full `GET /instructions/{id}` fails or the user hits Edit while it's in flight, `save()` PUTs the truncated text and overwrites the real body — silent data loss. *(KnowledgeExplorer.vue:2884,2888-2895,2955-3001)* **[Annex]**
- **INS-003** [P2] REST `PUT /instructions/{id}` commits the live row **before** versioning, and version creation is best-effort (swallowed on failure) — an edit can silently mutate live content with **no version saved**. *(instruction_service.py:1093 vs 1097-1154)* **[Annex]**
- **INS-004** [P2] Git-deleted-resource archival sets `status='archived'` + rewrites `formatted_content` directly, with **no version and no build** — the transition is invisible to version history and diverges from the main-build snapshot. *(instruction_sync_service.py:485-496; caller metadata_indexing_job_service.py:1079)*
- **INS-005** [P2] Git sync create/update creates a version **only when a build object is present**; build-creation errors are swallowed upstream, so a `None` build overwrites the row with no version. *(instruction_sync_service.py:347-375,393-424,1108-1187; metadata_indexing_job_service.py:1142-1143)*
- **INS-006** [P2] Version numbers are allocated by a **non-atomic** `MAX()+1` and the `(instruction_id, version_number)` index is **non-unique** — concurrent edits can mint duplicate version numbers. *(instruction_version_service.py:276-287; instruction_version.py:71-73; alembic e5f6a7b8c9d0:124)*
- **INS-007** [P2] Version-history UI marks the newest version (list index 0) as "current" **by position**, not by matching `instruction.current_version_id` — a staged, not-yet-promoted version is shown as live and the real live version is offered a "Restore". *(KnowledgeExplorer.vue:871-880)* **[Annex]**
- **INS-008** [P2] `viewVersion` swallows its fetch error (`catch {}`): clicking a version whose fetch fails does nothing — no diff, no toast. *(KnowledgeExplorer.vue:2489-2495)*
- **INS-009** [P2] Mid-run agent focus-change rebuilds the `<instructions>` block with a fresh builder that **omits `organization_settings`**, so `max_instructions_in_context` reverts to the 50 default — an org configured above 50 silently drops active instructions after any `set_report_agents` turn. *(agent_v2.py:4132-4137)* **[Annex]**
- **INS-010** [P2] Cross-agent / cross-org metadata leak: a per-agent manager can reference another agent's (or another org's) table/column/**SQL** metadata from a **global** instruction — reference validation does no org/access check for `metadata_resource`/`datasource_table`, and the data-source constraint is skipped when the instruction has no `data_source_ids`. *(instruction_reference_service.py:102-161,196-217; instruction_service.py:144)* **[Annex]**

## P3 — works but wrong / inconsistent across components / permission drift

### Suggestion-review & eval subsystem
- **INS-011** [P3] An entire **second suggestion-review implementation is unreachable**: the `diff.buildId` inline-hunk view, its Accept/Reject/Accept-all/Reject-all (`doResolve`→`/resolve`), the pending-change banner, and the eval strip only render when `reviewMode` is false — but `reviewMode` stays true whenever anything is pending, so `InstructionTrackedChanges` (`/hunks/*`) always wins. *(KnowledgeExplorer.vue:606,620-712,2353)* **[Annex]**
- **INS-012** [P3] Consequently the **"Run eval on this suggestion"** feature (suite `<select>` + Run + live progress) is effectively dead on `/agents`. *(KnowledgeExplorer.vue:636-668,2580-2594)*

### Swallowed errors / silent failure (recurring class)
- **INS-013** [P3] `runAnalysis` swallows errors (`catch {}`): the Analyze tab shows empty results on failure with no feedback. *(KnowledgeExplorer.vue:3052-3066)*
- **INS-014** [P3] `loadVersions` swallows errors (`catch {}`): a failed versions fetch renders as "No history" — indistinguishable from a real empty history. *(KnowledgeExplorer.vue:3069-3072, template 870)*
- **INS-015** [P3] `save()` reads errors via `error.value.message` (undefined for `useMyFetch`) → the user always sees a generic "Create/Save failed", while `saveMeta`/`doResolve`/delete correctly use `.data.detail`. *(KnowledgeExplorer.vue:2962,2989 vs 3025)*
- **INS-016** [P3] `InstructionModalComponent` git unlink/relink swallow API errors (console-only, no toast) so a failed unlink silently leaves the toggle in its old state; they also call `/api/instructions/…` while siblings use `/instructions/…`. *(InstructionModalComponent.vue:447-498)*
- **INS-017** [P3] `CreateInstructionTool` derives resolved state by heuristic and shows **"Rejected" on a transient fetch failure**, diverging from `EditInstructionTool` which uses the authoritative `/verdict` endpoint (treats fetch failure as `unknown`). *(CreateInstructionTool.vue:370-387 vs EditInstructionTool.vue:492-514)* **[Annex]**

### Status / "pending" / label inconsistencies
- **INS-018** [P3] "Pending" is defined differently per component: `AllInstructionsModal.isPending` counts a **rejected** build as pending, while `useInstructionHelpers.getEffectiveStatus` counts only `draft`/`pending_approval` — the same instruction can read Pending in the modal and Active in the tree. *(AllInstructionsModal.vue:266 vs useInstructionHelpers.ts:101-107)*
- **INS-019** [P3] `draft` is labelled **"Inactive"** in the tree/detail but **"Draft"** in `AllInstructionsModal` (opened from the same page header). *(useInstructionHelpers.ts:88-96 vs AllInstructionsModal.vue:273,283)*
- **INS-020** [P3] Two disjoint status vocabularies with no shared legend: instruction `status` = `draft|published|archived` vs build `status` = `pending_approval|rejected|main(Active)`; plus `approved|pending|suggested|active` seen in the FE. *(models/instruction.py:25; BuildExplorerModal.vue)*
- **INS-021** [P3] Create paths set live status inconsistently: REST create → `published`, agent create tool → row `draft`/version `published`, git sync → `draft`/frontmatter/auto-publish — the "same action" yields different live status. *(instruction_service.py:116; create_instruction.py:385-393; instruction_sync_service.py:210-215,1241-1254)*
- **INS-022** [P3] Version schemas omit `status` and `evidence`, so version history can't show whether a historical version was draft/published/archived, nor the AI evidence the model stores. *(instruction_version_schema.py:27-72 vs instruction_version.py:29-30,58)*
- **INS-023** [P3] `bulk_update` version-creation failures are swallowed after the row is committed — same unversioned-mutation risk as INS-003. *(instruction_service.py:2915-2950)*

### Behavioural parity across surfaces
- **INS-024** [P3] Accept/Reject UX diverges between the two in-chat cards: `create_instruction` renders explicit Accept/Reject buttons; `edit_instruction` renders inline per-hunk review with no buttons. *(CreateInstructionTool.vue:111-136 vs EditInstructionTool.vue:155-175)*
- **INS-025** [P3] "Click text to edit" opens a **different surface** per card: `CreateInstructionTool` opens an in-place modal; `EditInstructionTool`/`SearchInstructionsTool` open the right side-pane. *(CreateInstructionTool.vue:423-448 vs EditInstructionTool.vue:683-692, SearchInstructionsTool.vue:48)*
- **INS-026** [P3] Non-build fallback load paths gate only on `status=="published"` with no main-build membership check; since every instruction is created `published`, these can load un-promoted/pending-approval instructions when no main build exists. *(instruction_context_builder.py:171-188,330-346,414-430,1204-1218)*
- **INS-027** [P3] The mid-run rebuild (INS-009) also **omits `channel=`**, so channel-specific instructions (e.g. Slack-only) leak into a different channel after a focus change — diverges from the initial/cached build. *(agent_v2.py:4132-4137 vs context_hub.py:315-326)*
- **INS-028** [P3] Confirmation UX is inconsistent: instruction delete, file delete and version restore use native `window.confirm`, while folder ops were migrated to an in-app modal ("replaces the browser's native prompt/confirm"). *(KnowledgeExplorer.vue:984-986,2937-2954,2806-2816,3073-3077)*

### Permission gating drift
- **INS-029** [P3] Detail-pane gating is split: Edit/Delete/body use `canEditDetail` (per-DS `manage_instructions` on all attached agents) but the metadata chips (status/load/category/agents/labels) use `metaEditable`→org-level `canEditInstr` — a per-agent manager can edit the text but sees status/category/labels as read-only. *(KnowledgeExplorer.vue:2123-2127,3013-3015,743-783)* **[Annex]**
- **INS-030** [P3] Backend edit/delete authority is asymmetric: a per-DS agent-manager **can DELETE** a non-owned instruction on their managed agent but **cannot EDIT** it (403). *(instruction_service.py:3376-3389 vs routes/instruction.py:835-895)* **[Annex]**
- **INS-031** [P3] Frontend "visible-but-forbidden": the Edit button shows for a per-DS manager but `PUT` 403s for any instruction they don't own. *(usePermissions.ts:101-113; KnowledgeExplorer.vue:2123-2127,2988)*
- **INS-032** [P3] Frontend "hidden-but-permitted": `canEditInstruction` ignores ownership, so an owner lacking `manage` on the attached DS (or owning a global without org-level `manage`) gets no Edit button though the backend `owner_edit` path allows it. *(KnowledgeExplorer.vue:2123-2127 vs instruction_service.py:3383-3388)*
- **INS-033** [P3] Global-instruction creation is gated inconsistently: `POST /instructions` with empty `data_source_ids` lets a per-DS manager author an org-wide global, bypassing the `require_org_permission` gate that `POST /instructions/global` enforces (API-only; UI routes globals correctly). *(routes/instruction.py:67-104)* **[Annex]**

### Dead click targets / broken wiring
- **INS-034** [P3] `RecentInstructions` row click ignores the clicked instruction and always navigates to `/agents` (handler comment was never finished). *(console/RecentInstructions.vue:202-205,21)*
- **INS-035** [P3] `ConsoleInstructions.openManageLabelsModal` is never invoked, so `InstructionLabelsManagerModal` is unreachable; and even if opened, it listens `@labels-changed` while the manager emits `labelsUpdated`, so `handleLabelsChanged` can never fire. *(ConsoleInstructions.vue:152,416 vs InstructionLabelsManagerModal.vue:153)*
- **INS-036** [P3] `InstructionModalComponent`'s embedded labels manager is unreachable (`showManageLabelsModal` never set) and is fed `:instructions="[]"`, so its usage counts are always 0. *(InstructionModalComponent.vue:109-113,205)*
- **INS-037** [P3] In-chat create/edit tool cards emit `instruction-updated`, but `reports/[id]/index.vue` binds no listener — the emit is a no-op; sync relies solely on the global `instruction:resolved` window event. *(index.vue:394-401)*

### Redundant surfaces & copy
- **INS-038** [P3] **Three parallel instruction homes** exist: `/agents` (main), `/settings/instructions` (`ConsoleInstructions`, still live) and the report side-pane. `/instructions` redirects to `/agents` but `/settings/instructions` was never consolidated. *(pages/settings/instructions.vue; pages/instructions.vue:13)*
- **INS-039** [P3] `InstructionPrivateCreateComponent` has **0** `$t()` calls (23 hardcoded strings) while its sibling `InstructionGlobalCreateComponent` has **78** — two divergent create surfaces, one untranslated in all 10 shipped locales. *(InstructionPrivateCreateComponent.vue)*
- **INS-040** [P3] Hardcoded English (no i18n) across many instruction components where peers use `$t`, so copy can't be localized or context-adapted. Cluster — see annex for the full component list. *(BuildExplorerModal, Bulk*, InstructionsFilterBar, PrimaryInstructionMenu, InstructionTrackedChanges, InstructionSuggestions, RecentInstructions, InstructionDetailsModal, InstructionsListModalComponent, ReportAgentPanel, label/learning modals)* **[Annex]**

## P4 — cosmetic / dead code / minor

- **INS-041** [P4] Dead handler functions in `KnowledgeExplorer` (residue of the pre-`InstructionTrackedChanges` inline review): `acceptMergedHunk`, `rejectMergedHunk`, `acceptSource`, `rejectSource`, `approveSuggestion`, `discardSuggestion`, `locateSuggestion`, `scrollToBuild`, `sourceLabel`, `mergedReviewCount`, `mergedSegments`, local `resolveAll`. *(KnowledgeExplorer.vue:2354-2515)*
- **INS-042** [P4] Dead handlers in chat tool cards: `EditInstructionTool` `handleAccept/handleReject/isAccepting/isRejecting`; `CreateInstructionTool` `handlePublish/handleDelete/isPublishing/isDeleting/canCreateInstructions`; `InstructionSuggestions` `handleRemove/isRemoving/removingIndex`; `RecentInstructions` `getCategoryIcon/getCategoryIconClass`; `InstructionDetailsModal` `formatDate`; `ConsoleInstructions` `availableBuilds`. *(see annex-less file:line list in agent notes)*
- **INS-043** [P4] `TrainingInstructionsSummary.vue` is completely unreferenced (PascalCase and kebab) — dead component. *(components/TrainingInstructionsSummary.vue)*
- **INS-044** [P4] Dead `_create_pending_version` creates a whole new Instruction row (not a version) using the deprecated `global_status='suggested'` and has no callers. *(instruction_sync_service.py:429-468)*
- **INS-045** [P4] Deprecated dead columns `private_status` / `global_status` remain on the model ("not used") yet are still **written** by the git-sync paths — confusing for anyone auditing status. *(models/instruction.py:36-39; instruction_sync_service.py:221,368,453,1254)*
- **INS-046** [P4] Decorator owner-allowance computes `not_approved` from `global_status` (always NULL) so it is always True — the "unpublished only" limitation is not enforced at the decorator (backstopped downstream). *(permissions_decorator.py:208-215)*
- **INS-047** [P4] `force_global` is accepted by `create_instruction` but never used; global-ness derives solely from empty `data_source_ids`. *(instruction_service.py:84)*
- **INS-048** [P4] Version-history rows show only "vN + date"; `created_by_user_id` is returned by the API but never displayed — no author attribution. *(KnowledgeExplorer.vue:875-878)*
- **INS-049** [P4] `restore()` confirmation uses a hardcoded English `window.confirm` string while surrounding toasts are i18n'd. *(KnowledgeExplorer.vue:3075)*
- **INS-050** [P4] `PrimaryInstructionPicker` search icon uses `name="heroicons-magnifying-glass"` (missing the `i-` prefix) → likely renders blank; field also uses `left-2`/`ps-7` (LTR-only) instead of logical `start-*`. *(PrimaryInstructionPicker.vue:19-25)*
- **INS-051** [P4] Header "New instruction" (`openCreate()` with no scope) silently inherits the currently-open agent's scope — creates an agent-scoped instruction, not a global, with no indication. *(KnowledgeExplorer.vue:31,2916-2924)*
- **INS-052** [P4] Three parallel "make primary" entry points on the same surface (agent-view "Change" picker, inline "Edit", detail-pane "Primary" `KSelect`) — same endpoint, redundant and not obviously the same action. *(KnowledgeExplorer.vue:422-423,770; handlers 1515-1542,1691,1700)*
- **INS-053** [P4] `read_instruction` resolves the short-id prefix org-wide before the scope check and returns "exists but not available" for out-of-scope IDs — confirms existence of another agent's instruction (text not leaked). *(read_instruction.py:160-176,228-233)*
- **INS-054** [P4] Context-builder main path scopes only by `report.data_sources` with no per-user membership re-check (unlike the UI list path) — not reachable in a normal run; defense-in-depth only. *(instruction_context_builder.py:821-831)*

---

# Annex

### INS-001 — Unsanitized HTML render (XSS)
`InstructionText.vue:40` constructs `new MarkdownIt({ html: true, … })`; the template renders `md.render(...)` output through `<div v-html="block.html" />` (line 123). `html:true` passes raw tags through and `v-html` injects them unescaped. Instruction bodies are user- and AI-authored, and this component renders the agent's **primary** instruction on `/agents` (`KnowledgeExplorer.vue:425`, `<InstructionText :markdown="true" prose>`), so a body containing an event-handler-bearing tag (`<img src=x onerror=…>`) executes for every viewer of that agent. No sanitizer (DOMPurify) is applied. Fix: disable `html` in markdown-it, or sanitize rendered HTML before `v-html`. Confirm the same render path in the report side-pane before closing.

### INS-002 — Truncated-preview save / data loss
Tree/list rows come from the light projection (`view=light`) carrying `preview` (a prefix) but no `text`. `openInstruction` sets `detail = { ...ins, text: ins.text ?? ins.preview ?? '' }` then `syncDraft` → `draft.text = preview`, then `await`s `GET /api/instructions/{id}` and only re-syncs the draft `if (!editing.value)`. Two failure modes: (1) the GET throws (`catch {}`, no toast) → draft stays the preview; (2) the user clicks Edit during the await → `editing` is true so full text is deliberately not synced. In both, `save()` PUTs `draft.text` (the prefix), overwriting the full body. Fix: block Edit/Save until the full body has loaded, or never seed the editable draft from `preview`.

### INS-003 — PUT commits before best-effort versioning
`update_instruction` applies the edit and `await db.commit()` at line 1093, then creates a version inside a `try/except` that only logs on failure ("Don't fail the update if versioning fails", 1152-1154). Because the content commit precedes and is independent of versioning, any failure in `has_content_changed`/`create_version`/`add_to_build` leaves the live row mutated with **no version and no build entry**. Contrast the REST *create* path, which rolls back + soft-deletes + returns 503 on version/build failure (157-229). Fix: wrap the row mutation and version creation in one transaction, or create the version first.

### INS-007 — "Current" marked by position, not id
The version list renders newest-first and marks index 0 as current/live, offering "Restore" on the rest. But `GET /versions` returns staged, not-yet-promoted versions (higher `version_number`) too, and the live version is `instruction.current_version_id`. When a suggestion/edit is staged, index 0 is the staged version → shown as current, while the actually-live version gets a misleading "Restore" button. Fix: mark current by matching `current_version_id`.

### INS-009 / INS-027 — Mid-run rebuild drops org settings + channel
The canonical builder construction (`context_hub.py:315-326`) passes `current_user`, `organization_settings`, `mode`, `channel`. The mid-run re-scope after a focus change (`agent_v2.py:4132-4137`) constructs a fresh `InstructionContextBuilder` passing only `current_user`, `data_source_ids`, `mode` — dropping `organization_settings` (so `max_instructions_in_context` reverts to the 50 default; an org configured higher silently drops active `intelligent` instructions with no `<available_instructions>` catalog signal) and `channel` (so `applicable_channels` scoping is not applied and channel-specific instructions leak into the wrong channel). `prompt_builder_v3` itself does **not** scope instructions — it consumes the pre-rendered string — so this is the only divergence between the two named paths. Fix: pass the full arg set at the mid-run construction site (or reuse the cached section).

### INS-010 — Cross-agent/cross-org metadata leak via global references
`_validate_reference` filters by `organization_id` for `object_type in {instruction, connection_tool}` (152-192) but the `metadata_resource` (102-115) and `datasource_table` (117-161) branches query by id with **no org filter and no caller-access check** — only an optional `data_source_ids` membership check that is skipped when `data_source_ids` is falsy. `create_instruction` passes `ds_ids=None` for a global (instruction_service.py:144), so a global instruction can reference any `metadata_resource`/`datasource_table` by id. `_instruction_to_schema_with_references` then hydrates the full schema (name, columns, `sql_content`) into `GET /instructions/{id}`, which the creator can always read. Impact: a per-agent manager reads table/column/SQL of agents they don't manage, or (with a known UUID) another org's resource. Exploitation needs the target UUID (not enumerable via the access-scoped `available-references` endpoint) — hence P2, not P1. Fix: org-filter + access-check these two reference branches regardless of `data_source_ids`.

### INS-011 — Shadowed suggestion-review subsystem
Template order: `v-if="reviewMode"` (`InstructionTrackedChanges`, `/hunks/*`) → `v-else-if="diff"` (inline buildId hunks + `doResolve`→`/resolve`, plus eval strip) → `v-else` (plain text + amber pending banner). `reviewMode = detail && !creating && !editing && !(diff && diff.versionId) && pendingBuilds.length>0 && !reviewEmpty`. `viewSuggestion` sets `diff.buildId` with `versionId:null` (doesn't falsify `reviewMode`) and doesn't clear `pendingBuilds` — so whenever anything is pending, `reviewMode` is true and the `diff`/banner branches never render. Two full review implementations exist against two different backend contracts (client-computed `/resolve` with `promote_text/remaining_text` vs server-authoritative `/hunks/*`); the `/resolve` one — and the eval-validation strip nested inside it — is dead on this surface. Decide which contract is canonical and delete the other.

### INS-017 — Create-tool "Rejected" on transient failure
`refreshResolutionState` (CreateInstructionTool.vue:370-387) fetches `/instructions/{id}` and, on `instErr || !instData`, sets `resolution='rejected'`. A network blip on a still-pending suggestion renders "Rejected". `EditInstructionTool` (492-514) documents this exact failure mode and replaced it with `/instructions/{id}/builds/{buildId}/verdict` returning `pending|accepted|rejected|unknown`, treating fetch failure as `unknown`. The two cards sit side-by-side in one transcript and report resolution with different reliability. Fix: give the create card the same `/verdict` treatment.

### INS-029 / INS-030 — Split edit permissions (frontend) & edit/delete asymmetry (backend)
Frontend: `canEditDetail = useCanAll('manage_instructions','data_source', <all attached ds>)` gates Edit/Delete/body; `metaEditable = canEditInstr || creating` where `canEditInstr = useCan('manage_instructions')` (org-level). A user with per-DS `manage_instructions` on every attached agent but no org grant gets an editable body but read-only meta selects (rendered as `<span>`s), while `save()` still sends those fields — a confusing half-editable pane.
Backend: for a DS-attached instruction owned by X on agent A, a *second* manager Y of A: DELETE passes the decorator + route `check_resource_permissions` → deletes; UPDATE passes the same gates but `_determine_update_type` sees Y is neither org-admin nor owner → 403. So Y can delete (and resolve/accept-hunk/revert, which are route-body-gated) but cannot plain-edit. `test_rbac_instructions.py` branch-4 docstring claims per-DS grantees can edit, but the test only exercises *owner* self-edit — the gap is uncovered. Decide the intended rule and align decorator, `_determine_update_type`, and the frontend gate (`canEditInstruction` should also consult ownership — see INS-031/INS-032).

### INS-033 — Global-create inconsistency across endpoints
`create_global_instruction` calls `require_org_permission(manage_instructions)` when `data_source_ids` is empty (routes:97-103), explicitly so agent managers can't author globals. `create_private_instruction` (routes:67-81) uses the `resource_scoped` decorator (`has_any_resource_permission`) and, with empty `data_source_ids`, skips the body's `check_resource_permissions` → a global row owned by the manager is created. `test_rbac_instructions.py::test_create_instruction_matrix` and `test_global_instruction_authority.py` currently assert 200 for this, so the codebase blesses it (hence P3/documented drift, not P1). Live impact is bounded: `_can_auto_publish_build` returns False for a non-admin build containing a global, so it sits `pending_approval`. The UI never triggers it (globals go to `/instructions/global`). Fix: enforce the org gate on the private endpoint when `data_source_ids` is empty, and update the tests to match the intended rule.

### INS-040 — Hardcoded-copy cluster (i18n)
Fully or partially hardcoded English (app ships 10 locales: ar, de, es, fr, he, it, pt, ru, sv + en):
`BuildExplorerModal.vue` (0 `$t`; "Active"/"Pending"/"Rejected"/"Edit Instruction"/"Select a build to view details"), `BulkLabelsModal.vue` ("Set Labels"), `BulkScopeModal.vue` ("Set Source Scope"), `InstructionsFilterBar.vue` ("Search instructions…"/"Category"/"Load Rule"/"Data Source"), `PrimaryInstructionMenu.vue` ("Edit instruction"/"Replace with existing…"), `InstructionTrackedChanges.vue` ("Loading…"/"No pending changes — all resolved."), `InstructionSuggestions.vue` (all strings incl. toasts), `console/RecentInstructions.vue`, `InstructionDetailsModal.vue`, `InstructionsListModalComponent.vue`, `InstructionLearningSettingsModal.vue`, `InstructionLabelsManagerModal.vue`, `InstructionLabelFormModal.vue`, and `ReportAgentPanel.vue` (mixes literal "Overview"/"Edit instead" with `$t` siblings). Can be one Linear epic with a per-component checklist, or split per component.

---

## Notes for triage
- **INS-001 (XSS)** and the **INS-003/004/005/023 unversioned-mutation** cluster are the highest-value fixes.
- **INS-009/027** is a single fix (pass full args at the mid-run builder site) resolving two findings.
- **INS-029→033** are one coherent permission-model workstream; settle the intended rule for "per-DS manager vs owner vs org-admin" once and align all four gates + the tests.
- **UNCLEAR / needs a live sandbox click:** the `/api/instructions` vs `/instructions` prefix in INS-016 (does the proxy resolve both?); and confirming INS-001 renders identically in the report side-pane. Everything else is code-verified.
- Raw per-agent notes are in the session scratchpad (`agent1-main-surface.md` … `agent5-prompt-loading.md`, `direct-findings.md`).
