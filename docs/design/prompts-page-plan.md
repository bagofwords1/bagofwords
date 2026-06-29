# /prompts page + prompt management — build plan

Status: **approved design, ready to build**. Branch: `claude/scheduled-prompt-subscriptions-6gwu3y` (PR #460).

The Prompt model, access-aware list/CRUD, `@`-mention/search consumption, and
`PromptParametersModal` / `usePromptFill` already exist. This adds the **management
+ run hub**: a `/prompts` page, read-only + authoring modals, a real run endpoint,
admin **run-for** (safe run-on-behalf), and removes the `is_starter` flag.

---

## 1. Scope

- A `/prompts` page for **all users** (role-aware): each sees prompts they can resolve.
- **Card grid** + filters (search, scope, **by agent**, **by user**).
- **Card click → read-only modal**; Edit/Delete if `can_manage`; **Run for…** if admin.
- **Create/edit/delete** via a sectioned authoring modal (`MentionInput` text + params + scope). Anyone can create a **private** prompt; admins can also create agent/global and set private.
- **Run** (self): param modal if params → `POST /prompts/{id}/run` → new report → navigate.
- **Run for…** (admin): member/group picker + confirmation → `POST /prompts/{id}/run-for` → per-user **private** reports + inbox notification + audit.
- **Remove `is_starter`** — an agent's conversation starters = **all** prompts associated with that agent.

---

## 2. Data model changes

- **Remove `Prompt.is_starter`** (migration drops the column; schema/service cleanup).
- **New `prompt_runs` table** (usage tracking + audit + run-for provenance):
  `id, prompt_id, user_id (whose run / report owner), actor_id (who triggered; = user_id for self-run), report_id, parameters JSON, created_at`.
  Enables a future "Most used" sort and is the audit trail for run-for.
- Migration off the current head (`f7a8b9c0d1e2`): drop `is_starter`, create `prompt_runs`.

`Prompt` final shape: `title, text, mode, model_id, mentions, parameters, scope (agent|global|private), data_sources (M2M), user_id, organization_id`.

---

## 3. Backend API

### Reads
- `GET /api/prompts` (existing, access-aware) — add filters: `data_source_id` (agent), `created_by` (user), `scope`, `search`. Returns `can_manage` (existing). Later: `run_count` from `prompt_runs`.

### Run (self)
- `POST /api/prompts/{id}/run` body `{ parameters }`
  - Authz: caller can **resolve** the prompt (visibility rules).
  - Substitute params into `text`; create a **new report** owned by the caller, seeded with the prompt's `data_sources` + `mentions`; create the first completion (the substituted prompt); record a `prompt_runs` row (actor = user = caller).
  - Returns `{ report_id }` → frontend navigates to `/reports/{id}`.

### Run for others (admin) — safe run-on-behalf
- `POST /api/prompts/{id}/run-for` body `{ principal_type: 'users'|'group', user_ids?|group_id?, parameters }`
  - **Authz**: `full_admin` OR `manage` on **all** the prompt's agents.
  - **Expand** principal → users; **filter** to users who can resolve the prompt (access to its agents); the rest are reported as `skipped`.
  - For each target user: create a report **owned by + private to that user**, run **as that user** (their data access), record `prompt_runs` (actor = admin, user = target), and **emit an inbox notification** ("`<Admin>` ran `<prompt>` for you → view").
  - **Params** filled once by the admin, applied to all.
  - **Audit** via `audit_service`.
  - Returns `{ ran, skipped, skipped_users }`.

### CRUD (existing, unchanged authz)
- `POST/PUT/DELETE /api/prompts` — scope gating: `global` → full admin; `agent` → `manage` on all agents; `private` → any user (incl. admins choosing private).

---

## 4. Security invariants (run-for) — built in, not bolted on

1. **Vetted prompt only** — run-for runs a *saved* prompt the admin selects; no free-form text at trigger time (kills exfiltration-prompt crafting).
2. **Runs as the target user** — their data access, not the admin's.
3. **Output is the target's private report — the admin must NOT be able to read it.**
   - ⚠️ **Must-verify before building run-for**: confirm reports are owner-private by default and there is **no admin/full-admin bypass** that lets the actor open another user's report. If such a bypass exists, run-for reports must be marked so the actor cannot read them. This is the load-bearing invariant — without it, run-for becomes a data read-around.
4. **Access-filtered fan-out** — skip targets who can't resolve the prompt; surface the skip count.
5. **Audited** (`prompt_runs` + audit log) and **transparent** (inbox notification to each target).

---

## 5. Frontend

### Page — `pages/prompts/index.vue`
- **Header**: title, `[ + New prompt ]`, search box, **Scope** segmented (All/Global/Agent/Private), **Agent ▾**, **User ▾** filters.
- **Card grid**: responsive 1/2/3 columns.
- **Empty state**: "Create your first prompt" (authors) / "No prompts yet".

### `PromptCard.vue`
- Scope icon+chip (🌐 global / agent / 🔒 private), title, **text preview** (clamped, `{{params}}` highlighted), meta (scope/agent/params/category), **Run** button, hover **edit/delete** when `can_manage`.

### `PromptViewModal.vue` (read-only, on card click)
- Full text (highlighted params), parameters list, scope/agents, author/category.
- Actions: **Run**; **Edit/Delete** (`can_manage`); **Run for…** (admin).

### `PromptEditModal.vue` (authoring — sectioned, scheduled-task-modal feel)
1. **Prompt** — Title + `MentionInput` text (mentions + `{{param}}`; "Insert parameter" helper).
2. **Parameters** — repeatable rows `name · label · type · required · options`.
3. **Audience** — Scope: Private (always) / Agent (multiselect; shown if `manage` any agent) / Global (full admin only).
4. **Advanced** (collapsed) — mode, model, category/tags.
- Self-gates by permission, so one modal serves users and admins.

### `RunForModal.vue` (admin)
- Member search (org members) + group picker; params once (reuse `PromptParametersModal`); **confirmation** ("Run for N members, M skipped — results go to each privately") → `POST /run-for` → result toast.

### Run flow
- Run → if `parameters`, `PromptParametersModal` → `POST /run` → navigate to `/reports/{id}`.

### Reuse
`PromptParametersModal`, `usePromptFill` (`substitute`/`mergeMentions`), `MentionInput`, members/groups endpoints, the inbox (`useNotifications`). Add a `usePrompts` composable for the page's API calls.

### Nav
Add a **Prompts** entry to `layouts/default.vue` sidebar.

---

## 6. Remove `is_starter` (cross-cutting cleanup)

- Backend: drop column (migration) + remove `starters_only` param + remove from schema/service; `materialize`/backfill create plain agent prompts.
- The four agent surfaces fetch `?data_source_id=X` (drop `starters_only`):
  `KnowledgeExplorer.vue`, `report/ReportAgentPanel.vue`, `DataSourceQuestionsHome.vue`, `pages/reports/[id]/index.vue` (empty-state chips).
- An agent's "edit conversation starters" modal now edits that agent's prompts (all of them).
- UI: those surfaces cap + "show more"/scroll (an agent may now have many).

---

## 7. Phasing

1. **Backend core**: remove `is_starter` (migration + schema/service + 4 agent-surface fetches); add `prompt_runs`; `POST /run`; list filters (agent/user/scope/search). Tests.
2. **`/prompts` page**: card grid + filters, `PromptCard`, `PromptViewModal`, `PromptEditModal`, nav, Run flow. Verify (screenshots).
3. **Run-for**: verify the privacy invariant (§4.3) first; `POST /run-for` + fan-out + per-user private reports + inbox notify + audit; `RunForModal`. Verify.
4. Polish: filters/sort, i18n labels, empty/loading states.

Verification: live app + Playwright **screenshots** (no CI-run specs, per prior CI hygiene); backend unit tests for run/run-for fan-out + access filtering + the privacy invariant.

---

## 8. Open items to confirm during build
- **§4.3 report privacy** under run-for (the must-verify).
- Multi-agent prompts appear as starters on **all** their agents (accepted).
- "Most used" sort deferred until `prompt_runs` has data.
