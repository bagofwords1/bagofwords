# Instruction surfaces — consistency audit

**Status:** investigation only. No code changed.
**Question:** where do the Knowledge Explorer, the report-session Agent panel, and
the in-transcript instruction tool cards show *different data* for the *same*
instruction?

**Answer:** in a lot of places. There is no shared "instruction view" component
and no shared display contract — each surface picks its own endpoint, its own
projection, its own label functions, and its own body renderer. The list below
is grouped by root cause, then by concrete symptom.

---

## 0. The surfaces

| # | Surface | File | Body renderer | Metadata source |
|---|---------|------|---------------|-----------------|
| 1 | Knowledge Explorer tree row | `components/KnowledgeExplorer.vue` (`InstrLeaf`, ~L3580) | — (title only) | `GET /instructions?view=light` |
| 2 | Knowledge Explorer detail pane | `components/KnowledgeExplorer.vue` L647–947 | `InstructionEditor` (TipTap, `editable=false`) | `GET /instructions/{id}` (full) |
| 3 | Agent panel — Overview → Primary instruction | `components/report/ReportAgentPanel.vue` L114–137 | `InstructionText` (**no** `markdown`) | `GET /data_sources/{id}` → hand-built `primary_instruction` dict |
| 4 | Agent panel — Instructions list row | `ReportAgentPanel.vue` L333–400 | `InstructionText` (plain, 80 chars) | `GET /instructions?view=light` |
| 5 | Agent panel — instruction detail/edit | `InstructionGlobalCreateComponent.vue` (view mode L7–355, edit mode L358+) | `InstructionEditor` | `GET /instructions/{id}` (re-fetched internally) |
| 6 | `create_instruction` tool card | `components/tools/CreateInstructionTool.vue` | `InstructionText` (`markdown`, **no refs**) | `tool_execution.arguments_json` / `result_json` |
| 7 | `edit_instruction` tool card | `components/tools/EditInstructionTool.vue` | `InstructionText` (`markdown`, no refs) + `TrackedChangesView` | `result_json` + `GET /instructions/{id}` |
| 8 | `read_instruction` tool card | `components/tools/ReadInstructionTool.vue` | **raw `whitespace-pre-wrap` text** | `result_json` only |
| 9 | `search_instructions` tool card | `components/tools/SearchInstructionsTool.vue` | `InstructionText` (`markdown`, no refs) | `result_json.instructions[]` |
| 10 | "N instructions loaded" popover | `pages/reports/[id]/index.vue` L493–518 | — | `message._loaded_instructions` |
| 11 | Knowledge-harness card | `components/KnowledgeGroup.vue` | `TrackedChangesView` | `result_json` |
| 12 | Knowledge Explorer → All instructions modal | `instructions/AllInstructionsModal.vue` → `InstructionsTable.vue` | — | `view=light`, `live=true` + `live=false` |

Editors alone: **three** (KE inline pane, `InstructionGlobalCreateComponent`
inline in the panel, `InstructionModalComponent` → the same component in
`split-layout` mode, opened from `CreateInstructionTool` and the training modal).

---

## 1. Root cause A — different endpoints ⇒ different fields exist at all

`GET /instructions?view=light` returns `InstructionListItemSchema`
(`backend/app/schemas/instruction_schema.py` L223). It deliberately drops:
`text` (→ `preview`, 280 chars), `formatted_content`, `structured_data`,
`user` (only `user_id`), **`references`**, **`description`**, **`evidence`**,
**`primary_for`**, `reviewed_by`, `thumbs_up`.

`GET /instructions?view=full` returns `InstructionListSchema` — which *also*
has **no `references`** and no `evidence` / `kind` is present but `primary_for`
is.

Only `GET /instructions/{id}` (`InstructionSchema`) has the full set.

### 1.1 Dead bindings in the Agent panel list (rows come from `view=light`)

`ReportAgentPanel.vue`:

- **L382–400 — the entire References block can never render.** `inst.references`
  is not in `InstructionListItemSchema`, so `v-if="inst.references?.length"` is
  always false. Dead code.
- **L375–379 — the author + created-date badge can never render.**
  `inst.user` is not in the light schema (`user_id` only). So the panel list
  silently shows no author/date, while the KE detail pane (L923–925) shows both,
  and `InstructionsTable` (L370) shows the creator name.
- **L347 — `:references` mapping on the preview `InstructionText` is always
  `undefined`**, so `@Table` mentions in the preview render as plain text there
  and as indigo chips everywhere else.
- **L342 — the title fallback is `(inst.preview ?? inst.text)?.slice(0, 60)`**,
  i.e. a raw 60-char slice *including newlines*. Everywhere else
  (`instructionRowLabel`, both tool cards) takes `split('\n')[0]` first. Same
  instruction, different row label.

### 1.2 Primary instruction is a hand-built dict, not the schema

`backend/app/services/data_source_service.py` L1197–1222 builds
`primary_instruction` by hand with only:
`id, text, status, category, source_type, load_mode, title, organization_id, references[]`.

Consequences:

- The refs it emits carry `object_type / object_id / display_text` but **not**
  `data_source_type` / `data_source_icon` (which `InstructionReferenceSchema`
  *does* have). So mention chips in the primary instruction lose their data-source
  icon and fall back to the generic table glyph — in **both**
  `KnowledgeExplorer.vue` L492 and `ReportAgentPanel.vue` L130. The same
  instruction opened in the detail pane shows the right icons.
- No `kind`, `description`, `labels`, `data_sources`, `current_build_*`,
  `applicable_modes/channels`, `evidence`, `created_at`. So the primary
  instruction is never shown as pending-review, never shown as a skill, never
  shows its labels — in either surface.

### 1.3 `InstructionText` itself drops `data_source_icon`

`components/instructions/InstructionText.vue` L81–88: `normalizedRefs` maps to
`{ id, type, name, data_source_type }` — **`data_source_icon` is dropped**, yet
the template L10–12 tests and binds it. Per-agent custom icons (`emoji:` /
`preset:`) therefore never appear in a mention chip, anywhere.

---

## 2. Root cause B — different *sets* of instructions are listed

| Query param | KE tree (`loadGroup`, L3088) | Agent panel (`fetchTabData`, L954) |
|---|---|---|
| `include_archived` | **`true`** | *(omitted → false)* |
| `include_global` | **`false`** for an agent group | **`true`** |
| `kind` | separate `kind=skill` group | *(not used)* |
| `include_hidden` | omitted → false | omitted → false |

Symptoms:

- **Archived instructions exist in the KE tree and are invisible in the panel.**
- **The panel's per-agent list is agent instructions *plus* every global
  instruction**, and the tab count (`ReportAgentPanel.vue` L830) counts them
  together. KE deliberately separates "Global instructions" from the agent's own
  group, so the two surfaces report different counts for the same agent.
- **Skills** get their own group + a Skill/Instruction pill in KE
  (L888–889); in the panel they appear inline with nothing distinguishing them.
- `is_seen = false` ("Hidden") instructions are excluded from *both* lists — but
  the panel's editor exposes an `is_seen` toggle (`InstructionGlobalCreateComponent`
  L253–255 / the edit form), so a user can hide an instruction from a surface
  that never explains where it went. KE has no `is_seen` control at all.

---

## 3. Root cause C — the body renders four different ways

For one and the same `instruction.text`:

| Surface | Renderer | Markdown? | Mentions chipped? | Mermaid? |
|---|---|---|---|---|
| KE detail pane | `InstructionEditor` (TipTap) | yes | editor decorations | ? (editor path) |
| Panel detail (view mode) | `InstructionEditor` | yes | editor decorations | ? |
| **Panel Overview → primary** | `InstructionText`, **`markdown` not set** | **no** | yes | **no** |
| KE Overview → primary | `InstructionText` `:markdown="true"` | yes | yes | yes |
| create/edit/search tool cards | `InstructionText` `:markdown="true"` | yes | **no** (no `references` passed) | yes |
| `read_instruction` card | raw `{{ text }}` in a `<pre>`-ish div | **no** | **no** | **no** |

So the *same primary instruction* renders as formatted prose in the Knowledge
Explorer and as a wall of literal `## ` / `- ` characters in the report Agent
panel Overview tab (`ReportAgentPanel.vue` L130–134 vs `KnowledgeExplorer.vue`
L492). This is the most visible single discrepancy.

`InstructionText`'s own comment (L121) says it "mirrors InstructionEditor's
pipeline so read-only and edit views render identically" — that claim is only
true when `markdown` is passed.

---

## 4. Root cause D — three copies of the display-label logic

`composables/useInstructionHelpers.ts` is the shared one, but
`InstructionGlobalCreateComponent.vue` re-implements every function locally
(L1307–1504) and the tool cards use neither.

| Value | `useInstructionHelpers` (KE tree, KE detail, panel **list**, InstructionsTable) | `InstructionGlobalCreateComponent` (panel **detail**) | tool cards |
|---|---|---|---|
| `load_mode: intelligent` | `"Smart"` (hardcoded EN) | `t('…loadMode.smartLabel')` (localised) | **raw `"intelligent"`** |
| `load_mode: disabled` | `"Off"` | **falls through → raw `"disabled"`** | raw `"disabled"` |
| `status: published` | `"Active"` (hardcoded EN) | `t('…status.active')` (localised) | n/a |
| `category: data_modeling` | `"Data Modeling"` (hardcoded EN) | `t('…category.dataModeling')` | **raw `"data_modeling"`** |
| created date | KE: `Mon D, HH:MM` (L3537) | `Mon D, YYYY, HH:MM` (L1232) | n/a; panel list: `Mon D, YYYY` (L892) |

Consequences:

- **In any non-English locale the panel list badges are English and the panel
  detail sidebar right below them is localised.** Same screen, same field.
- `load_mode: 'disabled'` displays as the raw string `"disabled"` in the panel
  detail sidebar and as `"Off"` in KE.
- The tool cards (`CreateInstructionTool` L85, `EditInstructionTool` L144,
  `SearchInstructionsTool` L38/41) and the "instructions loaded" popover
  (`pages/reports/[id]/index.vue` L511, L514) print **raw enum values** for
  category and load mode.
- Inside KE itself: `categoryOpts` (L1856) labels options with
  `h.formatCategory` (hardcoded EN) while `statusEditOpts` / `loadOpts` /
  `kindOpts` (L1726, L1853–1855) use `$t`. Half-localised picker.

---

## 5. Root cause E — different option sets for the same field

- **Load mode.** KE `loadOpts` (L1854) = `always | intelligent | disabled`.
  `InstructionGlobalCreateComponent.loadModeOptions` (L1302) = `always |
  intelligent` — **no `disabled`**. An instruction set to `disabled` in KE
  cannot be seen as such, nor changed back, from the report panel.
- **Category.** KE `categoryOpts` (L1856) is server-driven
  (`GET /instructions/categories` → the `InstructionCategory` enum, minus
  `dashboard`). `InstructionGlobalCreateComponent.categoryOptions` (L1416) is
  hardcoded to `general | code_gen | system | visualizations` — missing
  `data_modeling` and `dashboard`. Opening a `data_modeling` instruction in the
  panel gives a select whose current value isn't in its own option list.
- **Category enum drift, backend vs frontend.** `InstructionCategory`
  (`instruction_schema.py` L31) = `code_gen, data_modeling, general, dashboard,
  visualization` (**singular**). Every frontend map uses `visualizations`
  (**plural**) and adds `system`, which is not in the enum. So
  `GET /instructions/categories` returns `visualization`, which
  `formatCategory` does not know and renders verbatim; and `system` rows (which
  exist in the FE type union, `useInstructionHelpers.ts` L31) can't come back
  from that endpoint.
- **Status.** KE offers `published | draft` only (L1853); the panel editor the
  same (L1349); but `archived` is a real value both render read-only. KE loads
  archived rows, the panel doesn't (§2).

---

## 6. Root cause F — "pending review" is derived three different ways

1. **KE tree row + KE detail header** — `isPending(ins)`, backed by
   `pendingInstrIds` from `GET /instructions/counts`
   (`pending_instruction_ids`). The header deliberately *neutralises* the build
   heuristic: `h.getStatusLabel({ ...detail, current_build_status: null,
   current_build_id: null })` (L658–659).
2. **KE detail → Details tab → Status pill** — `h.getStatusLabel(detail)`
   (L850), i.e. the **build heuristic**, un-neutralised.
   ⇒ **Within one open instruction, the header can say "Active" while the
   Status pill two panes below says "Pending review".** (This is exactly the
   rebased-no-op-build case the L655–657 comment describes.)
3. **KE Status pill for an admin** — when `metaEditable` is true the pill is
   replaced by a `KSelect` bound to `draft.status`, whose options are only
   Active/Inactive. So an admin and a viewer looking at the same pending
   instruction see different status text.
4. **Agent panel list** — `helpers.getStatusLabel(inst)` → build heuristic
   (differs from KE tree, which uses the server pending set).
5. **Agent panel detail** — `isPendingReview` (`InstructionGlobalCreateComponent`
   L1359), a *third* implementation of the same heuristic, reading
   `props.instruction` (which may be a light row) rather than the fetched full
   row.
6. **Agent panel banner** — `ReportAgentPanel.vue` L201 checks
   `current_build_status ∈ {draft, pending_approval}` inline, a fourth copy,
   and prints "unpublished draft" vs "pending review" — wording that appears
   nowhere in KE.

---

## 7. Root cause G — three permission derivations for "can I edit this?"

| Surface | Rule |
|---|---|
| KE (`canEditInstruction`, L2491) | `useCanAll('manage_instructions','data_source', attachedDsIds)` → for a **global** instruction (no DS) requires **org-level** perm |
| Panel detail (`InstructionGlobalCreateComponent` L1215) | same shape, but `editorTargetDsIds` **falls back to *all* agents** when the instruction has none (L1213) → a per-agent manager who manages every agent gets **Edit** on a global instruction |
| Panel header/create (`ReportAgentPanel` L717) | perm on the *currently selected agent*, regardless of what the open instruction is attached to |

⇒ The same global instruction can be read-only in KE and editable in the report
panel for the same user.

Also: KE shows a `Read only` lock chip (L681) when it withholds edit;
the panel shows a "Suggest edit" button instead (`isSuggestMode`, L1223 —
`canSuggestInstructions` is hardcoded `true`), so the two surfaces disagree on
whether a non-manager can propose anything.

---

## 8. Field-presence matrix (detail views)

| Field | KE detail pane | Panel detail (`InstructionGlobalCreateComponent` view mode) |
|---|---|---|
| title | ✅ (editable) | ✅ (uppercased by default — `uppercaseTitle` defaults `true`) |
| **description** | ✅ L826–827 | ❌ **not shown, not editable, not in the payload** |
| text | ✅ | ✅ |
| status | ✅ | ✅ |
| category | ✅ | ✅ |
| load_mode | ✅ | ✅ (no `disabled` option) |
| **kind** (skill/instruction) | ✅ L888–889 | ❌ |
| agents / data_sources | ✅ | ✅ |
| **folder placement** | ✅ L869–874 | ❌ |
| **primary_for** badge | ✅ L876–877 | ❌ |
| references | ✅ | ✅ |
| labels | ✅ (name only, no colour) | ✅ (**with colour dot** — different rendering) |
| **applicable_modes** | ✅ L900–908 | ❌ |
| **applicable_channels** | ✅ L909–917 | ❌ |
| source (user/ai/git) | ✅ L922 | ✅ (icon in "Created by") |
| author | ✅ | ✅ |
| created_at | ✅ | ✅ (different format) |
| **updated_at** | ✅ L925 | ❌ |
| **evidence** | ✅ L928–930 | ❌ |
| **is_seen (visibility)** | ❌ | ✅ L252–255 |
| **reviewed_by ("Approved by")** | ❌ | ✅ L215–218 (reads DEPRECATED field) |
| **Analyze tab** (related + impact) | ✅ L933–946 | ❌ (slot exists, panel doesn't pass it) |
| version history | ✅ side panel + restore | ✅ dropdown + Monaco diff + field-change list |
| download markdown | ✅ L677 | ❌ |
| git file path | ❌ | ✅ L19–22 (only when `isGitSourced` prop is passed — **ReportAgentPanel never passes it**, so git instructions show no path/sync state there either) |

Note the last row: `ReportAgentPanel.vue` L220–228 mounts
`InstructionGlobalCreateComponent` **without** `is-git-sourced` /
`is-git-synced`, so every git-sourced instruction opened from a report session
renders as if it were user-authored — no file path, no sync badge, no
unlink-on-edit confirmation. KE surfaces git provenance via
`h.getSourceIcon` / `getSourceTooltip` (L922) and `GitConnectionButton`.

`description` is the sharpest one: KE writes it on save (`save()` L3378 sends
`description`), the panel's `buildInstructionPayload` (L1627) omits it entirely
— so it survives (PUT treats `None` as no-change) but is invisible and
uneditable from the report session.

---

## 9. Reference round-trip loss

- KE `syncDraft` (L3333) keeps `display_text` and sends it back on save (L3378).
- `InstructionGlobalCreateComponent.buildInstructionPayload` (L1639–1644) sends
  only `{ object_type, object_id, column_name, relation_type: 'scope' }` —
  **`display_text` is dropped**, and `relation_type` is hardcoded to `'scope'`
  even for refs that were `'mention'`.

⇒ Editing an instruction from the report panel can rewrite its references'
display text and relation type, changing how the *same* instruction's mention
chips render afterwards in KE.

---

## 10. Tool cards (in `/reports/{id}` transcripts)

`CreateInstructionTool.vue`:

- Reads **`arguments_json`** for category / load_mode / confidence / evidence /
  `table_names` — i.e. what the model *asked for*, not what was stored. `text`
  correctly prefers `result_json.text` (L304, with a good comment) but the
  metadata does not get the same treatment. If the service clamped/normalised
  `category` or `load_mode`, the card shows the un-normalised input.
- Shows **`confidence`** and a **table count**, which no other surface shows.
- Shows **no title** — the header is `text.split('\n')[0]` even when
  `result_json.title` exists (compare `EditInstructionTool.headerTitle` L631,
  which *does* prefer the title, and `ReadInstructionTool` L57, which prefers
  `title` then `short_id`).
- Click-to-edit opens `InstructionModalComponent` → a *fourth* rendering of the
  editor (`split-layout`), not the panel and not KE.
- On fetch failure it falls back to a stub `{ id, text, category, load_mode }`
  (L432) and hands that to the editor, which will then re-fetch anyway.

`EditInstructionTool.vue`:

- `displayTableCount` (L597) counts **all** `fetchedInstruction.references`
  and labels them "tables" — refs of type `connection_tool` / `instruction` /
  `metadata_resource` are counted as tables.
- `metadataChanges` (L608) prints raw values into i18n strings
  (`changeCategory: { value: 'data_modeling' }`).
- Not registered in the shared/public report view
  (`pages/c/[token]/index.vue` imports Create/Read/Search but **not**
  `EditInstructionTool` — L260–267), so an `edit_instruction` block in a shared
  report falls through to the generic tool renderer.
- `CreateInstructionTool` honours a `readonly` prop for that shared view;
  `EditInstructionTool` has no such prop.

`ReadInstructionTool.vue`: renders the body as raw text, no markdown, no
mentions, capped at `max-h-48`. Nothing else in the app renders an instruction
body that way.

`SearchInstructionsTool.vue`: raw `category` / `load_mode` badges (L38–42),
title fallback truncates at **80** chars with `…` while the others use **60**
with `...`.

`pages/reports/[id]/index.vue` L493–518 ("N instructions loaded"): raw
`category`, raw `load_mode`, title falls back to `"Untitled"` **without** the
first-line-of-text fallback every other surface uses — so untitled instructions
are indistinguishable rows in that popover.

---

## 11. Smaller inconsistencies worth noting

- `ReportAgentPanel` L347 slices the preview to 80 chars *after* already having
  a 280-char `preview`; KE uses 60. Different truncation of the same string.
- `ReportAgentPanel` L358–361 renders a purple "Any" chip for global
  instructions; KE renders a grey globe chip labelled with
  `agentsPage.allAgentsPlaceholder` (L863). Same state, different word and
  colour.
- `ReportAgentPanel`'s status/category filter option lists are **derived from
  the loaded rows** (L741–749), so the available filters change as the list
  changes; KE's come from the server enum + a fixed list.
- `KnowledgeGroup.vue` labels are hardcoded English (`'New instruction'`,
  `'Text changes'`, `'Created …'`, `'Edited …'` — L111, L284, L289) while every
  neighbouring surface is `$t`-driven.
- `ReportAgentPanel` L186 `Edit instead` is a hardcoded English string in an
  otherwise fully-i18n'd template.
- `InstructionGlobalCreateComponent` defaults `uppercaseTitle: true` (L911). In
  view mode that is CSS only (L16), but the edit-mode input's `@input` handler
  (L372) writes `value.toUpperCase()` back into the model — so **as soon as a
  user touches the title field in the report panel, the stored title is
  uppercased for good**. `ReportAgentPanel` L220 does not pass
  `:uppercase-title="false"`; KE stores and shows the title as typed. This is
  the one item here that mutates data rather than only displaying it
  differently.
- `InstructionGlobalCreateComponent` reads `props.instruction.reviewed_by`
  (L215) — flagged `DEPRECATED - not used` in `instruction_schema.py` L64/L292.

---

## 12. Suggested direction (not implemented)

1. **One display contract.** Delete the local label helpers in
   `InstructionGlobalCreateComponent` and move `useInstructionHelpers` to `$t`
   so every surface is localised identically. Add `disabled` to its load-mode
   map and to the panel's option list; drive category options from
   `GET /instructions/categories` everywhere; reconcile
   `visualization`/`visualizations` and `system` between the enum and the FE.
2. **One body renderer.** Make `InstructionText` the read-only renderer
   everywhere, always with `:markdown="true"` and always with `:references`.
   Fix `normalizedRefs` to carry `data_source_icon`. Point
   `ReadInstructionTool` and the panel Overview at it.
3. **One projection.** Either add `references` + `user` (or a minimal author
   projection) to `InstructionListItemSchema`, or delete the dead blocks in
   `ReportAgentPanel` L375–400 that pretend they're there. Have
   `data_source_service` emit `InstructionSchema` (or at least the ref fields
   `data_source_type` / `data_source_icon`) for `primary_instruction` instead of
   the hand-built dict.
4. **One pending derivation.** Pick either the server pending set or the
   `current_build_*` heuristic and use it in all six places; the KE header vs
   Details-pill disagreement is a bug either way.
5. **One list scope.** Align `include_archived` / `include_global` / `kind`
   between `loadGroup` and `fetchTabData`, or make the panel say which extra set
   it's showing.
6. **One permission rule** — most likely KE's `useCanAll` over attached agents,
   applied in `InstructionGlobalCreateComponent` too (drop the "all agents"
   fallback).
7. **Stop the uppercase rewrite** — pass `:uppercase-title="false"` from
   `ReportAgentPanel`, or better, remove the behaviour.
