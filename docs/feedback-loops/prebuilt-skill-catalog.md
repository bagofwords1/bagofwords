# Feedback Loop — pre-built skill catalog

Skills existed as a mechanism (`Instruction.kind='skill'`, advertised in
`<available_skills>`, pulled on demand via `read_instruction`) but every org
started empty: someone had to author a playbook from a blank page before the
mechanism did anything. `Instruction.catalog_key` / `catalog_version` were
already reserved on the model for this — "Unused until the catalog ships".

This loop ships the catalog: 10 curated playbooks an admin enables per org,
and verifies end to end that an enabled skill reaches the planner the way a
hand-authored one does.

---

## What shipped

**Catalog (code-defined, never in the DB)** — `backend/app/ai/skills/`:
- `library/<key>.md` — one playbook per file, YAML frontmatter
  (`key`, `title`, `description`, `category`, `version`, optional `modes`,
  `tags`) plus a markdown body. Authored as prose so a reviewer reads the
  skill in the diff instead of an escaped Python string.
- `catalog.py` — parser + loader. A malformed file is dropped with a log line
  rather than taking down the catalog; `tests/unit/test_skill_catalog_library.py`
  is what turns "dropped silently" into a failing build.

**Install** — `backend/app/services/skill_catalog_service.py`:
copy-on-install into a normal `Instruction` row (`kind='skill'`,
`status='published'`, `load_mode='intelligent'`) stamped with
`catalog_key` + `catalog_version`. Copying (rather than referencing a global
row) is what lets an admin tune a shipped skill; the version stamp is what
lets a later bump be detected without matching on titles, and `is_customized`
warns before an update overwrites local edits.

**Routes** — `GET /api/instructions/skill-catalog` (any member; the catalog is
product documentation), `POST .../{key}/install`, `DELETE .../{key}`,
`POST .../{key}/update` (org-level `manage_instructions` — the same gate as
`POST /instructions/global`, because an installed skill is a global row that
reaches every request in the org).

**UI** — inside the knowledge explorer, where skills already live. Clicking the
**Skills** row opens the catalog in the explorer's own detail panel (the same
slot as Tables / Tools / Evals — a new `panelView.kind === 'skill-catalog'`,
agent-less like `global-evals`), so the catalog is one click from the tree with
nothing to expand first: `frontend/components/instructions/SkillCatalogPanel.vue`.

The panel has two tabs. **Enabled** lists every `kind='skill'` instruction the
org has — pre-built and hand-authored — and **Catalog** lists the shipped
entries. A row is a title, a one-line description and a **toggle**. An
uninstalled entry expands in place to show the **shipped playbook read-only**,
so it can be read before anyone enables it; an installed one opens in the
existing instruction editor — versions, Details strip, Analyze tab and all —
because an enabled skill *is* a normal instruction and needed nothing new built
for it.

Skills is a single button in the tree, not an expandable group: its contents
live in the panel, so there is nothing to expand into. And `listFor('global')`
now excludes `kind='skill'` — a global skill has no data sources either, so
without that filter every enabled skill also appeared under Global
instructions.

Three UI shapes were tried and discarded on the way: a modal (wrong for reading
16 playbooks), a standalone `/skills` page with Enabled/Catalog tabs (a second
home for something the explorer already owns, plus a redundant top-level nav
entry), and a sub-row inside the expanded Skills group (a second click to reach
something the group row itself can open).

## Three fixes this loop found

1. **`_build_skills_catalog` ignored `applicable_modes`/`applicable_channels`.**
   Loaded instructions honored the scoping (lines 213/452/854 of
   `instruction_context_builder.py`); the skills catalog did not. A
   training-only playbook (`dashboard-to-instructions`, `dashboard-to-evals`)
   would burn a catalog slot on every chat request and invite the planner to
   read a procedure built on tools that do not exist in chat mode.
   Fixed by applying `_passes_mode_channel` in `_build_skills_catalog`.
   Regression: `test_mode_scoped_skill_is_only_advertised_in_its_mode`
   (verified failing without the fix).

2. **The knowledge tree kept a stale row after a disable.** `loadGroup`
   merges rows by id (`mergeRows`) and can never remove one, so a disabled
   skill lingered in the tree while the badge count correctly dropped —
   visible in `07c_tree_after_disable` from the first run (badge "Skills 2",
   three rows listed). The existing `deleteInstruction` path already handled
   this by filtering client-side; the modal now reports `removedId` and
   `onSkillCatalogChanged` does the same.

3. **Neither list projection carried a skill's `description`.**
   `InstructionListSchema` declared the field and never populated it; the light
   row did not declare it at all. The Enabled tab therefore rendered a prefix
   of the markdown body (`# Predictive modeling with scikit-learn The sandbox
   can train models…`) where the one-line description belongs — the same line
   the agent sees in `<available_skills>`. Both projections now carry it, along
   with `catalog_key`/`catalog_version`, which were also absent.

An installed skill that had been edited could only be put back to the shipped
text if a *version bump* happened to be pending: the re-sync action was gated on
`update_available`, so local edits were effectively one-way. It is now offered
whenever the copy diverges, labelled **Update** when a new version exists and
**Reset** when it does not (`test_update_resets_local_edits_without_a_version_bump`).

Skill bodies also opened with an `# H1` repeating their own title, which the
detail pane rendered directly under the heading it duplicated. The frontmatter
`title` and `read_instruction`'s response both carry it, so the H1 was dropped
from all 16 files.

A round of self-review caught three more before they shipped: `is_customized`
compared only the body, so scoping a skill to one channel (without editing the
text) skipped the overwrite confirmation and the update silently reset that
scoping; `VALID_MODES` accepted `deep` (retired) and `excel` (never a mode), so
an entry scoped to either would install cleanly and then be invisible in every
real mode; and `uninstall` removed only the first row for a key, so a duplicate
from a raced install kept the playbook advertised after it was disabled.

---

## Environment

Per `.claude/skills/sandbox-feedback-loop/SKILL.md`. Backend on :8000 with
`BOW_DATABASE_URL=sqlite:///db/app.db`, a pinned `BOW_ENCRYPTION_KEY`, and
`BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium`; frontend on :3000 after
`bash scripts/download-vendor-libs.sh frontend/public/libs`. Playwright driven
from the scratchpad with `executablePath: '/opt/pw-browsers/chromium'`.

The i18n locale sweep needs the documented symlink
(`chromium_headless_shell-1193/chrome-linux/headless_shell` →
`/opt/pw-browsers/chromium`); with it, `npx playwright test
--config=playwright.i18n.config.ts` is **30 passed**.

---

## Verification

### 1. UI, as an admin

On `/skills`: the Enabled tab opens on the org's skills and the Catalog tab
lists all 16, each tab's count in its label. Enabling from the Catalog tab is
`POST .../install` → 200, the button flips Enable → Disable, an "Enabled"
badge appears, and the Enabled tab count goes 6 → 7 with the new skill listed;
disabling reverses all of it (`DELETE` → 200, 7 → 6). Search filters both
tabs. The update flow shows "Update available" + "Edited", asks for
confirmation before overwriting a customized skill, and clears both badges
afterwards.

The knowledge tree keeps its own list of skills, and its stale-row bug (fix 2)
was found and fixed while the UI was still a modal in that tree — the fix
still matters because disabling from `/skills` and returning to the tree hits
the same merge path.

### 2. UI, as a member

A member invited into the org sees all 16 cards, the lock notice
("Only an administrator can enable a skill for the organization"), and every
toggle `disabled` — `toggle disabled for member: true`.

### 2b. RTL

`/skills` in Hebrew: `html[dir=rtl]`, the whole page mirrored (sidebar, tabs,
cards, actions), no unresolved `skillCatalog.*` keys. Skill titles and
descriptions stay English by design — they are catalog content the agent
reads, not UI chrome.

### 3. Context — the part that matters

`InstructionContextBuilder` run against the live sandbox DB with 6 skills
installed:

```
INSTALLED CATALOG SKILLS (6):
  dashboard-to-instructions  v1.0  kind=skill status=published load_mode=intelligent modes=['training']
  rca-metric-movement        v1.0  kind=skill status=published load_mode=intelligent modes=None
  erd-mermaid                v1.0  kind=skill status=published load_mode=intelligent modes=None
  data-freshness-preflight   v1.0  kind=skill status=published load_mode=intelligent modes=None
  dashboard-to-evals         v1.0  kind=skill status=published load_mode=intelligent modes=['training']
  ml-sklearn                 v1.0  kind=skill status=published load_mode=intelligent modes=None

===== mode=chat:     4 skills advertised, 0 force-loaded =====
===== mode=training: 6 skills advertised, 0 force-loaded =====

body leaked for <every key>: False
```

The rendered block the planner receives is one line per skill:

```xml
<available_skills>
Skills available on demand. Each lists a short id, title and a one-line
description — the full instructions are NOT shown here. ...
<skill short_id="f1417a47" title="Root cause analysis for a metric movement">
Use when someone asks why a metric moved, dropped, spiked or missed target — decompose the change before explaining it.
</skill>
...
</available_skills>
```

~20 KB of playbook content costs ~600 characters of prompt until something
needs it.

### 4. Advertised → readable

The real `read_instruction` tool, called with the short id from the catalog
against a real report context, returns `success: true` and the full body
(5280 chars for `rca-metric-movement`, 4302 for `erd-mermaid`) — so the
progressive-disclosure loop closes.

### 5. Tests

- `tests/unit/test_skill_catalog_library.py` — 23 passed. Every shipped file
  parses; keys unique and filename-matched; description within the 160-char
  cap the prompt truncates at and trigger-shaped ("Use when…"); malformed
  frontmatter rejected in 8 shapes.
- `tests/e2e/test_skill_catalog.py` — 11 passed. Lifecycle, idempotency,
  re-install, unknown key, customized detection, version re-sync, and the
  three context assertions.
- `tests/e2e/rbac/test_skill_catalog_admin_gate.py` — 3 passed. Member
  refused on install/uninstall/update with nothing changed; member may browse;
  installs do not leak across orgs.
- Regression: instruction e2e suites (`test_instruction*.py`,
  `test_read_instruction_tool.py`, `test_search_instructions_chat_mode.py`,
  `test_skills_catalog.py`, `test_skill_smart_loading.py`) — 132 passed.

## Not verified here

A live LLM turn (planner actually calling `read_instruction` mid-conversation)
could not run: the sandbox's Anthropic key returns
`400 … credit balance is too low`, recorded on the completion row as
`{"code": "quota"}`. Verification 3 and 4 cover the same contract at the
context and tool layers, which is where the behavior is defined; a
credit-bearing key would let the loop go one step further.
