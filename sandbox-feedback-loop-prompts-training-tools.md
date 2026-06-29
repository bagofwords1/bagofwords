# Sandbox Feedback Loop — Prompts tools in Training mode

Adds agent-facing tools so the **training-mode** agent can curate the new
**Prompts** (reusable, completion-shaped saved requests / conversation starters /
templated prompts) the same way it already curates Instructions:

- `create_prompt` — save a reusable prompt and attach it to the agent(s) being trained
- `search_prompts` — list/search existing prompts (dedupe before creating, or find one to edit)
- `edit_prompt` — refine text/title, re-attach to agents, toggle starter, adjust mode/parameters

This doc is the runnable feedback loop used to confirm the behavior in a fresh
sandbox.

---

## Decisions baked in (from the request)

1. **Prompts go live immediately.** Unlike instructions (which the training
   agent stages into a draft *build* that an admin approves), prompts have no
   build/version/approval model — `create_prompt` / `edit_prompt` write the live
   `Prompt` row directly via `PromptService`.
2. **Authoring is governed by the agent-manager tier (`manage_agent`).** This is
   PR #489's per-`data_source` **`manage`** grant, which `RESOURCE_PERM_IMPLIES`
   makes a superset (`manage` ⇒ `manage_instructions` / `create_entities` /
   `manage_evals` / `manage_members`). `PromptService.create_prompt` /
   `update_prompt` already enforce `manage` on every target agent (or org admin
   for `scope='global'`), so the tools inherit exactly that gate — no new
   permission key was introduced. Built on top of PR #490 (which carries #489).

---

## What changed (the feature)

Tool schemas — `backend/app/ai/tools/schemas/`:
- `create_prompt.py` (`CreatePromptInput/Output`, `PromptParameterSpec`)
- `edit_prompt.py` (`EditPromptInput/Output`)
- `search_prompts.py` (`SearchPromptsInput/Output`, `SearchPromptsItem`)

Tool implementations — `backend/app/ai/tools/implementations/`:
- `create_prompt.py` — `CreatePromptTool`, `category="action"`, `allowed_modes=["training"]`.
  Defaults to `scope='agent'`; when `data_source_ids` is omitted it attaches to
  the **active agents on the current report**. Maps `PromptService` 403 → a clean
  `permission_denied` rejection observation (no blind retry).
- `edit_prompt.py` — `EditPromptTool`, `category="action"`, `allowed_modes=["training"]`.
  Patch-style: only the fields you pass are changed. 404→`not_found`, 403→`permission_denied`.
- `search_prompts.py` — `SearchPromptsTool`, `category="research"`, `allowed_modes=["training"]`.
  Lists visible prompts via `PromptService.list_prompts` (access-scoped) and
  applies a literal+regex keyword union in-process, mirroring `search_instructions`.

Planner prompt — `backend/app/ai/agents/planner/prompt_builder_v3.py`:
- The `TRAINING MODE` block now lists `search_prompts` / `create_prompt` /
  `edit_prompt` under **Key tools** and adds routing examples
  (`"add a prompt for…"` → `create_prompt`, `"list saved prompts"` → `search_prompts`,
  `"rename that prompt"` → `edit_prompt`).

Gating is entirely via `allowed_modes=["training"]` + the registry mode filter
(`ToolRegistry._matches_filter`) — `agent_v2` already builds the catalog with
`mode=self.mode`, so no agent_v2 change was needed.

---

## Loop A — registry gating (no DB, no LLM)

Confirms the tools exist **only** in training mode.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python - <<'PY'
from app.ai.registry import ToolRegistry
r = ToolRegistry()
for mode in ("training", "chat"):
    names = set()
    for pt in ("action", "research"):
        names |= {t["name"] for t in r.get_catalog_for_plan_type(pt, mode=mode)}
    print(mode, "->", sorted(n for n in names if "prompt" in n))
PY
```

**Observed (PASS):**
```
training -> ['create_prompt', 'edit_prompt', 'search_prompts']
chat -> []
```

---

## Loop B — tool behavior + manage_agent gate (DB, no LLM)

Drives all three tools through `run_stream` against the real `PromptService`,
including the authorization gate: a user holding only a per-agent `manage` grant
(the manage_agent tier) can create/search/edit; a member without it is denied.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
.venv/bin/python -m pytest tests/prompts/test_prompt_training_tools.py -v -s
```

**Asserts:**
- create (manager, agent-scoped) → `success`, attached to the agent, `is_starter`.
- create (non-manager of the agent) → `rejected_reason == "permission_denied"`.
- `search_prompts(query=["revenue"])` and `starters_only` return the prompt.
- `edit_prompt` renames + toggles starter; persists (re-search confirms).
- `edit_prompt` by a non-manager → `permission_denied`.

**Observed (PASS):** `1 passed`.

---

## Loop C — live UI + LLM (training mode, Haiku)

Run the app, open an agent in **Training** mode, and confirm a real model turn
creates/searches/edits a prompt that then shows up on the agent.

```bash
# backend (Anthropic Haiku key supplied out-of-band, never committed)
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
export ANTHROPIC_API_KEY=...   # haiku test key
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# frontend
cd frontend && npm run dev
```

Steps:
1. Create/seed an agent (data source) you manage.
2. Open a report on that agent and switch the composer to **Training** mode.
3. Prompt: *"Add a saved prompt for monthly revenue by category and make it a starter."*
   → the agent calls `create_prompt`; the prompt appears under the agent's prompts.
4. Prompt: *"List the saved prompts."* → `search_prompts` returns it.
5. Prompt: *"Rename that prompt to 'Monthly revenue v2'."* → `edit_prompt` updates it.

Screenshot proof is captured in the PR / session.

---

## Notes / non-goals

- No build/approval flow for prompts (decision #1) — writes are live.
- `scope='global'` remains org-admin-only (enforced by `PromptService`); the
  tool surfaces the 403 as `permission_denied` rather than failing hard.
- `required_permissions` on the tool metadata is left empty on purpose: the real
  gate is the service-layer `manage` check, and `agent_v2` does not pass
  `required_permissions` into catalog filtering.
