# Feedback Loop — pre-built skill catalog

Skills existed as a mechanism (`Instruction.kind='skill'`, advertised in
`<available_skills>`, pulled on demand via `read_instruction`) but every org
started empty: someone had to author a playbook from a blank page before the
mechanism did anything. `Instruction.catalog_key` / `catalog_version` were
already reserved on the model for this — "Unused until the catalog ships".

This loop ships the catalog: 16 curated playbooks an admin enables per org,
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

**UI** — `frontend/components/instructions/SkillCatalogModal.vue`, opened from
the Skills group in the knowledge tree. Cards carry Enabled / Update available /
Edited badges; non-admins get a read-only view with a lock notice.

## Two fixes this loop found

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

Two smaller gaps fixed alongside: `catalog_key`/`catalog_version` were absent
from both list projections, and `InstructionListSchema` declared `description`
but never populated it — so every full list row reported `description=None`,
which for a skill is the one line that says what it is for.

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

Enable three skills through the modal, all `POST .../install` → 200; each
button flips Enable → Disable and grows an "Enabled" badge; the tree picks up
the rows and the count. Disable flips back, `DELETE` → 200, and the row leaves
the tree (after fix 2). Search filters; the update flow shows
"Update available" + "Edited", asks for confirmation before overwriting a
customized skill, and clears both badges afterwards.

### 2. UI, as a member

A member invited into the org sees all 16 cards, the lock notice
("Only an administrator can enable a skill for the organization"), and every
toggle `disabled` — `toggle disabled for member: true`.

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
